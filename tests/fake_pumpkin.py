"""A stand-in for the Pumpkin binary, used by the test-suite.

It mirrors the behaviour the manager depends on (taken from Pumpkin's source and from runs of the real 0.2.0
binary): it reads ``pumpkin.toml`` from the working directory, prints the same startup lines, answers the Java
server-list ping, serves the JSON-RPC management protocol over a WebSocket with Bearer authentication, reads
console commands from stdin and shuts down gracefully on ``stop`` / SIGTERM.

Like the real thing it keeps whitelist/ops/ban lists in a ``data`` folder (written as soon as a command changes
them), starts every new world on Normal difficulty whatever ``default_difficulty`` says, and only saves a
difficulty set with the ``difficulty`` command when the world is saved.

Usage: ``python fake_pumpkin.py BUILD_FILE`` where BUILD_FILE is a JSON document (padded with spaces) that
selects the behaviour: version, startup_delay, crash_on_start, corrupt_world, crash_after, hang_on_stop,
no_ready_line, exit_code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import threading
import time
import tomllib
import uuid
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve


def read_build_file(path: str) -> dict:
    """The build file is JSON; it may start with an ELF magic number and end with padding (as real binaries do)."""
    text = Path(path).read_bytes().removeprefix(b"\x7fELF").decode("utf-8", "replace")
    return json.loads(text.strip().strip("\x00").strip())


BUILD = read_build_file(sys.argv[1]) if len(sys.argv) > 1 else {}
VERSION = str(BUILD.get("version", "1.0"))
CFG = tomllib.loads(Path("pumpkin.toml").read_text(encoding="utf-8")) if Path("pumpkin.toml").exists() else {}
NET = CFG.get("networking", {})
JAVA = NET.get("java", {})
MGMT = NET.get("management", {})

players: list[str] = []
KEEP_ALIVE: list[object] = []  # listening sockets must stay referenced for the life of the process
autosave = True
difficulty = "normal"  # in memory until the world is saved; pumpkin.toml's default_difficulty is not consulted (as in the real server)
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
DATA_FILES = ("whitelist.json", "ops.json", "banned-players.json", "banned-ips.json")
stop_event: asyncio.Event
loop: asyncio.AbstractEventLoop


def log(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} \x1b[32mINFO\x1b[0m  {text}", flush=True)


def reply(text: str) -> None:
    """Command feedback: the real server prints it without a timestamp."""
    print(text, flush=True)


def world_file() -> Path:
    return Path("world") / "level.dat"


def init_world() -> None:
    global difficulty
    Path("world/region").mkdir(parents=True, exist_ok=True)
    region = Path("world/region/r.0.0.mca")
    if not region.exists():
        region.write_bytes(os.urandom(64 * 1024))
    if not world_file().exists():
        world_file().write_text(json.dumps({"created_by": VERSION, "saves": 0, "blocks": 0}), encoding="utf-8")
    try:
        difficulty = str(json.loads(world_file().read_text(encoding="utf-8")).get("difficulty", "normal"))
    except ValueError:
        log("ERROR corrupt level.dat")
        raise SystemExit(102)


def init_data_files() -> None:
    Path("data").mkdir(exist_ok=True)
    for name in DATA_FILES:
        if not (Path("data") / name).exists():
            (Path("data") / name).write_text("[]", encoding="utf-8")


def save_world(reason: str = "save") -> None:
    data = json.loads(world_file().read_text(encoding="utf-8"))
    data["saves"] += 1
    data["last_saved_by"] = VERSION
    data["difficulty"] = difficulty
    world_file().write_text(json.dumps(data), encoding="utf-8")
    log(f"World saved ({reason})")


def offline_uuid(name: str) -> str:
    """The UUID an offline-mode server derives from a name (Java's UUID.nameUUIDFromBytes)."""
    return str(uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{name}".encode()).digest(), version=3))


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S+00:00", time.gmtime())


def edit_list(filename: str, key: str, value: str, add: bool, entry: dict | None = None) -> bool:
    """Add or remove the entry whose ``key`` is ``value`` in a data file; False when there was nothing to do."""
    path = Path("data") / filename
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    present = any(e.get(key) == value for e in entries)
    if add == present:
        return False
    entries = [e for e in entries if e.get(key) != value] + ([entry] if add else [])
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return True


def read_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    result = 0
    for i in range(5):
        byte = buf[pos + i]
        result |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return result, pos + i + 1
    raise ValueError("varint")


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def status_json() -> str:
    return json.dumps({
        "version": {"name": VERSION, "protocol": 800},
        "players": {"max": int(JAVA.get("max_players", 1000)), "online": len(players), "sample": [{"name": p, "id": "0"} for p in players]},
        "description": {"text": str(JAVA.get("motd", "A blazingly fast Pumpkin server!"))},
    })


async def slp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            length, _ = read_varint(await reader.readexactly(1) + b"\0" * 0)
            packet = await reader.readexactly(length)
            packet_id, pos = read_varint(packet)
            if packet_id == 0 and len(packet) > 1:
                continue  # handshake
            if packet_id == 0:
                body = status_json().encode()
                payload = varint(0) + varint(len(body)) + body
                writer.write(varint(len(payload)) + payload)
                await writer.drain()
            elif packet_id == 1:
                writer.write(varint(len(packet)) + packet)
                await writer.drain()
                break
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, IndexError):
        pass
    finally:
        writer.close()


async def msmp_handler(connection) -> None:
    async for raw in connection:
        msg = json.loads(raw)
        method, params, mid = msg.get("method"), msg.get("params"), msg.get("id")
        result = None
        if method == "minecraft:players":
            result = [{"id": f"00000000-0000-0000-0000-{i:012d}", "name": n} for i, n in enumerate(players)]
        elif method == "minecraft:server/status":
            result = {"started": True, "players": [{"name": n} for n in players], "version": {"name": VERSION, "protocol": 800}}
        elif method == "minecraft:server/save":
            if BUILD.get("slow_save"):
                await asyncio.sleep(float(BUILD["slow_save"]))
            save_world("rpc")
            result = True
        elif method == "minecraft:server/system_message":
            text = (params or {}).get("message", {}).get("message", {}).get("literal", "")
            log(f"[system message] {text}")
            result = True
        elif method == "minecraft:server/stop":
            loop.call_soon(stop_event.set)
            result = True
        else:
            await connection.send(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}))
            continue
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}))


