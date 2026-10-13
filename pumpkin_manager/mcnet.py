"""Talking to a running Pumpkin server: Server List Ping and the JSON-RPC management protocol (MSMP)."""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

from websockets.asyncio.client import connect

# ----------------------------------------------------------------- Server List Ping


class PingError(Exception):
    pass


class StillStartingError(PingError):
    """The server answered, but with a message that it is still starting instead of its status: Forge 1.7.10 does that until every
    mod has finished starting (GT New Horizons goes on for half a minute after its "Done" line). Not the same as a silent server."""


def _encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    result = 0
    for i in range(5):
        if offset + i >= len(data):
            raise PingError("truncated VarInt")
        byte = data[offset + i]
        result |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return result, offset + i + 1
    raise PingError("VarInt too long")


async def _read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    for i in range(5):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return result
    raise PingError("VarInt too long")


def flatten_motd(description: Any) -> str:
    """Reduce a chat component (str or nested dict/list) to plain text."""
    if isinstance(description, str):
        return description
    if isinstance(description, list):
        return "".join(flatten_motd(part) for part in description)
    if isinstance(description, dict):
        text = str(description.get("text", ""))
        return text + "".join(flatten_motd(part) for part in description.get("extra", []))
    return ""


async def server_list_ping(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """Return ``{online, max, version, protocol, motd, sample}`` or raise ``PingError``."""
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                host_bytes = host.encode("utf-8")
                handshake = (
                    _encode_varint(0x00)
                    + _encode_varint(0xFFFFFFFF)  # protocol -1: "I just want the status"
                    + _encode_varint(len(host_bytes)) + host_bytes
                    + struct.pack(">H", port)
                    + _encode_varint(1)
                )
                writer.write(_encode_varint(len(handshake)) + handshake + b"\x01\x00")
                await writer.drain()

                length = await _read_varint(reader)
                if not 0 < length <= 1 << 20:
                    raise PingError("implausible status packet length")
                packet = await reader.readexactly(length)
                packet_id, offset = _decode_varint(packet)
                if packet_id != 0:
                    raise PingError(f"unexpected packet id {packet_id}")
                json_len, offset = _decode_varint(packet, offset)
                payload = json.loads(packet[offset : offset + json_len].decode("utf-8", "replace"))
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
    except (TimeoutError, OSError, asyncio.IncompleteReadError, ValueError) as exc:
        raise PingError(str(exc) or exc.__class__.__name__) from exc

    if not isinstance(payload, dict):  # Forge 1.7.10 answers with a plain message ("Server is still starting!...") while it loads
        raise StillStartingError(f"the server sent a message instead of its status: {str(payload)[:100]}")
    players = payload.get("players") if isinstance(payload.get("players"), dict) else {}
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    return {
        "online": int(players.get("online", 0)),
        "max": int(players.get("max", 0)),
        "version": str(version.get("name", "")),
        "protocol": version.get("protocol"),
        "motd": flatten_motd(payload.get("description", "")),
        "sample": [p.get("name") for p in players.get("sample", []) if isinstance(p, dict) and p.get("name")],
    }


# ------------------------------------------------------------------------- MSMP


class MsmpError(Exception):
    pass


class MsmpClient:
    """Minimal JSON-RPC 2.0 client for Pumpkin's management server (one request at a time).

    Connections are short-lived on purpose: the manager only needs a handful of calls (player list,
    save, announcements), and a connection per operation never has to be repaired after restarts.
    """

    def __init__(self, port: int, secret: str, host: str = "127.0.0.1", timeout: float = 8.0):
        self.uri = f"ws://{host}:{port}/"
        self.secret = secret
        self.timeout = timeout
        self._ws: Any = None
        self._next_id = 0

    async def __aenter__(self) -> "MsmpClient":
        try:
            self._ws = await connect(
                self.uri,
                additional_headers={"Authorization": f"Bearer {self.secret}"},
                open_timeout=min(self.timeout, 5.0),
                ping_interval=None,
                max_size=4 * 1024 * 1024,
            )
        except Exception as exc:  # websockets raises a broad family of errors
            raise MsmpError(f"could not connect to the management server: {exc}") from exc
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        self._next_id += 1
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            request["params"] = params
        try:
            async with asyncio.timeout(timeout or self.timeout):
                await self._ws.send(json.dumps(request))
                while True:
                    message = json.loads(await self._ws.recv())
                    # Notifications (no id) can arrive between request and response; skip them.
                    if isinstance(message, dict) and message.get("id") == self._next_id:
                        if message.get("error"):
                            err = message["error"]
                            raise MsmpError(f"{method}: {err.get('message')} {err.get('data') or ''}".strip())
                        return message.get("result")
        except TimeoutError as exc:
            raise MsmpError(f"{method}: timed out") from exc
        except MsmpError:
            raise
        except Exception as exc:
            raise MsmpError(f"{method}: {exc}") from exc

    # Convenience wrappers for the calls the manager uses.
    async def status(self) -> dict[str, Any]:
        return await self.call("minecraft:server/status")

    async def players(self) -> list[dict[str, Any]]:
        return await self.call("minecraft:players") or []

    async def save(self, flush: bool = True, timeout: float = 120.0) -> bool:
        return bool(await self.call("minecraft:server/save", {"flush": flush}, timeout=timeout))

    async def broadcast(self, text: str, overlay: bool = False) -> bool:
        return bool(
            await self.call(
                "minecraft:server/system_message", {"message": {"message": {"literal": text}, "overlay": overlay}}
            )
        )
