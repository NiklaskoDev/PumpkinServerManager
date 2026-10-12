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
from .util import SERVER_ID_RE, atomic_write_text, dir_size, iso, plain_environment, slugify

log = logging.getLogger(__name__)

PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
PLAYER_DATA_DIR = "data"  # where Pumpkin keeps the lists; Java servers keep them in the server folder itself
JAVA_START_HINT = "modded servers can need several minutes on a first start; raise the startup timeout in the server's settings if it is still working."
JAVA_CLEAN_EXIT_HINT = (
    "It ended by itself without crashing. Some modpacks do that on their first start, after downloading extra files "
    "(Better MC does); starting the server again usually finishes the setup."
)
PLAYER_LIST_FILES = (
    ("whitelist", "whitelist.json"), ("ops", "ops.json"), ("banned_players", "banned-players.json"), ("banned_ips", "banned-ips.json"),
)
# Pumpkin ignores ``default_difficulty``: every new world starts on Normal and only the /difficulty command changes it.
NEW_WORLD_DIFFICULTY = "Normal"
_SECRET_ALPHABET = string.ascii_letters + string.digits

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
        self.access: Any = None  # attached by the application factory (AccessService)
        self.jvm: Any = None  # attached by the application factory (JavaServerService: the Java engine)
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
            "engine": rec.engine,
            "modpack": None,
            "java": None,
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
        if rec.is_java and self.jvm is not None:
            view["modpack"], view["java"] = self.jvm.describe(rec)
            view["update"]["pending_pack"] = view["modpack"]["pending"]
            view["update"]["available"] = view["modpack"]["pending"] is not None
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
        if spec.get("engine") == "java":
            if self.jvm is None:
                raise ServerError("Java servers are not available in this installation.")
            return await self.jvm.create(spec, ctx)
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
            values = pc.validate_values({
                "telemetry": app.telemetry_default, "op_permission_level": pc.DEFAULT_OP_LEVEL, **(spec.get("settings") or {}),
            })
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
        if rec.is_java:
            return self.jvm.ensure_config(rec, ctx)
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
        env = plain_environment(self.settings.extra_env_passthrough)
        env.update(HOME=str(self.server_dir(rec.id)), RUST_BACKTRACE="1", TZ=self.settings.timezone)
        return env

    async def _do_start(self, rec: ServerRecord, ctx: JobContext) -> None:
        rec = self.get(rec.id)
        if self.pm.runtime(rec.id).is_active:
            ctx.step("The server is already running")
            return
        directory = self.server_dir(rec.id)
        ready_re = timeout_hint = fail_re = None
        if rec.is_java:
            if self.jvm is None:
                raise ServerError("Java servers are not available in this installation.")
            if not directory.is_dir():
                raise ServerError("The server folder is missing.")
            launch = await self.jvm.prepare_start(rec, ctx)
            argv, env, label, ready_re, timeout_hint, fail_re = launch.argv, launch.env, launch.label, launch.ready_re, JAVA_START_HINT, launch.fail_re
        else:
            build = self.repo.get_build(rec.build_id)
            if build is None:
                raise ServerError("This server has no Pumpkin version assigned.")
            if not self.builds.is_available(build):
                ctx.step(f"Downloading Pumpkin {build.label}")
                build = await self.builds.ensure(build, self._download_progress(ctx, span=0.5))
            if not directory.is_dir():
                raise ServerError("The server folder is missing.")
            self.ensure_config(rec, ctx)
            argv, env, label = self.command_builder(rec, build), self.child_env(rec), f"Pumpkin {build.label}"
        if not port_is_free(rec.java_port):
            raise ServerError(f"Port {rec.java_port} is already in use by another program. Change the server port in its settings.")
        if rec.bedrock_port and not port_is_free(rec.bedrock_port, udp=True):
            raise ServerError(f"UDP port {rec.bedrock_port} (Bedrock) is already in use by another program.")
        fresh_world = False if rec.is_java else self._world_is_new(rec)  # must be looked at before Pumpkin creates the world

        ctx.step(f"Starting {label}")
        ctx.progress(0.6)
        for attempt in range(2 if rec.is_java else 1):  # a modpack may end its first start on purpose (JAVA_CLEAN_EXIT_HINT)
            try:
                await self.pm.start(
                    rec.id, argv, directory, env, ready_port=rec.java_port, ready_timeout=rec.policy.start_timeout,
                    ready_re=ready_re, timeout_hint=timeout_hint, strict_ping=rec.is_java, fail_re=fail_re,
                    clean_exit_hint=JAVA_CLEAN_EXIT_HINT if rec.is_java else "",
                )
                break
            except ProcessError as exc:
                if attempt == 0 and rec.is_java and exc.clean_exit:
                    ctx.step("The server ended by itself while starting (exit code 0), which some modpacks do after downloading extra "
                             "files on their first start. Starting it once more", "warning")
                    continue
                self.reporter.record("error", "start", f"'{rec.name}' failed to start: {exc}", rec.id, notify="crashed", title=f"{rec.name} failed to start")
                raise ServerError(str(exc)) from exc
        self.repo.update_server(rec.id, desired_running=1)
        ctx.step("The server is up and accepting connections")
        if not rec.is_java:
            await self._apply_difficulty(rec, ctx, fresh_world=fresh_world)
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
        holders = self.repo.users_with_server(rec.id)  # who gets told it is gone: the accounts that had access to it
        if self.pm.runtime(rec.id).is_active:
            await self._do_stop(rec, ctx, user_initiated=True)
        directory = self.server_dir(rec.id)
        if final_backup and directory.is_dir() and any(directory.iterdir()):
            ctx.step("Creating a final backup")
            await self.backups.create(rec, "pre-delete", ctx, mode="cold", note="Final backup before the server was deleted")
        ctx.step("Deleting the server files")
        await asyncio.to_thread(shutil.rmtree, directory, True)
        self.repo.delete_server(rec.id)  # the accounts' access to it goes with it
        if self.access is not None:
            self.access.invalidate()
        self.pm.forget(rec.id)
        await asyncio.to_thread(shutil.rmtree, self.settings.logs_dir / rec.id, True)
        if delete_backups:
            for row in self.repo.list_backups(rec.id):
                self.backups.delete(row["id"], quiet=True)
        self.reporter.record("warning", "server", f"Server '{rec.name}' deleted", None)
        self.events.publish("servers", "deleted", {"id": rec.id}, audience=holders)

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
        if rec.is_java:
            for key in ("channel", "pinned_tag", "bedrock", "bedrock_port"):
                changes.pop(key, None)  # these belong to Pumpkin servers
            if "java" in changes:
                restart_required |= self.jvm.patch(rec, changes["java"])
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
        if rec.is_java:
            return self.jvm.read_config(rec)
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
        if rec.is_java:
            return self.jvm.update_config(rec, values)
        clean = pc.validate_values(values)
        path = self.config_path(rec.id)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        new_text = pc.apply_managed(pc.apply_values(text, clean), self.managed(rec))[0]
        self._write_config(path, text, new_text)
        return {"saved": True, "restart_required": self.pm.runtime(rec.id).is_active}

    def read_raw_config(self, rec: ServerRecord) -> dict[str, Any]:
        if rec.is_java:
            return self.jvm.read_raw_config(rec)
        path = self.config_path(rec.id)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {"content": pc.strip_secrets(text), "managed_keys": [".".join(p) for p, _ in self.managed(rec).items()]}

    def write_raw_config(self, rec: ServerRecord, content: str) -> dict[str, Any]:
        if rec.is_java:
            return self.jvm.write_raw_config(rec, content)
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

    async def save_settings(self, rec: ServerRecord, values: dict[str, Any]) -> dict[str, Any]:
        """Save game settings and, where the running server allows it, apply them without a restart."""
        result = self.update_config(rec, values)
        applied: list[str] = []
        if not rec.is_java and "default_difficulty" in values and self.pm.runtime(rec.id).state == "running":
            if await self._apply_difficulty(rec, force=True):
                applied.append("default_difficulty")
        result["applied_live"] = applied
        result["restart_required"] = result["restart_required"] and any(key not in applied for key in values)
        return result

    # --------------------------------------------------------------- difficulty
    def _config_text(self, rec: ServerRecord) -> str:
        path = self.config_path(rec.id)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _world_is_new(self, rec: ServerRecord) -> bool:
        """True while the server has no saved world, i.e. when the next start makes Pumpkin create one."""
        try:
            level_name = pc.read_values(self._config_text(rec))["default_level_name"]
        except pc.ConfigError:
            return False
        return not (self.server_dir(rec.id) / level_name / "level.dat").is_file()

    async def _apply_difficulty(
        self, rec: ServerRecord, ctx: JobContext | None = None, *, fresh_world: bool = False, force: bool = False
    ) -> bool:
        """Set the configured difficulty on the running server with the ``difficulty`` console command.

        Pumpkin does not read ``default_difficulty`` from pumpkin.toml, so without this every world would stay on Normal.
        The manager remembers the difficulty it applied last and only sends the command when the setting differs from
        that (or the world is brand new, or ``force`` is set): a difficulty changed in-game survives restarts.
        """
        try:
            wanted = pc.read_values(self._config_text(rec))["default_difficulty"]
        except pc.ConfigError:
            return False
        key = f"difficulty:{rec.id}"
        known = NEW_WORLD_DIFFICULTY if fresh_world else self.repo.get_kv(key)
        if known == wanted and not force:
            self.repo.set_kv(key, wanted)
            return False
        try:
            await self.pm.send_command(rec.id, f"difficulty {wanted.lower()}")  # the command only accepts lower case
        except ProcessError as exc:
            if ctx:
                ctx.warn(f"Could not set the difficulty to {wanted}: {exc}")
            return False
        self.repo.set_kv(key, wanted)
        if ctx:
            ctx.step(f"Difficulty set to {wanted} (Pumpkin does not read it from pumpkin.toml, so the manager sets it with a command)")
        return True

    # ------------------------------------------------------------------ players
    def _lists_dir(self, rec: ServerRecord) -> Path:
        return self.jvm.lists_dir(rec) if rec.is_java else self.server_dir(rec.id) / PLAYER_DATA_DIR

    def read_player_lists(self, rec: ServerRecord) -> dict[str, list[dict[str, Any]]]:
        # Unlike vanilla, Pumpkin keeps these files in a "data" folder inside the server directory and writes them as soon as
        # a command changes a list.
        directory = self._lists_dir(rec)
        result: dict[str, list[dict[str, Any]]] = {}
        for key, filename in PLAYER_LIST_FILES:
            entries: list[dict[str, Any]] = []
            try:
                data = json.loads((directory / filename).read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [self.jvm.normalize_entry(e) if rec.is_java else e for e in data if isinstance(e, dict)][:2000]
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

    # ---------------------------------------------------------- operator levels
    # Pumpkin's /op takes no level: it always uses ``op_permission_level`` from pumpkin.toml. A single operator's level lives
    # in data/ops.json, which the server reads only when it starts (editing the file while it runs has no effect and is
    # overwritten by the next /op or /deop). So changing a level on a running server means: stop, edit the file, start.
    def op_default_level(self, rec: ServerRecord) -> int:
        if rec.is_java:
            return self.jvm.op_default_level(rec)
        try:
            return int(pc.read_values(self._config_text(rec))["op_permission_level"])
        except (pc.ConfigError, TypeError, ValueError):
            return pc.DEFAULT_OP_LEVEL

    def _find_op(self, rec: ServerRecord, name: str) -> dict[str, Any] | None:
        wanted = name.lower()  # in online mode the server may have corrected the capitalisation
        return next((e for e in self.read_player_lists(rec)["ops"] if str(e.get("name", "")).lower() == wanted), None)

    def _write_op_level(self, rec: ServerRecord, name: str, level: int) -> None:
        path = self._lists_dir(rec) / "ops.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                raise ValueError("not a list")
        except (OSError, ValueError):
            raise ServerError("The operator list (data/ops.json) could not be read, so it was left untouched.") from None
        found = False
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
                entry["level"] = level
                found = True
        if not found:
            raise ServerError(f"{name} is not an operator.")
        atomic_write_text(path, json.dumps(entries, indent=2))

    async def _do_set_op_level(self, rec: ServerRecord, ctx: JobContext, name: str, level: int) -> None:
        rec = self.get(rec.id)
        state = self.pm.runtime(rec.id).state
        if state in ("starting", "stopping"):
            raise ServerError("The server is starting or stopping. Try again in a moment.")
        running = state == "running"
        entry = self._find_op(rec, name)
        if entry is None:
            if not running:
                raise ServerError(
                    f"{name} is not an operator yet. Start the server first: it has to look the player up before they can be made an operator."
                )
            ctx.step(f"Making {name} an operator")
            await self.command(rec, f"op {name}")
            deadline = time.monotonic() + 30  # an online-mode server asks Mojang for the account first
            while entry is None and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                entry = self._find_op(rec, name)
            if entry is None:
                raise ServerError(f"The server did not add {name} to its operator list. Its answer is in the console (is the name spelled right?).")
        name = str(entry.get("name") or name)
        if entry.get("level") == level:
            ctx.step(f"{name} is already operator level {level}")
            return
        if running:
            ctx.step("The server reads operator levels only when it starts, so it restarts now")
            await self._do_stop(rec, ctx)
            self._write_op_level(rec, name, level)
            await self._do_start(rec, ctx)
        else:
            self._write_op_level(rec, name, level)
        ctx.step(f"{name} is now operator level {level}")

    def submit_operator_level(self, rec: ServerRecord, name: str, level: int) -> dict[str, Any]:
        if not PLAYER_NAME_RE.match(name):
            raise ServerError("Player names may only contain letters, digits, '_', '-' and '.'.")
        if level not in pc.OP_LEVELS:
            raise ServerError("The operator level must be 1, 2, 3 or 4.")
        return self.jobs.submit(
            "operator", f"Set {name} to operator level {level}", rec.id, lambda ctx: self._do_set_op_level(rec, ctx, name, level)
        )

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
            if rec.is_java:
                raise MsmpError("Java servers have no management protocol here")
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
        for leftover in base.glob(".create-*"):  # a modpack install that was interrupted
            shutil.rmtree(leftover, ignore_errors=True)

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