def msmp_auth(connection, request):
    expected = f"Bearer {MGMT.get('secret', '')}"
    if request.headers.get("Authorization") != expected:
        return connection.respond(HTTPStatus.UNAUTHORIZED, '{"error":"Unauthorized"}\n')
    return None


def handle_player_command(cmd: str, args: list[str]) -> bool:
    """whitelist / op / deop / ban / pardon / ban-ip / pardon-ip / kick, with the replies of the real server."""
    name = args[1] if cmd == "whitelist" and len(args) >= 2 else (args[0] if args else "")
    reason = " ".join(args[1:]) or "Banned by an operator."
    if cmd == "whitelist" and len(args) == 2 and args[0] in ("add", "remove"):
        add = args[0] == "add"
        if edit_list("whitelist.json", "name", name, add, {"uuid": offline_uuid(name), "name": name}):
            reply(f"Added {name} to the allowlist" if add else f"Removed {name} from the allowlist")
        else:
            reply("Could not add %1$s to the allowlist" if add else "Could not remove %1$s from the allowlist")
    elif cmd == "op" and name:
        entry = {"uuid": offline_uuid(name), "name": name, "level": int(CFG.get("op_permission_level", 4)), "bypasses_player_limit": False}
        reply(f"Opped: {name}" if edit_list("ops.json", "name", name, True, entry) else "Could not op (already op or higher): %s")
    elif cmd == "deop" and name:
        reply(f"De-opped: {name}" if edit_list("ops.json", "name", name, False) else "Could not de-op (permission level too high): %s")
    elif cmd == "ban" and name:
        entry = {"uuid": offline_uuid(name), "name": name, "created": stamp(), "source": "Server", "expires": "forever", "reason": reason}
        if edit_list("banned-players.json", "name", name, True, entry):
            reply(f"Banned player {name}")
            if name in players:
                players.remove(name)
                log(f"{name} left the game")
        else:
            reply("Could not ban player %1$s")
    elif cmd == "pardon" and name:
        reply(f"Unbanned player {name}" if edit_list("banned-players.json", "name", name, False) else f"Could not unban player {name}")
    elif cmd == "ban-ip" and name:
        entry = {"ip": name, "created": stamp(), "source": "Server", "expires": "forever", "reason": reason}
        reply(f"Banned IP address {name}" if edit_list("banned-ips.json", "ip", name, True, entry) else "Could not ban IP address %1$s")
    elif cmd == "pardon-ip" and name:
        reply(f"Unbanned IP address {name}" if edit_list("banned-ips.json", "ip", name, False) else "Nothing changed. That IP isn't banned")
    elif cmd == "kick" and name:
        if name in players:
            players.remove(name)
            log(f"{name} left the game")
            reply(f"Kicked {name}")
        else:
            reply("No player was found")
    else:
        return False
    return True


