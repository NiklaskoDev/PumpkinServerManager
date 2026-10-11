"""Server lifecycle: creation, configuration, start/stop, crash handling and live monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import string
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import pumpkin_config as pc
from .binaries import BuildStore
from .config import Settings
from .errors import UserError
from .events import EventBus
from .jobs import JobContext, JobService
from .mcnet import MsmpClient, MsmpError, PingError, server_list_ping
from .models import AppSettings, BuildRecord, ServerPolicy, ServerRecord
from .process import ProcessError, ProcessManager
from .releases import ReleaseError, ReleaseSource, RemoteBuild
from .repo import Repo
from .reporter import Reporter
from .util import SERVER_ID_RE, atomic_write_text, dir_size, iso, slugify

log = logging.getLogger(__name__)

PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_SECRET_ALPHABET = string.ascii_letters + string.digits
_CHILD_ENV_KEEP = {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
_CHILD_ENV_KEEP_WINDOWS = {
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "WINDIR", "PROGRAMDATA", "ALLUSERSPROFILE",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "COMMONPROGRAMFILES",
}

CommandBuilder = Callable[[ServerRecord, BuildRecord], list[str]]


class ServerError(UserError):
    """A server operation could not be carried out."""


def port_is_free(port: int, udp: bool = False) -> bool:
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


class ServerService:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        events: EventBus,
        pm: ProcessManager,
        builds: BuildStore,
        jobs: JobService,
        reporter: Reporter,
        source_getter: Callable[[], ReleaseSource],
        app_settings: Callable[[], AppSettings],
        command_builder: CommandBuilder | None = None,
    ):
        self.settings = settings
        self.repo = repo
        self.events = events
        self.pm = pm
        self.builds = builds
        self.jobs = jobs
        self.reporter = reporter
        self._source = source_getter
        self._app_settings = app_settings
        self.command_builder: CommandBuilder = command_builder or (lambda rec, build: [str(builds.binary_path(build.id))])
        self.backups: Any = None  # attached by the application factory (BackupService)
        self._create_lock = asyncio.Lock()
        self._disk_cache: dict[str, tuple[float, int]] = {}
        self._last_poll: dict[str, float] = {}
        settings.servers_dir.mkdir(parents=True, exist_ok=True)
        pm.on_change = self.publish_state
        pm.on_exit = self._on_exit

    # ------------------------------------------------------------------- paths
    def server_dir(self, server_id: str) -> Path:
        if not SERVER_ID_RE.match(server_id):
            raise ServerError("Invalid server id.")
        return self.settings.servers_dir / server_id

    def config_path(self, server_id: str) -> Path:
        return self.server_dir(server_id) / "pumpkin.toml"

    def get(self, server_id: str) -> ServerRecord:
        rec = self.repo.get_server(server_id)
        if rec is None:
            raise ServerError("That server does not exist.", 404)
        return rec

    def managed(self, rec: ServerRecord) -> pc.ManagedConfig:
        return pc.ManagedConfig(rec.java_port, rec.mgmt_port, rec.mgmt_secret, rec.bedrock_port)

    # ------------------------------------------------------------------- views
    def view(self, rec: ServerRecord, detail: bool = False) -> dict[str, Any]:
        rt = self.pm.runtime(rec.id)
        build = self.repo.get_build(rec.build_id)
        previous = self.repo.get_build(rec.previous_build_id)
        pending = self.repo.get_build(rec.pending_build_id)
        app = self._app_settings()
        lo, hi = self.settings.port_range
        blo, bhi = self.settings.bedrock_port_range
        view: dict[str, Any] = {
            "id": rec.id,
            "name": rec.name,
            "created_at": rec.created_at,
            "state": rt.state,
            "busy": self.jobs.busy_operation(rec.id),
            "desired_running": rec.desired_running,
            "uptime": rt.uptime(),
            "exit_code": rt.exit_code,
            "last_error": rt.last_error,
            "channel": rec.channel,
            "pinned_tag": rec.pinned_tag,
            "build": build.public() if build else None,
            "previous_build": previous.public() if previous else None,
            "ports": {
                "java": rec.java_port,
                "bedrock": rec.bedrock_port,
                "java_published": lo <= rec.java_port <= hi,
                "bedrock_published": rec.bedrock_port is None or blo <= rec.bedrock_port <= bhi,
            },
            "public_address": app.public_address or self.settings.public_address,
            "players": {"online": rt.players_online, "max": rt.players_max, "names": rt.player_names},
            "mc_version": rt.mc_version,
            "motd": rt.motd,
            "ping_ok": rt.ping_ok,
            "msmp_ok": rt.msmp_ok,
            "stats": {"cpu": rt.cpu_percent, "rss": rt.rss_bytes},
            "update": {
                "mode": rec.policy.update_mode,
                "available": pending is not None,
                "pending_build": pending.public() if pending else None,
                "pending_since": rec.pending_since,
                "last_check": rec.last_update_check_at,
            },
            "last_backup_at": rec.last_backup_at,
            "policy": rec.policy.model_dump(),
        }
        if detail:
            view["stats"]["cpu_history"] = list(rt.cpu_history)
            view["stats"]["mem_history"] = list(rt.mem_history)
            view["disk_bytes"] = self._disk_cache.get(rec.id, (0, None))[1]
        return view

    def list_views(self) -> list[dict[str, Any]]:
        return [self.view(rec) for rec in self.repo.list_servers()]

    def publish_state(self, server_id: str) -> None:
        rec = self.repo.get_server(server_id)
        if rec:
            self.events.publish("servers", "server", self.view(rec))

    # -------------------------------------------------------------------- ports
    def allocate_port(self, kind: str, preferred: int | None = None) -> int:
        all_used = self.repo.used_ports()
        # Java and management listen on TCP, so they must never share a port (even if the ranges were configured to overlap).
        used = all_used["bedrock"] if kind == "bedrock" else all_used["java"] | all_used["mgmt"]
        ranges = {"java": self.settings.port_range, "bedrock": self.settings.bedrock_port_range, "mgmt": self.settings.mgmt_port_range}
        lo, hi = ranges[kind]
        udp = kind == "bedrock"

        def usable(port: int) -> bool:
            if port in used or not port_is_free(port, udp):
                return False
            if kind == "bedrock":
                # A Bedrock server also binds port+1 (IPv6 server-list status; a dual-stack socket on Linux), so
                # neighbouring servers need a gap of two between them.
                return port + 1 not in used and port - 1 not in used and port_is_free(port + 1, udp=True)
            return True

        if preferred is not None:
            if not 1024 <= preferred <= 65535:
                raise ServerError("Ports must be between 1024 and 65535.")
            if preferred in used or (kind == "bedrock" and (preferred + 1 in used or preferred - 1 in used)):
                raise ServerError(f"Port {preferred} is already assigned to another server.")
            if not usable(preferred):
                raise ServerError(f"Port {preferred} is already in use by another program.")
            return preferred
        for port in range(lo, hi + 1):
            if usable(port):
                return port
        extra = " Widen PORT_RANGE and the published ports of the container." if kind != "mgmt" else ""
        if kind == "bedrock":
            extra = " Each Bedrock server needs two UDP ports; widen BEDROCK_PORT_RANGE and the published ports of the container."
        raise ServerError(f"No free {'Bedrock (UDP) ' if udp else ''}port left in {lo}-{hi}.{extra}")

    # ------------------------------------------------------------------- create
    async def resolve_build(self, channel: str, pinned_tag: str | None, ctx: JobContext | None = None) -> BuildRecord:
        try:
            remote: RemoteBuild = await (self._source().by_tag(pinned_tag) if pinned_tag else self._source().latest(channel))
            return self.builds.register(remote)
        except ReleaseError as exc:
            cached = [b for b in self.repo.list_builds() if b.downloaded and b.channel == channel and (not pinned_tag or b.tag == pinned_tag)]
            if not cached:
                raise
            if ctx:
                ctx.warn(f"Could not reach GitHub ({exc}); using the cached build {cached[0].label}")
            return cached[0]

    def _download_progress(self, ctx: JobContext, base: float = 0.0, span: float = 0.8) -> Callable[[int, int], None]:
        def callback(done: int, total: int) -> None:
            fraction = base + span * (done / total if total else 0)
            ctx.progress(fraction, f"Downloading Pumpkin ({done // 1048576} of {total // 1048576} MiB)")

        return callback

    def _unique_id(self, name: str) -> str:
        base = slugify(name)
        existing = {r.id for r in self.repo.list_servers()}
        candidate, n = base, 2
        while candidate in existing or (self.settings.servers_dir / candidate).exists():
            candidate = f"{base[:36]}-{n}"
            n += 1
        return candidate

    async def create(self, spec: dict[str, Any], ctx: JobContext) -> ServerRecord:
        name = " ".join(str(spec["name"]).split())
        if not 1 <= len(name) <= 40:
            raise ServerError("The server name must be 1-40 characters long.")
        if any(r.name.lower() == name.lower() for r in self.repo.list_servers()):
            raise ServerError("A server with that name already exists.")
        app = self._app_settings()
        channel = spec.get("channel") or app.default_channel
        pinned = spec.get("pinned_tag") or None
        try:
            policy = ServerPolicy.model_validate(spec.get("policy") or {})
            values = pc.validate_values({"telemetry": app.telemetry_default, **(spec.get("settings") or {})})
        except pc.ConfigError as exc:
            raise ServerError(f"Invalid settings: {exc}") from exc
        except ValueError as exc:
            raise ServerError(f"Invalid policy: {exc}") from exc

        ctx.step("Looking up the Pumpkin build")
        build = await self.resolve_build(channel, pinned, ctx)
        ctx.step(f"Using Pumpkin {build.label} ({build.channel})")
        if not self.builds.is_available(build):
            ctx.step("Downloading the server binary")
            build = await self.builds.ensure(build, self._download_progress(ctx))
        ctx.progress(0.85, "Creating the server")

        async with self._create_lock:
            server_id = self._unique_id(name)
            java_port = self.allocate_port("java", spec.get("java_port"))
            bedrock_port = self.allocate_port("bedrock", spec.get("bedrock_port")) if spec.get("bedrock") else None
            mgmt_port = self.allocate_port("mgmt")
            rec = ServerRecord(
                id=server_id, name=name, created_at=iso(), channel=build.channel if pinned else channel, pinned_tag=pinned,
                build_id=build.id, previous_build_id=None, java_port=java_port, bedrock_port=bedrock_port,
                mgmt_port=mgmt_port, mgmt_secret="".join(secrets.choice(_SECRET_ALPHABET) for _ in range(40)),
                policy=policy, desired_running=False, last_backup_at=iso(), last_update_check_at=None,
                pending_build_id=None, pending_since=None,
            )
            directory = self.server_dir(server_id)
            try:
                directory.mkdir(parents=True)
                atomic_write_text(directory / "pumpkin.toml", pc.build_initial(values, self.managed(rec)))
                self.repo.insert_server(rec)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        ctx.claim(rec.id)
        ctx.step(f"Server '{name}' created on port {java_port}")
        self.reporter.record("success", "server", f"Server '{name}' created ({build.label}, port {java_port})", rec.id)
        self.publish_state(rec.id)
        if spec.get("start", True):
            await self._do_start(rec, ctx)
        return rec

    # -------------------------------------------------------------- start / stop
    def ensure_config(self, rec: ServerRecord, ctx: JobContext | None = None) -> None:
        """Re-assert manager-owned keys; refuse to start on a file Pumpkin would silently ignore."""
        path = self.config_path(rec.id)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        try:
            new_text, changed = pc.apply_managed(text, self.managed(rec))
        except pc.ConfigError as exc:
            raise ServerError(
                f"{exc} Pumpkin would ignore the whole file and start with default settings. "
                "Fix pumpkin.toml (Settings > Advanced or the Files tab) and start again."
            ) from exc
        if changed or not path.is_file():
            atomic_write_text(path, new_text)
            if changed and ctx:
                ctx.step(f"Updated manager-controlled settings in pumpkin.toml ({', '.join(changed)})")
        try:
            pc.validate_raw(new_text)
        except pc.ConfigError as exc:
            # Not fatal (our checks are stricter than Pumpkin's in places): report it and let the user decide.
            if ctx:
                ctx.warn(f"pumpkin.toml has questionable values: {exc}")

    def child_env(self, rec: ServerRecord) -> dict[str, str]:
        keep = _CHILD_ENV_KEEP | (_CHILD_ENV_KEEP_WINDOWS if os.name == "nt" else set()) | set(self.settings.extra_env_passthrough)
        env = {k: v for k, v in os.environ.items() if k in keep}
        env.update(HOME=str(self.server_dir(rec.id)), RUST_BACKTRACE="1", TZ=self.settings.timezone)
        return env

    async def _do_start(self, rec: ServerRecord, ctx: JobContext) -> None:
        rec = self.get(rec.id)
        if self.pm.runtime(rec.id).is_active:
            ctx.step("The server is already running")
            return
        build = self.repo.get_build(rec.build_id)
        if build is None:
            raise ServerError("This server has no Pumpkin version assigned.")
        if not self.builds.is_available(build):
            ctx.step(f"Downloading Pumpkin {build.label}")
            build = await self.builds.ensure(build, self._download_progress(ctx, span=0.5))
        directory = self.server_dir(rec.id)
        if not directory.is_dir():
            raise ServerError("The server folder is missing.")
        self.ensure_config(rec, ctx)
        if not port_is_free(rec.java_port):
            raise ServerError(f"Port {rec.java_port} is already in use by another program. Change the server port in its settings.")
        if rec.bedrock_port and not port_is_free(rec.bedrock_port, udp=True):
            raise ServerError(f"UDP port {rec.bedrock_port} (Bedrock) is already in use by another program.")

        ctx.step(f"Starting Pumpkin {build.label}")
        ctx.progress(0.6)
        try:
            await self.pm.start(
                rec.id, self.command_builder(rec, build), directory, self.child_env(rec),
                ready_port=rec.java_port, ready_timeout=rec.policy.start_timeout,
            )
        except ProcessError as exc:
            self.reporter.record("error", "start", f"'{rec.name}' failed to start: {exc}", rec.id, notify="crashed", title=f"{rec.name} failed to start")
            raise ServerError(str(exc)) from exc
        self.repo.update_server(rec.id, desired_running=1)
        ctx.step("The server is up and accepting connections")
        self.reporter.record("success", "start", f"'{rec.name}' started", rec.id)
        self.publish_state(rec.id)

    async def _do_stop(self, rec: ServerRecord, ctx: JobContext, *, user_initiated: bool = False) -> None:
        rec = self.get(rec.id)
        rt = self.pm.runtime(rec.id)
        if user_initiated:
            self.repo.update_server(rec.id, desired_running=0)
        if not rt.is_active:
            ctx.step("The server is not running")
            return
        ctx.step("Stopping the server (the world is saved first)")
        code = await self.pm.stop(rec.id, rec.policy.stop_timeout)
        ctx.step(f"The server stopped (exit code {code})")
        if user_initiated:
            self.reporter.record("info", "stop", f"'{rec.name}' stopped", rec.id)
        self.publish_state(rec.id)

    async def _do_restart(self, rec: ServerRecord, ctx: JobContext) -> None:
        await self._do_stop(rec, ctx)
        ctx.progress(0.4)
        await self._do_start(rec, ctx)

    def submit_start(self, rec: ServerRecord, delay: float = 0) -> dict[str, Any]:
        async def run(ctx: JobContext) -> None:
            if delay:
                await asyncio.sleep(delay)
            await self._do_start(rec, ctx)

        return self.jobs.submit("start", f"Start {rec.name}", rec.id, run)

    def submit_stop(self, rec: ServerRecord) -> dict[str, Any]:
        return self.jobs.submit("stop", f"Stop {rec.name}", rec.id, lambda ctx: self._do_stop(rec, ctx, user_initiated=True))

    def submit_restart(self, rec: ServerRecord) -> dict[str, Any]:
        return self.jobs.submit("restart", f"Restart {rec.name}", rec.id, lambda ctx: self._do_restart(rec, ctx))

    def submit_create(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.jobs.submit("create", f"Create {spec.get('name', 'server')}", None, lambda ctx: self.create(spec, ctx))

    async def kill(self, rec: ServerRecord) -> None:
        await self.pm.kill(rec.id)
        self.repo.update_server(rec.id, desired_running=0)
        self.reporter.record("warning", "stop", f"'{rec.name}' was killed", rec.id)

    # ------------------------------------------------------------------- delete
    async def _do_delete(self, rec: ServerRecord, ctx: JobContext, final_backup: bool, delete_backups: bool) -> None:
        if self.pm.runtime(rec.id).is_active:
            await self._do_stop(rec, ctx, user_initiated=True)
        directory = self.server_dir(rec.id)
        if final_backup and directory.is_dir() and any(directory.iterdir()):
            ctx.step("Creating a final backup")
            await self.backups.create(rec, "pre-delete", ctx, mode="cold", note="Final backup before the server was deleted")
        ctx.step("Deleting the server files")
        await asyncio.to_thread(shutil.rmtree, directory, True)
        self.repo.delete_server(rec.id)
        self.pm.forget(rec.id)
        await asyncio.to_thread(shutil.rmtree, self.settings.logs_dir / rec.id, True)
        if delete_backups:
            for row in self.repo.list_backups(rec.id):
                self.backups.delete(row["id"], quiet=True)
        self.reporter.record("warning", "server", f"Server '{rec.name}' deleted", None)
        self.events.publish("servers", "deleted", {"id": rec.id})

    def submit_delete(self, rec: ServerRecord, final_backup: bool, delete_backups: bool) -> dict[str, Any]:
        return self.jobs.submit(
            "delete", f"Delete {rec.name}", rec.id, lambda ctx: self._do_delete(rec, ctx, final_backup, delete_backups)
        )

    # ------------------------------------------------------------------ patching
    def patch(self, rec: ServerRecord, changes: dict[str, Any]) -> tuple[ServerRecord, bool]:
        """Apply manager-level changes. Returns (record, restart_required)."""
        restart_required = False
        columns: dict[str, Any] = {}
        if "name" in changes:
            name = " ".join(str(changes["name"]).split())
            if not 1 <= len(name) <= 40:
                raise ServerError("The server name must be 1-40 characters long.")
            if any(r.name.lower() == name.lower() and r.id != rec.id for r in self.repo.list_servers()):
                raise ServerError("A server with that name already exists.")
            columns["name"] = name
        if "policy" in changes:
            merged = {**rec.policy.model_dump(), **changes["policy"]}
            try:
                columns["policy"] = ServerPolicy.model_validate(merged)
            except ValueError as exc:
                raise ServerError(f"Invalid policy: {exc}") from exc
        if "channel" in changes and changes["channel"] != rec.channel:
            columns.update(channel=changes["channel"], pending_build_id=None, pending_since=None)
        if "pinned_tag" in changes:
            columns["pinned_tag"] = changes["pinned_tag"] or None
            columns.update(pending_build_id=None, pending_since=None)
        if "java_port" in changes and changes["java_port"] != rec.java_port:
            columns["java_port"] = self.allocate_port("java", int(changes["java_port"]))
            restart_required = True
        if "bedrock" in changes:
            if changes["bedrock"] and rec.bedrock_port is None:
                columns["bedrock_port"] = self.allocate_port("bedrock", changes.get("bedrock_port"))
                restart_required = True
            elif not changes["bedrock"] and rec.bedrock_port is not None:
                columns["bedrock_port"] = None
                restart_required = True
            elif changes["bedrock"] and changes.get("bedrock_port") and changes["bedrock_port"] != rec.bedrock_port:
                columns["bedrock_port"] = self.allocate_port("bedrock", changes["bedrock_port"])
                restart_required = True
        if columns:
            self.repo.update_server(rec.id, **columns)
        rec = self.get(rec.id)
        if restart_required:
            self.ensure_config(rec)
        self.publish_state(rec.id)
        return rec, restart_required and self.pm.runtime(rec.id).is_active

    # ------------------------------------------------------------------- config
    def read_config(self, rec: ServerRecord) -> dict[str, Any]:
        path = self.config_path(rec.id)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        try:
            values = pc.read_values(text)
            error = None
        except pc.ConfigError as exc:
            values, error = pc.read_values(""), str(exc)
        return {"values": values, "schema": pc.schema(), "exists": path.is_file(), "error": error,
                "restart_required_hint": self.pm.runtime(rec.id).is_active}

    def update_config(self, rec: ServerRecord, values: dict[str, Any]) -> dict[str, Any]:
        clean = pc.validate_values(values)
        path = self.config_path(rec.id)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        new_text = pc.apply_managed(pc.apply_values(text, clean), self.managed(rec))[0]
        self._write_config(path, text, new_text)
        return {"saved": True, "restart_required": self.pm.runtime(rec.id).is_active}

    def read_raw_config(self, rec: ServerRecord) -> dict[str, Any]:
        path = self.config_path(rec.id)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {"content": pc.strip_secrets(text), "managed_keys": [".".join(p) for p, _ in self.managed(rec).items()]}

    def write_raw_config(self, rec: ServerRecord, content: str) -> dict[str, Any]:
        if len(content.encode("utf-8")) > 512 * 1024:
            raise ServerError("pumpkin.toml is limited to 512 KiB.")
        path = self.config_path(rec.id)
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        # The editor shows the secret masked; keep the real one.
        if "********" in content:
            content = re.sub(r'(?m)^(\s*secret\s*=\s*)"\*{8}"', rf'\1"{rec.mgmt_secret}"', content)
        pc.validate_raw(content)
        new_text, changed = pc.apply_managed(content, self.managed(rec))
        self._write_config(path, old, new_text)
        return {"saved": True, "reset_managed_keys": changed, "restart_required": self.pm.runtime(rec.id).is_active}

    def _write_config(self, path: Path, old_text: str, new_text: str) -> None:
        if old_text and old_text != new_text:
            with contextlib.suppress(OSError):
                atomic_write_text(path.with_name("pumpkin.toml.manager-bak"), old_text)
        pc.parse(new_text)  # never write something we cannot read back
        atomic_write_text(path, new_text)

    # ------------------------------------------------------------------ players
    def read_player_lists(self, rec: ServerRecord) -> dict[str, list[dict[str, Any]]]:
        directory = self.server_dir(rec.id)
        result: dict[str, list[dict[str, Any]]] = {}
        for key, filename in (
            ("whitelist", "whitelist.json"), ("ops", "ops.json"), ("banned_players", "banned-players.json"),
            ("banned_ips", "banned-ips.json"),
        ):
            entries: list[dict[str, Any]] = []
            try:
                data = json.loads((directory / filename).read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [e for e in data if isinstance(e, dict)][:2000]
            except (OSError, ValueError):
                pass
            result[key] = entries
        return result

    async def player_action(self, rec: ServerRecord, action: str, target: str, reason: str | None = None) -> None:
        commands = {
            "whitelist_add": "whitelist add {t}", "whitelist_remove": "whitelist remove {t}",
            "op": "op {t}", "deop": "deop {t}", "ban": "ban {t}", "pardon": "pardon {t}",
            "kick": "kick {t}", "ban_ip": "ban-ip {t}", "pardon_ip": "pardon-ip {t}",
        }
        if action not in commands:
            raise ServerError("Unknown player action.")
        if action in ("ban_ip", "pardon_ip"):
            try:
                target = str(ipaddress.ip_address(target.strip()))
            except ValueError:
                raise ServerError("That is not a valid IP address.") from None
        elif not PLAYER_NAME_RE.match(target):
            raise ServerError("Player names may only contain letters, digits, '_', '-' and '.'.")
        command = commands[action].format(t=target)
        if reason and action in ("ban", "kick", "ban_ip"):
            reason = " ".join(reason.split())[:100]
            if reason:
                command += f" {reason}"
        try:
            await self.pm.send_command(rec.id, command)
        except ProcessError as exc:
            raise ServerError(f"{exc} Player changes are applied through the server console, so the server must be running.") from exc

    async def command(self, rec: ServerRecord, command: str) -> None:
        try:
            await self.pm.send_command(rec.id, command)
        except ProcessError as exc:
            raise ServerError(str(exc)) from exc

    # ---------------------------------------------------------------- crash policy
    async def _on_exit(self, server_id: str, code: int | None, unexpected: bool) -> None:
        if not unexpected:
            return
        rec = self.repo.get_server(server_id)
        if rec is None:
            return
        rt = self.pm.runtime(server_id)
        self.reporter.record(
            "error", "crash", f"'{rec.name}' stopped unexpectedly (exit code {code})", server_id,
            notify="crashed", title=f"{rec.name} crashed",
        )
        if not rec.policy.restart_on_crash or not rec.desired_running or self.jobs.is_busy(server_id):
            return
        now = time.time()
        rt.crash_times = [t for t in rt.crash_times if now - t < 600] + [now]
        if len(rt.crash_times) > 3:
            self.reporter.record(
                "error", "crash", f"'{rec.name}' crashed {len(rt.crash_times)} times in 10 minutes; automatic restarts are paused", server_id,
                notify="crashed", title=f"{rec.name} keeps crashing",
            )
            return
        delay = 5 * 3 ** (len(rt.crash_times) - 1)
        self.reporter.record("info", "crash", f"Restarting '{rec.name}' in {delay} seconds", server_id)
        asyncio.get_running_loop().call_later(delay, self._restart_after_crash, server_id)

    def _restart_after_crash(self, server_id: str) -> None:
        rec = self.repo.get_server(server_id)
        if rec is None or self.pm.runtime(server_id).state != "crashed" or self.jobs.is_busy(server_id):
            return
        with contextlib.suppress(Exception):
            self.submit_start(rec)

    # ---------------------------------------------------------------- monitoring
    async def monitor_tick(self) -> None:
        now = time.monotonic()
        stats: dict[str, Any] = {}
        for server_id in self.pm.running_ids():
            rec = self.repo.get_server(server_id)
            if rec is None:
                continue
            self.pm.sample_stats(server_id)
            rt = self.pm.runtime(server_id)
            if rt.state == "running" and now - self._last_poll.get(server_id, 0) >= 10:
                self._last_poll[server_id] = now
                await self._poll_players(rec)
            stats[server_id] = {
                "cpu": rt.cpu_percent, "rss": rt.rss_bytes, "players": rt.players_online, "max_players": rt.players_max,
                "names": rt.player_names, "uptime": rt.uptime(),
            }
        if stats:
            self.events.publish("servers", "stats", stats)

    async def _poll_players(self, rec: ServerRecord) -> None:
        rt = self.pm.runtime(rec.id)
        before = (rt.players_online, tuple(rt.player_names), rt.mc_version)
        previous_names = set(rt.player_names)
        try:
            info = await server_list_ping("127.0.0.1", rec.java_port, timeout=2.5)
            rt.ping_ok = True
            rt.players_online, rt.players_max = info["online"], info["max"]
            rt.mc_version, rt.motd = info["version"] or rt.mc_version, info["motd"]
            names = list(info["sample"])
        except PingError:
            rt.ping_ok = False
            names = list(rt.player_names)
        try:
            async with MsmpClient(rec.mgmt_port, rec.mgmt_secret, timeout=4) as client:
                players = await client.players()
                names = [p.get("name") for p in players if p.get("name")]
                rt.msmp_ok = True
                if not rt.ping_ok:
                    rt.players_online = len(names)
        except MsmpError:
            rt.msmp_ok = False
        rt.player_names = sorted(names)
        for name in sorted(set(names) - previous_names):
            self.reporter.record("info", "player", f"{name} joined", rec.id)
        for name in sorted(previous_names - set(names)):
            self.reporter.record("info", "player", f"{name} left", rec.id)
        if before != (rt.players_online, tuple(rt.player_names), rt.mc_version):
            self.publish_state(rec.id)

    async def disk_usage(self, server_id: str, max_age: float = 300) -> int:
        """Size of the server folder; walking a big world is slow, so results are cached briefly."""
        cached = self._disk_cache.get(server_id)
        if cached is None or time.monotonic() - cached[0] > max_age:
            size = await asyncio.to_thread(dir_size, self.server_dir(server_id))
            self._disk_cache[server_id] = (time.monotonic(), size)
            return size
        return cached[1]

    async def refresh_disk_usage(self) -> None:
        for rec in self.repo.list_servers():
            await self.disk_usage(rec.id)

    # ------------------------------------------------------------- boot / reconcile
    def reconcile(self) -> None:
        """Repair leftovers of interrupted restores and remove temp folders."""
        base = self.settings.servers_dir
        for rec in self.repo.list_servers():
            directory = base / rec.id
            olds = sorted(base.glob(f".{rec.id}.old-*"))
            if not directory.exists() and olds:
                log.warning("Recovering %s from an interrupted restore", rec.id)
                olds[-1].rename(directory)
                olds = olds[:-1]
            for leftover in [*olds, *base.glob(f".{rec.id}.restore-*")]:
                shutil.rmtree(leftover, ignore_errors=True)
            if not directory.exists():
                log.warning("The folder of server %s is missing; creating an empty one", rec.id)
                directory.mkdir()

    def autostart_targets(self) -> list[ServerRecord]:
        return [
            rec for rec in self.repo.list_servers()
            if rec.policy.autostart == "always" or (rec.policy.autostart == "last" and rec.desired_running)
        ]

    def start_autostart(self) -> int:
        targets = self.autostart_targets()
        for index, rec in enumerate(targets):
            with contextlib.suppress(Exception):
                self.submit_start(rec, delay=index * 3)
        return len(targets)

    async def shutdown(self) -> None:
        def timeout_for(server_id: str) -> float:
            rec = self.repo.get_server(server_id)
            return float(rec.policy.stop_timeout) if rec else 30.0

        await self.pm.shutdown_all(timeout_for, self.settings.shutdown_timeout)