def handle_difficulty(args: list[str]) -> None:
    global difficulty
    if not args:
        reply(f"The difficulty is {difficulty.capitalize()}")
    elif args[0] not in DIFFICULTIES:  # the real command only accepts lower case
        reply("Incorrect argument for command")
    elif args[0] == difficulty:
        reply(f"The difficulty did not change; it is already set to {difficulty}")
    else:
        difficulty = args[0]
        reply(f"Set game difficulty to {difficulty.capitalize()}")


def handle_command(line: str) -> None:
    global autosave
    parts = line.strip().split()
    if not parts:
        return
    cmd, args = parts[0], parts[1:]
    if cmd == "stop":
        if BUILD.get("hang_on_stop"):
            log("ignoring stop (hang_on_stop)")
            return
        stop_event.set()
    elif cmd == "save-all":
        save_world("save-all")
    elif cmd == "save-off":
        autosave = False
        log("Automatic saving is now disabled")
    elif cmd == "save-on":
        autosave = True
        log("Automatic saving is now enabled")
    elif cmd == "say":
        log(f"[Server] {' '.join(args)}")
    elif cmd == "fakejoin" and args:
        players.append(args[0])
        log(f"{args[0]} joined the game")
    elif cmd == "fakeleave" and args:
        if args[0] in players:
            players.remove(args[0])
        log(f"{args[0]} left the game")
    elif cmd == "difficulty":
        handle_difficulty(args)
    elif handle_player_command(cmd, args):
        pass
    elif cmd == "list":
        log(f"There are {len(players)} players online: {', '.join(players)}")
    else:
        log(f"Unknown or incomplete command: {line.strip()}")


def stdin_reader() -> None:
    for line in sys.stdin:
        loop.call_soon_threadsafe(handle_command, line)


async def main() -> int:
    global stop_event, loop
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    log(f"Starting \x1b[33mPumpkin\x1b[0m 0.0.0-fake {VERSION} Java Minecraft (Protocol 800)")
    await asyncio.sleep(float(BUILD.get("startup_delay", 0.05)))

    if BUILD.get("corrupt_world"):
        Path("world").mkdir(exist_ok=True)
        world_file().write_text("CORRUPTED BY " + VERSION, encoding="utf-8")
    if BUILD.get("crash_on_start"):
        print("thread 'main' panicked at src/lib.rs:1:1: simulated crash", flush=True)
        return int(BUILD.get("exit_code", 101))
    init_world()
    init_data_files()

    host, _, port = str(JAVA.get("address", "0.0.0.0:25565")).rpartition(":")
    if not BUILD.get("no_ready_line"):
        KEEP_ALIVE.append(await asyncio.start_server(slp_client, host or "0.0.0.0", int(port)))
    if MGMT.get("enabled"):
        mhost, _, mport = str(MGMT["address"]).rpartition(":")
        await serve(msmp_handler, mhost, int(mport), process_request=msmp_auth)
        log(f"Management server is listening on {MGMT['address']}")
    threading.Thread(target=stdin_reader, daemon=True).start()
    if hasattr(signal, "SIGTERM") and os.name != "nt":
        if BUILD.get("ignore_sigterm"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # really ignore it, so only SIGKILL can end this process
        else:
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    if not BUILD.get("no_ready_line"):
        log("Started server; took 12ms")
        log(f"Server is now running. Connect using port: Java Edition: {JAVA.get('address')}")
    if BUILD.get("crash_after"):
        async def crash_later() -> None:
            await asyncio.sleep(float(BUILD["crash_after"]))
            print("thread 'tokio-runtime-worker' panicked: simulated late crash", flush=True)
            os._exit(int(BUILD.get("exit_code", 101)))

        asyncio.create_task(crash_later())

    await stop_event.wait()
    log("Received stop request; stopping server...")
    save_world("shutdown")
    log("The server has stopped.")
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
