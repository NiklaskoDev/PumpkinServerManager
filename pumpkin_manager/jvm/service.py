"""Java servers: creating modpack servers and Vanilla, Paper and Fabric servers, starting them and managing their settings.

A Java server is an ordinary server of the manager (same console, backups, players, accounts) that runs a different
engine: the manager downloads the Java it needs, installs the modpack and its mod loader (or the server software itself),
and starts it with the right command line. ``ServerService`` hands over to this class wherever the two engines differ.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import string
import time
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..jobs import JobContext
from ..models import PROXY_KINDS, SOFTWARE_NAMES, ServerPolicy, ServerRecord, software_label
from ..pumpkin_config import ConfigError
from ..servers import ServerError
from ..util import atomic_write_text, human_bytes, iso
from . import gtnh, missingmods
from . import properties as props
from . import proxy as proxy_config
from .archive import ServerPack
from .curseforge import (
    RELEASE_TYPES,
    CurseForgeClient,
    SearchUnavailable,
    is_curseforge_pack,
    page_link,
    read_manifest,
    summarize_file,
    summary_of_upload,
    version_name,
)
from .gtnh import GtnhCatalog
from .loaders import LoaderInstaller, find_launch, launch_arguments
from .modrinth import ModrinthClient, parse_project_reference, summarize_version
from .mojang import MojangMeta, available_java
from .mrpack import LOCK_DIR, LOCK_NAME, PackError, PackIndex, PackInstaller, override_mods, read_pack
from .net import DownloadError, ProgressCallback
from .runtime import JavaRuntimes
from .software import PROXY_JAVA, Build, Options, SoftwareCatalog, SoftwareInstaller

if TYPE_CHECKING:
    from ..servers import ServerService

log = logging.getLogger(__name__)

READY_RE = re.compile(r"\bDone \([0-9.,]+s\)!")  # "Done (12.345s)! For help, type "help"": printed by vanilla and every loader
# What vanilla, Forge and NeoForge log when a server cannot start (a mod that fails to load, for instance). Forge and NeoForge can
# stay alive afterwards on threads of their own, so the manager acts on this line instead of waiting for the startup timeout.
FAIL_RE = re.compile(r"Failed to start the minecraft server")
# BungeeCord says it listens, not "Done". When its configuration cannot be used it logs the exception of its main thread, and then its
# Java process stays alive doing nothing (so the manager must act on the line, as it does for Forge).
PROXY_READY_RE = re.compile(r"\bListening on /")
PROXY_FAIL_RE = re.compile(r'Exception in thread "main"|Invalid configuration encountered')
PROXY_STOP_COMMAND = "end"  # BungeeCord has no "stop"
PROXY_MEMORY_MB = 512  # a proxy only passes packets on: this is enough for hundreds of players
# Forge up to Minecraft 1.12 stops at startup when the world has blocks or items that the mods no longer have, and waits until somebody
# types "/fml confirm" (they are removed, after a backup of the world that Forge makes) or "/fml cancel" into its console. Forge compares
# what is typed with exactly those words, slash included: "fml confirm" is not an answer, and the server keeps waiting.
FML_PROMPT_RE = re.compile(r"Run the command /fml confirm or")
FML_COUNT_RE = re.compile(r"There are (\d+) missing blocks and items")
FML_ANSWER = "/fml confirm"
MAX_CONFIRMED_MISSING = 1000  # an update of a pack removes some; a thousand or more says that the mods or the world are not what they should be
EULA_URL = "https://aka.ms/MinecraftEULA"
_SECRET_ALPHABET = string.ascii_letters + string.digits
MIN_MEMORY_MB, MAX_MEMORY_MB = 512, 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
# Modded servers start slowly, save slowly and write a lot of files the world does not need to be restored.
JAVA_POLICY_DEFAULTS: dict[str, Any] = {
    "start_timeout": 900, "stop_timeout": 180, "update_mode": "notify", "backup_exclude": ["logs", "crash-reports", "cache"],
}
SOFTWARE_MEMORY_MB = 2048  # what a server without a modpack starts with


# What these servers unpack or download by themselves when they start and find it missing: Vanilla extracts libraries/ and versions/
# from its jar, Paper rebuilds libraries/ and cache/, Fabric downloads libraries/ and .fabric/. A fresh server is two thirds of that
# (130-220 MB, with a world of 3-7 MB), so backups leave it out; the patterns with a slash only match at the top of the server folder.
REGENERATED = ["cache", "libraries/*", "versions/*"]


def software_policy_defaults(kind: str) -> dict[str, Any]:
    """Vanilla, Paper, Fabric and Spigot start and save faster than a modpack. Paper builds are small steps (plugins are made for them),
    so they are installed by themselves unless the person says otherwise; a new Minecraft version never is. A Spigot build takes a
    quarter of an hour to make, so there it is the person's call. A proxy starts in seconds and has no world."""
    if kind in PROXY_KINDS:  # BungeeCord downloads its libraries and modules when it starts and has a log of its own besides logs/
        return {
            **JAVA_POLICY_DEFAULTS, "start_timeout": 300, "stop_timeout": 60, "update_mode": "notify",
            "backup_exclude": ["logs", "proxy.log*", "libraries/*"],
        }
    # Spigot unpacks its libraries into bundler/ instead of libraries/
    excluded = ["logs", "crash-reports", *REGENERATED, *([".fabric/*"] if kind == "fabric" else []), *(["bundler/*"] if kind == "spigot" else [])]
    return {
        **JAVA_POLICY_DEFAULTS, "start_timeout": 600, "stop_timeout": 120, "update_mode": "auto" if kind == "paper" else "notify",
        "backup_exclude": excluded,
    }


def gtnh_policy_defaults() -> dict[str, Any]:
    """GT New Horizons starts slowly (hundreds of mods, a generated world), and a mod of the pack keeps copies of the world in
    ``backups/``: the manager's own backups leave that folder out."""
    return {**JAVA_POLICY_DEFAULTS, "start_timeout": gtnh.START_TIMEOUT, "backup_exclude": [*JAVA_POLICY_DEFAULTS["backup_exclude"], "backups/*"]}


def mods_text(count: int) -> str:
    return f"{count} mod" if count == 1 else f"{count} mods"


def client_only_info(index: PackIndex) -> dict[str, Any]:
    """What the server remembers about the mods that were left out because they only work on a client (for the dashboard)."""
    return {"client_only": sorted(index.client_only)[:400], "client_only_count": len(index.client_only)}


def manual_info(index: PackIndex) -> dict[str, Any]:
    """The mods that have to be added by hand (their authors do not allow downloading them automatically), for the dashboard."""
    return {"manual": [{"path": f.path, "name": f.name, "page": f.page} for f in index.manual][:300], "manual_count": len(index.manual)}


@dataclass
class NewServer:
    """What every new Java server has in common, checked before anything is downloaded."""

    name: str
    memory: int
    jvm_args: str
    values: dict[str, Any]
    policy: ServerPolicy


@dataclass
class OpenedPack:
    """A modpack that is ready to install: what it holds, what the server will remember about it, and the files to delete after."""

    index: PackIndex
    path: Path  # the pack zip (the manifest and the configuration folders)
    info: dict[str, Any]
    temp: list[Path]

    def cleanup(self) -> None:
        for path in self.temp:
            path.unlink(missing_ok=True)


def default_jvm_flags(java_major: int) -> list[str]:
    # No screen: mods that ask for a window get an error they can handle instead of looking for a display (the container has none).
    flags = ["-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", "-Djava.awt.headless=true"]
    if java_major <= 8:
        flags.append("-XX:+UseG1GC")  # Java 8 still defaults to the throughput collector
    return flags


@dataclass
class JavaLaunch:
    argv: list[str]
    env: dict[str, str]
    ready_re: re.Pattern[str]
    fail_re: re.Pattern[str]
    label: str
    prompt_re: re.Pattern[str] | None = None  # the console line that says the server waits for an answer
    on_prompt: Callable[[list[str]], Awaitable[str | None]] | None = None  # what to type then (None: nothing)
    stop_command: str = "stop"  # what ends it gracefully when typed into its console


class JavaServerService:
    def __init__(
        self, svc: ServerService, runtimes: JavaRuntimes, mojang: MojangMeta, modrinth: ModrinthClient, loaders: LoaderInstaller,
        packs: PackInstaller, uploads_dir: Path, curseforge: CurseForgeClient, software: SoftwareCatalog, installer: SoftwareInstaller,
        gtnh_catalog: GtnhCatalog,
    ):
        self.svc = svc
        self.runtimes = runtimes
        self.mojang = mojang
        self.modrinth = modrinth
        self.curseforge = curseforge
        self.gtnh = gtnh_catalog  # GT New Horizons' own download site
        self.loaders = loaders
        self.packs = packs
        self.software = software  # what Mojang, PaperMC and Fabric offer
        self.installer = installer  # puts that on disk
        self.uploads_dir = uploads_dir
        self._icons: dict[str, tuple[bytes, str]] = {}
        self.argv_wrapper: Callable[[list[str]], list[str]] | None = None  # a test seam, like ServerService.command_builder
        # Servers whose next start is the one of an update from the pack's own server files: the blocks the new version removed are
        # confirmed there, because they are meant to go (``update`` fills and empties this). What the servers were asked, by server id.
        self.confirm_missing: set[str] = set()
        self.prompts: dict[str, dict[str, Any]] = {}
        uploads_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _error(message: str) -> ServerError:
        return ServerError(message)

    def properties_path(self, rec_or_id: ServerRecord | str) -> Path:
        """The file a server's settings live in: server.properties, or config.yml for a proxy."""
        server_id = rec_or_id if isinstance(rec_or_id, str) else rec_or_id.id
        name = proxy_config.FILE_NAME if isinstance(rec_or_id, ServerRecord) and rec_or_id.is_proxy else props.FILE_NAME
        return self.svc.server_dir(server_id) / name

    def _read(self, rec: ServerRecord) -> str:
        path = self.properties_path(rec)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def parse_memory(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not MIN_MEMORY_MB <= value <= MAX_MEMORY_MB:
            raise ConfigError({"memory_mb": f"must be a whole number of MiB between {MIN_MEMORY_MB} and {MAX_MEMORY_MB}"})
        return value

    @staticmethod
    def parse_jvm_args(text: Any) -> str:
        """Extra JVM options, one line. Only options (``-X...``, ``-D...``) are accepted: nothing that picks another program or file."""
        if text in (None, ""):
            return ""
        if not isinstance(text, str) or len(text) > 2000 or "\n" in text or "\r" in text or "\x00" in text:
            raise ConfigError({"jvm_args": "must be one line of at most 2000 characters"})
        try:
            tokens = shlex.split(text)
        except ValueError as exc:
            raise ConfigError({"jvm_args": f"cannot be read: {exc}"}) from None
        for token in tokens:
            if not token.startswith("-") or token.lower() in ("-jar", "-cp", "-classpath", "--class-path", "-p", "--module-path"):
                raise ConfigError({"jvm_args": f"'{token}' is not allowed: only JVM options such as -Xss2M or -Dname=value can be added"})
        return " ".join(shlex.quote(t) for t in tokens)

    # ------------------------------------------------------------------- views
    def describe(self, rec: ServerRecord) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """(modpack, java) parts of the server view: what it runs, and how it is run. A server that runs Vanilla, Paper or Fabric
        has no modpack (see ``describe_software``)."""
        spec = rec.spec
        installed = self.runtimes.get(int(spec.get("java") or 0))
        java = {
            "major": spec.get("java"), "memory_mb": spec.get("memory_mb"), "jvm_args": spec.get("jvm_args", ""),
            "runtime": installed.release if installed else None,
        }
        if spec.get("software"):
            return None, java
        pack = spec.get("pack") or {}
        loader = spec.get("loader") or {}
        label = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}.get(loader.get("kind", ""), loader.get("kind", ""))
        modpack = {
            "source": pack.get("source"), "title": pack.get("title"), "slug": pack.get("slug"), "icon": pack.get("icon"),
            "version_number": pack.get("version_number"), "pending": pack.get("pending"), "can_roll_back": bool(spec.get("previous")),
            "client_only": pack.get("client_only", []), "client_only_count": pack.get("client_only_count", 0),
            "leave_out_client_only": spec.get("leave_out_client_only", True), "minecraft": spec.get("minecraft"),
            "server_pack": bool(pack.get("server_pack")), "manual": self._missing_manual(rec, pack),
            "loader": {"kind": loader.get("kind"), "version": loader.get("version"), "label": f"{label} {loader.get('version', '')}".strip()},
            "variant": pack.get("variant"),
            # a server made from a page that only lists a pack (GT New Horizons on CurseForge) never starts: say why, and what to do
            "notice": gtnh.REFERENCE_ONLY.get(str(pack.get("project"))) if pack.get("source") == "curseforge" else None,
        }
        return modpack, java

    def describe_software(self, rec: ServerRecord) -> dict[str, Any] | None:
        """What a Vanilla, Paper, Fabric, Spigot or BungeeCord server runs, for the server view (None for the other kinds of server)."""
        software = rec.spec.get("software")
        if not software:
            return None
        kind = str(software.get("kind") or "")
        api = software.get("fabric_api")
        return {
            "kind": kind, "name": SOFTWARE_NAMES.get(kind, kind), "proxy": kind in PROXY_KINDS, "minecraft": software.get("minecraft") or None,
            "build": software.get("build") or None,
            "channel": software.get("channel"), "label": software_label(software), "installed_at": software.get("installed_at"),
            "support": software.get("support"), "pending": software.get("pending"), "upgrade": software.get("upgrade"),
            "can_roll_back": bool(rec.spec.get("previous")), "fabric_api": {"number": api.get("number")} if api else None,
        }

    def _missing_manual(self, rec: ServerRecord, pack: dict[str, Any]) -> list[dict[str, Any]]:
        """The mods that have to be added by hand and are not in the server's mods folder yet."""
        wanted = pack.get("manual") or []
        if not wanted:
            return []
        directory = self.svc.server_dir(rec.id)
        return [m for m in wanted if not (directory / str(m.get("path", "-"))).is_file()]

    # ------------------------------------------------------------------ create
    def _check_new(self, spec: dict[str, Any], *, memory: int, defaults: dict[str, Any], proxy: bool = False) -> NewServer:
        """Check what every new Java server needs (name, EULA, memory, options, settings, policy) before anything is downloaded. A proxy
        needs no EULA (it runs no Minecraft server) and its settings are those of its config.yml."""
        svc = self.svc
        name = " ".join(str(spec["name"]).split())
        if not 1 <= len(name) <= 40:
            raise self._error("The server name must be 1-40 characters long.")
        if any(r.name.lower() == name.lower() for r in svc.repo.list_servers()):
            raise self._error("A server with that name already exists.")
        if not proxy and spec.get("eula") is not True:
            raise self._error(f"Running a Minecraft server means accepting the Minecraft EULA ({EULA_URL}). Tick the box to confirm.")
        try:
            wanted = self.parse_memory(memory if spec.get("memory_mb") is None else spec["memory_mb"])  # the API sends None when it was left out
            jvm_args = self.parse_jvm_args(spec.get("jvm_args"))
            if proxy:
                values = proxy_config.validate_values(spec.get("settings") or {})
            else:
                values = props.validate_values({"op-permission-level": props.DEFAULT_OP_LEVEL, **(spec.get("settings") or {})})
            policy = ServerPolicy.model_validate({**defaults, **(spec.get("policy") or {})})
        except ConfigError as exc:
            raise self._error(f"Invalid settings: {exc}") from exc
        except ValueError as exc:
            raise self._error(f"Invalid policy: {exc}") from exc
        return NewServer(name, wanted, jvm_args, values, policy)

    async def create(self, spec: dict[str, Any], ctx: JobContext) -> ServerRecord:
        software = spec.get("software")
        if software is not None:
            if not isinstance(software, dict):
                raise self._error("The software to install is not valid.")
            kind = SoftwareCatalog.check_kind(software.get("kind"))
            proxy = kind in PROXY_KINDS
            new = self._check_new(
                spec, memory=PROXY_MEMORY_MB if proxy else SOFTWARE_MEMORY_MB, defaults=software_policy_defaults(kind), proxy=proxy,
            )
            return await self._create_software(new, kind, software, spec, ctx)
        if (spec.get("modpack") or {}).get("source") == gtnh.PROJECT:
            return await self._create_modpack(self._check_new(spec, memory=gtnh.MEMORY_MB, defaults=gtnh_policy_defaults()), spec, ctx)
        return await self._create_modpack(self._check_new(spec, memory=4096, defaults=JAVA_POLICY_DEFAULTS), spec, ctx)

    async def _register(self, new: NewServer, staging: Path, record_spec: dict[str, Any], port: Any) -> tuple[ServerRecord, int]:
        """Give a new server the files every server gets, move its folder into place and record it. Returns it and its game port."""
        svc = self.svc
        proxy = (record_spec.get("software") or {}).get("kind") in PROXY_KINDS  # no EULA and no server.properties: config.yml instead
        text = ""
        if not proxy:
            self._write_eula(staging)
            existing = staging / props.FILE_NAME
            text = props.apply(existing.read_text(encoding="utf-8") if existing.is_file() else "", new.values)
        async with svc._create_lock:
            if any(r.name.lower() == new.name.lower() for r in svc.repo.list_servers()):  # another creation may have finished meanwhile
                raise self._error("A server with that name already exists.")
            server_id = svc._unique_id(new.name)
            java_port = svc.allocate_port("java", port)
            mgmt_port = svc.allocate_port("mgmt")
            directory = svc.server_dir(server_id)
            rec = ServerRecord(
                id=server_id, name=new.name, created_at=iso(), channel="", pinned_tag=None, build_id=None, previous_build_id=None,
                java_port=java_port, bedrock_port=None, mgmt_port=mgmt_port,
                mgmt_secret="".join(secrets.choice(_SECRET_ALPHABET) for _ in range(40)), policy=new.policy, desired_running=False,
                last_backup_at=iso(), last_update_check_at=None, pending_build_id=None, pending_since=None, engine="java", spec=record_spec,
            )
            try:
                os.replace(staging, directory)
                if proxy:
                    atomic_write_text(directory / proxy_config.FILE_NAME, self.initial_proxy_config(java_port, new.values))
                else:
                    atomic_write_text(directory / props.FILE_NAME, props.apply_managed(text, java_port)[0])
                svc.repo.insert_server(rec)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        return rec, java_port

    def initial_proxy_config(self, port: int, values: dict[str, Any]) -> str:
        """The config.yml of a proxy: the servers the manager runs are the ones it sends players to (they are all on this machine)."""
        backends = sorted((r for r in self.svc.repo.list_servers() if not r.is_proxy), key=lambda r: (r.created_at, r.name.lower()))
        servers = [{"name": proxy_config.server_name(r.id), "address": f"127.0.0.1:{r.java_port}", "motd": r.name} for r in backends]  # the oldest first
        return proxy_config.build_initial(
            port, values.get("motd", proxy_config.DEFAULT_MOTD), values.get("max_players", proxy_config.DEFAULT_MAX_PLAYERS), servers,
        )

    def _announce_created(self, rec: ServerRecord, java_port: int, what: str, ctx: JobContext) -> None:
        ctx.claim(rec.id)
        ctx.step(f"Server '{rec.name}' created on port {java_port}")
        self.svc.reporter.record("success", "server", f"Server '{rec.name}' created ({what}, port {java_port})", rec.id)
        self.svc.publish_state(rec.id)

    async def _create_modpack(self, new: NewServer, spec: dict[str, Any], ctx: JobContext) -> ServerRecord:
        svc = self.svc
        modpack = spec.get("modpack") or {}
        staging = svc.settings.servers_dir / f".create-{uuid.uuid4().hex}"
        opened: OpenedPack | None = None
        try:
            ctx.step("Looking up the modpack")
            leave_out = spec.get("leave_out_client_only") is not False
            opened = await self.open_pack(modpack, ctx, leave_out=leave_out)
            index, pack_path, pack_info = opened.index, opened.path, opened.info
            overview = index.summary_public()
            ctx.step(f"{index.name} {index.version_id}: Minecraft {index.minecraft}, {index.loader.label}, {mods_text(overview['mods'])}")
            for reason, count in index.archive_notes.items():
                ctx.step(f"Not taken over from the pack's server files: {count} file{'' if count == 1 else 's'} ({reason})")
            if index.manual:
                few = len(index.manual) == 1
                ctx.warn(
                    f"{mods_text(len(index.manual))} cannot be downloaded automatically: "
                    f"{'its author does not allow it' if few else 'their authors do not allow it'}. {'It is' if few else 'They are'} listed "
                    f"in the Modpack tab with a link{'' if few else ' to each'}; add {'it' if few else 'them'} to the mods folder yourself."
                )

            bundled = pack_info.get("source") == gtnh.PROJECT  # the pack's server files bring their own loader
            launch = gtnh.launch_of(index.archive, pack_info["variant"]) if bundled else None  # fails here, before anything is installed
            major = await self.java_for_pack(index, pack_info)
            ctx.step(f"{index.name} runs on Java {major}" if bundled else f"Minecraft {index.minecraft} needs Java {major}")
            java = await self.runtimes.ensure(major, self._java_progress(ctx, major))

            staging.mkdir(parents=True)
            ctx.step(f"Installing {mods_text(overview['mods'])}")
            await self.packs.install(index, pack_path, staging, lambda fraction, message: ctx.progress(0.25 + 0.5 * fraction, message))
            if bundled:
                if gtnh.switch_off_own_backups(staging):
                    ctx.step("Switched off the backups of ServerUtilities, a mod of the pack: the manager looks after the backups")
            else:
                ctx.step(f"Installing {index.loader.label}")
                launch = await self.loaders.install(index.loader, staging, java.java, lambda message: ctx.progress(None, message))
            ctx.progress(0.9, "Creating the server")
            rec, java_port = await self._register(new, staging, {
                "kind": "modpack", "minecraft": index.minecraft, "loader": {"kind": index.loader.kind, "version": index.loader.version},
                "java": major, "memory_mb": new.memory, "jvm_args": new.jvm_args, "launch": launch, "pack": pack_info,
                "leave_out_client_only": leave_out, "eula_accepted_at": iso(),
            }, spec.get("java_port"))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if opened is not None:
                opened.cleanup()
        if modpack.get("source") == "upload":
            self._forget_upload(modpack.get("upload"))
        self._announce_created(rec, java_port, index.name, ctx)
        if spec.get("start", True) and index.manual:
            ctx.step("The server is not started yet: add the mods listed in the Modpack tab first, then start it")
        elif spec.get("start", True):
            ctx.step("Starting the server for the first time (the world is generated now, which takes a while)")
            await svc._do_start(rec, ctx)
        return rec

    async def _create_software(self, new: NewServer, kind: str, request: dict[str, Any], spec: dict[str, Any], ctx: JobContext) -> ServerRecord:
        """Vanilla, Paper, Fabric, Spigot or BungeeCord: nothing to unpack, so this is mostly one download (Spigot: one build) and a check
        of it."""
        svc = self.svc
        proxy = kind in PROXY_KINDS
        staging = svc.settings.servers_dir / f".create-{uuid.uuid4().hex}"
        try:
            ctx.step(f"Looking up {SOFTWARE_NAMES[kind]}")
            build, options = await self.software.resolve(kind, request.get("minecraft") or None, request.get("build") or None)
            ctx.step(f"Using {build.label}")
            if kind == "paper" and not build.stable:
                ctx.warn(
                    f"Paper has no stable build for Minecraft {build.minecraft} yet. Build {build.build} is an experimental one ({build.channel}); "
                    "PaperMC advises against running those on a server that matters. Choose an older Minecraft version for a stable build."
                )
            if (options.support or {}).get("status") == "UNSUPPORTED":
                end = (options.support or {}).get("end")
                ctx.warn(f"PaperMC no longer maintains Minecraft {build.minecraft}{f' (since {end})' if end else ''}: it still runs, but gets no more fixes.")
            major = await self.java_for_software(build, options)
            ctx.step(f"{software_label({'kind': kind})} runs on Java {major}" if proxy else f"Minecraft {build.minecraft} needs Java {major}")
            java = await self.runtimes.ensure(major, self._java_progress(ctx, major))

            staging.mkdir(parents=True)
            launch = await self.installer.fetch(
                build, staging, java.java, lambda message: ctx.progress(None, message), self.fetch_progress(ctx, build, 0.25, 0.5),
                java_major=major, jdk_progress=self._java_progress(ctx, major, kit=True),
            )
            fabric_api = None
            if kind == "fabric" and request.get("fabric_api") is not False:
                api = await self.installer.fabric_api(build.minecraft)
                if api is None:
                    ctx.warn(
                        f"Fabric API has no version for Minecraft {build.minecraft} yet. Most mods need it: add it to the mods folder yourself "
                        "once it is published."
                    )
                else:
                    ctx.step(f"Installing Fabric API {api.number}")
                    fabric_api = await self.installer.install_fabric_api(api, staging)
            jvm_args = new.jvm_args
            if not jvm_args and options.flags and request.get("recommended_flags") is not False:
                jvm_args = self.parse_jvm_args(" ".join(options.flags))
                ctx.step("Using the Java options PaperMC recommends for this version")
            ctx.progress(0.9, "Creating the server")
            software: dict[str, Any] = {
                "kind": kind, "minecraft": build.minecraft, "build": build.build, "channel": build.channel, "installed_at": iso(),
                "support": options.support,
            }
            if fabric_api:
                software["fabric_api"] = fabric_api
            record_spec = {
                "kind": "software", "minecraft": build.minecraft, "software": software, "java": major, "memory_mb": new.memory,
                "jvm_args": jvm_args, "launch": launch,
            }
            if not proxy:
                record_spec["eula_accepted_at"] = iso()
            rec, java_port = await self._register(new, staging, record_spec, spec.get("java_port"))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._announce_created(rec, java_port, build.label, ctx)
        if spec.get("start", True):
            ctx.step(
                "Starting the proxy for the first time (it downloads its libraries and modules now)" if proxy
                else "Starting the server for the first time (the world is generated now, which takes a while)"
            )
            await svc._do_start(rec, ctx)
        return rec

    async def leave_out_client_only(self, index: PackIndex, ctx: JobContext, pack_path: Path | None = None) -> None:
        """Drop the mods that only work on a client, although the pack lists them for the server.

        Packs exported from a client launcher mark nearly every file "required" on the server, client-only mods (minimaps, shaders,
        menus, ...) included, and many of those stop a server from starting. Modrinth knows for every mod whether it can run on a
        server (looked up by the file's checksum, also for packs from CurseForge, whose files are mostly on both), and CurseForge
        tags files for clients. Mods that the pack ships inside its override folders are looked up too, and a mod that another
        mod needs stays, whatever its own project says about servers (a client-only library that a server mod depends on is
        still needed there). A pack's own server files are left as they are: the author already chose them."""
        if index.archive is not None:
            return
        candidates = [f for f in index.server_files if f.sha1]
        shipped = await asyncio.to_thread(override_mods, pack_path, index.override_prefixes) if pack_path is not None else {}
        hashes = [f.sha1 for f in candidates] + list(shipped.values())
        if not hashes:
            return
        ctx.step("Asking Modrinth which mods only work on a client")
        try:
            support = await self.modrinth.server_support(hashes)
        except DownloadError as exc:
            if index.source == "modrinth":
                raise self._error(f"Could not ask Modrinth which mods are client-only: {exc}") from exc
            ctx.warn(f"Modrinth could not be asked which mods are client-only ({exc}); only CurseForge's own tags are used")
            support = {}
        # One entry per file: the names the sources know it by, the ones it needs, and whether it is meant for clients only.
        facts: dict[str, tuple[set[str], set[str], bool]] = {}

        def describe(key: str, sha1: str, project: str = "", requires: tuple[str, ...] = (), hint: bool = False) -> None:
            known = support.get(sha1) or {}
            idents = {project} if project else set()
            needs = set(requires)
            if known:
                idents.add("mr:" + known["project"])
                needs |= {"mr:" + r for r in known["requires"]}
            facts[key] = (idents, needs, hint or known.get("side") == "unsupported")

        for f in candidates:
            describe(f.path, f.sha1, f.project, f.requires, f.client_hint)
        for path, sha1 in shipped.items():
            describe("override:" + path, sha1)
        client_only = {key for key, (_, _, client) in facts.items() if client}
        needed = {need for key, (_, needs, client) in facts.items() if not client for need in needs}
        kept: set[str] = set()
        grew = True
        while grew:  # something that stays can need a client-only library, which can need another one
            grew = False
            for key in client_only - kept:
                if facts[key][0] & needed:
                    kept.add(key)
                    needed |= facts[key][1]
                    grew = True
        out = client_only - kept
        paths = [f.path for f in candidates if f.path in out]
        shipped_paths = [path for path in shipped if "override:" + path in out]
        index.leave_out(paths)
        index.leave_out_overrides(shipped_paths)
        count = len(paths) + len(shipped_paths)
        if count:
            ctx.step(f"Leaving out {mods_text(count)} that only work{'s' if count == 1 else ''} on a client")
        if kept:
            ctx.step(f"Keeping {mods_text(len(kept))} that only work{'s' if len(kept) == 1 else ''} on a client, because other mods in the pack need {'it' if len(kept) == 1 else 'them'}")

    def _java_progress(self, ctx: JobContext, major: int, kit: bool = False) -> Callable[[int, int], None]:
        """What to tell a job while Java is downloaded (``kit``: the development kit that Spigot is built with)."""
        name = f"Java {major}{' development kit' if kit else ''}"

        def callback(done: int, total: int) -> None:
            ctx.progress(0.05 + 0.15 * (done / total if total else 0), f"Downloading {name} ({done // 1048576} of {total // 1048576} MiB)")

        return callback

    async def java_for_software(self, build: Build, options: Options) -> int:
        """The Java a build of server software runs on: what its Minecraft version needs and what its maker says (a proxy has no
        Minecraft version: it runs on the Java the manager uses for such things, if its maker allows)."""
        if build.kind in PROXY_KINDS:
            return available_java(max(PROXY_JAVA, options.java_minimum or 0))
        return available_java(max(await self.mojang.java_major(build.minecraft), options.java_minimum or 0))

    def fetch_progress(self, ctx: JobContext, build: Build, base: float, span: float) -> Callable[[int, int], None]:
        """What to tell a job while a build is fetched: how much of a download is in, or (Spigot, which is built) how far the build is."""
        if build.kind == "spigot":
            return lambda done, total: ctx.progress(base + span * (done / total if total else 0))  # the builder words what it is doing
        return lambda done, total: ctx.progress(
            base + span * (done / total if total else 0), f"Downloading {build.label} ({done // 1048576} of {total // 1048576} MiB)"
        )

    # ------------------------------------------------------------------ opening a pack
    async def open_pack(self, modpack: dict[str, Any], ctx: JobContext, *, leave_out: bool = True) -> OpenedPack:
        """Fetch and read the modpack a new server is made from (Modrinth, CurseForge or an uploaded file)."""
        source = modpack.get("source")
        if source == gtnh.PROJECT:
            opened = await self._download_gtnh(modpack, ctx)
        elif source == "curseforge":
            try:
                project = await self.curseforge.project(str(modpack.get("project", "")))
                files = await self.curseforge.files(project["id"])
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
            if str(project["id"]) in gtnh.REFERENCE_ONLY:
                raise self._error(gtnh.REFERENCE_ONLY[str(project["id"])])
            if not files:
                raise self._error(f"'{project.get('name')}' has no versions that can be installed.")
            wanted = modpack.get("version")
            if wanted:
                file = next((f for f in files if str(f["id"]) == str(wanted)), None)
                if file is None:
                    raise self._error("That version of the modpack does not exist (any more).")
            else:
                file = next((f for f in files if f.get("releaseType") == 1), files[0])
            opened = await self._download_curseforge(project, file, ctx, want_server_pack=modpack.get("server_pack") is not False)
        elif source == "upload":
            path = self._upload_path(modpack.get("upload"))
            if is_curseforge_pack(path):
                manifest = read_manifest(path)
                info = {"source": "upload", "title": manifest.name, "version_number": manifest.version, "format": "curseforge"}
                try:
                    index = await self.curseforge.build_index(manifest, info, server_pack=None, say=ctx.step)
                except DownloadError as exc:
                    raise self._error(str(exc)) from exc
                opened = OpenedPack(index, path, info, [])
            else:
                index = read_pack(path)
                opened = OpenedPack(index, path, {"source": "upload", "title": index.name, "version_number": index.version_id}, [])
        elif source == "modrinth":
            opened = await self._download_modrinth(modpack, ctx)
        else:
            raise self._error("Choose a modpack: search Modrinth or CurseForge, paste a link, or upload a pack file.")
        try:
            await self._wanted_mods(opened, ctx)
            if leave_out:
                await self.leave_out_client_only(opened.index, ctx, opened.path)
        except BaseException:
            opened.cleanup()
            raise
        opened.info = {**opened.info, **client_only_info(opened.index), **manual_info(opened.index)}
        return opened

    async def _download_gtnh(
        self, modpack: dict[str, Any], ctx: JobContext, *, version: str | None = None, variant: str | None = None
    ) -> OpenedPack:
        """Download one build of GT New Horizons' own server files (the newest stable version unless one is named) and read them."""
        try:
            release = await self.gtnh.release(version or modpack.get("version") or None)
            chosen = gtnh.pick_variant(release, variant or modpack.get("variant") or None)
            ctx.step(f"Downloading {gtnh.TITLE} {release.version} (the server files for {gtnh.VARIANT_NAMES[chosen]})")
            path = self.uploads_dir / f"{uuid.uuid4().hex}.zip"
            try:
                await self.gtnh.download(
                    release, chosen, path,
                    lambda done, total: ctx.progress(0.05 + 0.15 * (done / total if total else 0), f"Downloading the server files ({done // 1048576} of {total // 1048576} MiB)"),
                )
                pack = await asyncio.to_thread(ServerPack, path, whole=True)
                if not pack.usable:
                    raise PackError("The server files hold no mods, so they cannot be what GT New Horizons is supposed to be.")
                index = gtnh.build_index(release, pack)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        return OpenedPack(index, path, gtnh.pack_info(release, chosen), [path])

    async def java_for_pack(self, index: PackIndex, info: dict[str, Any]) -> int:
        """The Java a pack runs on: what its Minecraft version asks for, or, for GT New Horizons, what its build is made for."""
        if info.get("source") == gtnh.PROJECT:
            return gtnh.java_for(info.get("variant"), info.get("java_max"))
        return available_java(await self.mojang.java_major(index.minecraft))

    async def _wanted_mods(self, opened: OpenedPack, ctx: JobContext) -> None:
        """Packs that ship the Missing Mods Checker (Better MC): install the mods its list asks for, and keep that list off the server,
        where the mod would try to open a window and end the server (see ``missingmods``)."""
        index = opened.index
        if index.archive is not None:  # the author's server files hold the mods
            return
        try:
            wanted = await asyncio.to_thread(missingmods.read_wanted, opened.path, index.override_prefixes)
            if wanted is None:
                return
            index.skip_overrides.add(missingmods.CONFIG)
            present = {path.lower() for path in await asyncio.to_thread(override_mods, opened.path, index.override_prefixes)}
            await missingmods.add_wanted(index, wanted, present, self.curseforge, ctx.step, ctx.warn)
        except PackError as exc:
            index.skip_overrides.add(missingmods.CONFIG)
            ctx.warn(f"{exc} The list is ignored, so mods that the Missing Mods Checker would have asked for may be missing.")

    async def pack_versions(self, source: str, project: str, title: str = "") -> list[dict[str, Any]]:
        """The versions of a modpack, newest first, in the shape the dashboard shows them."""
        if source == gtnh.PROJECT:
            return [r.public() for r in await self.gtnh.releases()]
        if source == "curseforge":
            return [summarize_file(f, title) for f in await self.curseforge.files(project)]
        return [summarize_version(v) for v in await self.modrinth.versions(project)]

    async def open_version(
        self, rec: ServerRecord, version_id: str, ctx: JobContext, announce: Callable[[str], None] | None = None
    ) -> OpenedPack:
        """Fetch and read another version of the modpack a server was made from (for an update). ``announce`` hears the version's
        name as soon as it is known, before the download starts."""
        pack = rec.spec.get("pack") or {}
        if pack.get("source") == gtnh.PROJECT:
            try:
                release = await self.gtnh.release(version_id)
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
            if announce:
                announce(release.version)
            opened = await self._download_gtnh({}, ctx, version=release.version, variant=pack.get("variant") or "modern")
        elif pack.get("source") == "curseforge":
            try:
                project = await self.curseforge.project(str(pack.get("project")))
                file = await self.curseforge.file(project["id"], version_id)
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
            if file.get("isServerPack") or file.get("modId") != project["id"]:
                raise self._error("That version belongs to another modpack.")
            if announce:
                announce(version_name(str(project.get("name") or ""), file.get("displayName")) or version_id)
            from_server_pack = bool(pack.get("server_pack"))
            opened = await self._download_curseforge(project, file, ctx, want_server_pack=from_server_pack, require_server_pack=from_server_pack)
        else:
            try:
                version = await self.modrinth.version(version_id)
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
            if version.get("project_id") != pack.get("project"):
                raise self._error("That version belongs to another modpack.")
            if announce:
                announce(str(version.get("version_number") or version_id))
            opened = await self._download_modrinth({"project": pack.get("project"), "version": version_id}, ctx, version=version)
        try:
            await self._wanted_mods(opened, ctx)
            if rec.spec.get("leave_out_client_only", True):
                await self.leave_out_client_only(opened.index, ctx, opened.path)
        except BaseException:
            opened.cleanup()
            raise
        opened.info = {**opened.info, **client_only_info(opened.index), **manual_info(opened.index)}
        return opened

    async def _download_modrinth(self, modpack: dict[str, Any], ctx: JobContext, version: dict[str, Any] | None = None) -> OpenedPack:
        try:
            project: dict[str, Any] = {}
            if version is None:
                reference = parse_project_reference(str(modpack.get("project", "")))
                project = await self.modrinth.project(reference)
                versions = await self.modrinth.versions(project["id"])
                if not versions:
                    raise self._error(f"'{project['title']}' has no versions that can be installed.")
                wanted = modpack.get("version")
                if wanted:
                    version = next((v for v in versions if v["id"] == wanted), None)
                    if version is None:
                        raise self._error("That version of the modpack does not exist (any more).")
                else:
                    version = next((v for v in versions if v.get("version_type") == "release"), versions[0])
            else:
                project = await self.modrinth.project(version["project_id"])
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        ctx.step(f"Downloading {project['title']} {version.get('version_number')}")
        path = self.uploads_dir / f"{uuid.uuid4().hex}.mrpack"
        try:
            await self.modrinth.download_pack(
                version, path, lambda done, total: ctx.progress(0.2 + 0.05 * (done / total if total else 0), "Downloading the modpack file")
            )
            index = read_pack(path)
        except DownloadError as exc:
            path.unlink(missing_ok=True)
            raise self._error(str(exc)) from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        info = {
            "source": "modrinth", "project": project["id"], "slug": project.get("slug"), "title": project["title"],
            "icon": project.get("icon_url"), "version_id": version["id"], "version_number": version.get("version_number"),
            "published": version.get("date_published"), "version_type": version.get("version_type", "release"),
        }
        return OpenedPack(index, path, info, [path])

    async def _download_curseforge(
        self, project: dict[str, Any], file: dict[str, Any], ctx: JobContext, *, want_server_pack: bool, require_server_pack: bool = False,
    ) -> OpenedPack:
        """Download one version of a CurseForge modpack (and the author's server pack for it, when wanted and there is one)."""
        cf = self.curseforge
        title = f"{project.get('name')} {file.get('displayName') or ''}".strip()
        temp: list[Path] = []

        def shown(what: str) -> ProgressCallback:
            return lambda done, total: ctx.progress(0.2 + 0.05 * (done / total if total else 0), f"Downloading {what} ({done // 1048576} of {total // 1048576} MiB)")

        try:
            ctx.step(f"Downloading {title}")
            pack_path = self.uploads_dir / f"{uuid.uuid4().hex}.zip"
            temp.append(pack_path)
            await cf.download(file, pack_path, what=title, progress=shown("the modpack file"))
            manifest = read_manifest(pack_path)
            server_path: Path | None = None
            server_id = file.get("serverPackFileId")
            if want_server_pack and server_id:
                server_file = await cf.file(project["id"], server_id)
                if not server_file.get("downloadUrl"):
                    if require_server_pack:
                        raise self._error("The author does not allow this version's server files to be downloaded automatically.")
                else:
                    ctx.step(f"Downloading the pack's own server files ({human_bytes(server_file.get('fileLength') or 0)})")
                    server_path = self.uploads_dir / f"{uuid.uuid4().hex}.zip"
                    temp.append(server_path)
                    await cf.download(server_file, server_path, what="the server files", progress=shown("the server files"))
            elif require_server_pack:
                raise self._error("This version has no server files of its own, but the server was installed from such files.")
            info = {
                "source": "curseforge", "project": str(project["id"]), "slug": project.get("slug"), "title": project.get("name"),
                "icon": (project.get("logo") or {}).get("thumbnailUrl"), "version_id": str(file["id"]),
                "version_number": version_name(str(project.get("name") or ""), file.get("displayName")) or manifest.version,
                "published": file.get("fileDate"),
                "version_type": RELEASE_TYPES.get(file.get("releaseType"), "release"),
            }
            index = await cf.build_index(manifest, info, server_pack=server_path, say=ctx.step)
            if require_server_pack and index.archive is None:
                raise self._error("This version's server files hold no mods, but the server was installed from server files with mods.")
            info["server_pack"] = index.archive is not None
            return OpenedPack(index, pack_path, info, temp)
        except DownloadError as exc:
            for path in temp:
                path.unlink(missing_ok=True)
            raise self._error(str(exc)) from exc
        except BaseException:
            for path in temp:
                path.unlink(missing_ok=True)
            raise

    # ----------------------------------------------------------------- uploads
    def _upload_path(self, upload_id: Any) -> Path:
        if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise self._error("That upload is not known (any more). Upload the modpack file again.")
        path = self.uploads_dir / f"{upload_id}.mrpack"
        if not path.is_file():
            raise self._error("That upload is not known (any more). Upload the modpack file again.")
        return path

    def _forget_upload(self, upload_id: Any) -> None:
        with contextlib.suppress(Exception):
            self._upload_path(upload_id).unlink(missing_ok=True)

    async def accept_upload(self, chunks: AsyncIterable[bytes]) -> dict[str, Any]:
        """Store an uploaded .mrpack (the request body as it arrives), check it and describe it."""
        upload_id = uuid.uuid4().hex
        path = self.uploads_dir / f"{upload_id}.mrpack"
        size = 0
        try:
            with open(path, "wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise self._error("That file is too large for a modpack (limit 1 GiB).")
                    await asyncio.to_thread(out.write, chunk)
            summary = summary_of_upload(path) if is_curseforge_pack(path) else read_pack(path).summary_public()
            return {"upload": upload_id, "pack": summary}
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def purge_old_uploads(self, max_age_seconds: float = 24 * 3600) -> int:
        removed = 0
        for path in self.uploads_dir.glob("*.mrpack"):
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > max_age_seconds:
                    path.unlink()
                    removed += 1
        return removed

    # ------------------------------------------------------------------ browsing
    async def pack_icon(self, url: str) -> tuple[bytes, str]:
        if url in self._icons:
            return self._icons[url]
        try:
            icon = await (self.curseforge if (urlsplit(url).hostname or "") == "media.forgecdn.net" else self.modrinth).fetch_image(url)
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        if len(self._icons) >= 300:
            self._icons.pop(next(iter(self._icons)))
        self._icons[url] = icon
        return icon

    def sources(self) -> dict[str, Any]:
        """Where modpacks can come from, and whether each source is ready to use."""
        return {"modrinth": {"enabled": True}, "curseforge": {"enabled": self.curseforge.enabled, "search": self.curseforge.search_allowed}}

    async def search_packs(self, query: str, offset: int = 0, source: str = "modrinth") -> dict[str, Any]:
        if source == "curseforge":
            try:
                return {**await self.curseforge.search(query, offset=offset), "search": True}
            except SearchUnavailable as exc:
                # The key may not search: show what CurseForge itself lists, and let the person add a pack by its project number.
                try:
                    hits = await self.curseforge.featured()
                except DownloadError as inner:
                    raise self._error(str(inner)) from inner
                needle = query.strip().lower()
                shown = [h for h in hits if needle in (h["title"] or "").lower()] if needle else hits
                return {"hits": shown, "total": len(shown), "search": False, "note": str(exc)}
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
        try:
            return await self.modrinth.search(query, offset=offset)
        except DownloadError as exc:
            raise self._error(str(exc)) from exc

    async def pack_details(self, reference: str, source: str = "modrinth") -> dict[str, Any]:
        if source == gtnh.PROJECT:
            try:
                return await self.gtnh.project()
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
        if source == "curseforge" or "curseforge.com" in reference.lower() or reference.strip().isdigit():
            try:
                project = await self.curseforge.project(reference)
                files = await self.curseforge.files(project["id"])
            except DownloadError as exc:
                raise self._error(str(exc)) from exc
            if str(project["id"]) in gtnh.REFERENCE_ONLY:
                raise self._error(gtnh.REFERENCE_ONLY[str(project["id"])])
            logo = project.get("logo") or {}
            return {
                "id": str(project["id"]), "slug": project.get("slug"), "title": project.get("name"), "description": project.get("summary"),
                "icon_url": logo.get("thumbnailUrl") or logo.get("url"), "downloads": project.get("downloadCount", 0),
                "versions": [summarize_file(f, str(project.get("name") or "")) for f in files], "source": "curseforge",
                "page": page_link(project.get("slug"), str(project["id"])),
            }
        try:
            project = await self.modrinth.project(reference)
            versions = await self.modrinth.versions(project["id"])
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        return {
            "id": project["id"], "slug": project.get("slug"), "title": project["title"], "description": project.get("description"),
            "icon_url": project.get("icon_url"), "downloads": project.get("downloads", 0),
            "versions": [summarize_version(v) for v in versions], "source": "modrinth",
        }

    # ------------------------------------------------------------------- config
    def read_config(self, rec: ServerRecord) -> dict[str, Any]:
        path = self.properties_path(rec)
        if rec.is_proxy:
            return {
                "values": proxy_config.read_values(self._read(rec)), "schema": proxy_config.schema(), "exists": path.is_file(), "error": None,
                "restart_required_hint": self.svc.pm.runtime(rec.id).is_active, "title": "Proxy settings", "file": proxy_config.FILE_NAME,
            }
        return {
            "values": props.read_values(self._read(rec)), "schema": props.schema(), "exists": path.is_file(), "error": None,
            "restart_required_hint": self.svc.pm.runtime(rec.id).is_active,
        }

    def update_config(self, rec: ServerRecord, values: dict[str, Any]) -> dict[str, Any]:
        text = self._read(rec)
        if rec.is_proxy:
            self._write(rec, text, proxy_config.apply_managed(proxy_config.apply_values(text, values), rec.java_port)[0])
        else:
            self._write(rec, text, props.apply_managed(props.apply_values(text, values), rec.java_port)[0])
        return {"saved": True, "restart_required": self.svc.pm.runtime(rec.id).is_active}

    def read_raw_config(self, rec: ServerRecord) -> dict[str, Any]:
        if rec.is_proxy:
            return {"content": self._read(rec), "managed_keys": ["the port in host (under listeners)"], "file": proxy_config.FILE_NAME}
        return {"content": self._read(rec), "managed_keys": list(props.MANAGED_KEYS), "file": props.FILE_NAME}

    def write_raw_config(self, rec: ServerRecord, content: str) -> dict[str, Any]:
        file = proxy_config.FILE_NAME if rec.is_proxy else props.FILE_NAME
        if len(content.encode("utf-8")) > 512 * 1024:
            raise self._error(f"{file} is limited to 512 KiB.")
        new_text, changed = (proxy_config if rec.is_proxy else props).apply_managed(content, rec.java_port)
        self._write(rec, self._read(rec), new_text)
        return {"saved": True, "reset_managed_keys": changed, "restart_required": self.svc.pm.runtime(rec.id).is_active}

    def _write(self, rec: ServerRecord, old_text: str, new_text: str) -> None:
        path = self.properties_path(rec)
        if old_text and old_text != new_text:
            with contextlib.suppress(OSError):
                atomic_write_text(path.with_name(f"{path.name}.manager-bak"), old_text)
        atomic_write_text(path, new_text)

    def ensure_config(self, rec: ServerRecord, ctx: JobContext | None = None) -> None:
        """Re-assert what the manager owns (the port) before every start."""
        path = self.properties_path(rec)
        if rec.is_proxy and not path.is_file():  # BungeeCord would write its own, on a port that is not this server's
            atomic_write_text(path, self.initial_proxy_config(rec.java_port, {}))
            if ctx:
                ctx.step("config.yml was missing; wrote a new one")
            return
        text = self._read(rec)
        new_text, changed = (proxy_config if rec.is_proxy else props).apply_managed(text, rec.java_port)
        if changed or not path.is_file():
            atomic_write_text(path, new_text)
            if ctx and changed:
                ctx.step(f"Updated manager-controlled settings in {path.name} ({', '.join(changed)})")

    def op_default_level(self, rec: ServerRecord) -> int:
        return int(props.read_values(self._read(rec))["op-permission-level"])

    # -------------------------------------------------------------------- patch
    def patch(self, rec: ServerRecord, changes: dict[str, Any]) -> bool:
        """Apply changes to the Java settings (memory, extra options). Returns whether a restart is needed."""
        spec = dict(rec.spec)
        if "memory_mb" in changes:
            spec["memory_mb"] = self.parse_memory(changes["memory_mb"])
        if "jvm_args" in changes:
            spec["jvm_args"] = self.parse_jvm_args(changes["jvm_args"])
        if spec != rec.spec:
            self.svc.repo.update_server(rec.id, spec=spec)
            return True
        return False

    # -------------------------------------------------------------------- start
    def _write_eula(self, directory: Path) -> None:
        atomic_write_text(
            directory / "eula.txt",
            f"#By changing the setting below to TRUE you are indicating your agreement to our EULA ({EULA_URL}).\n"
            f"#Accepted through Pumpkin Server Manager on {iso()}\neula=true\n",
        )

    def _eula_accepted(self, directory: Path) -> bool:
        try:
            return props.parse((directory / "eula.txt").read_text(encoding="utf-8")).get("eula", "").lower() == "true"
        except OSError:
            return False

    def build_argv(self, rec: ServerRecord, java: Path, directory: Path) -> list[str]:
        spec = rec.spec
        launch = spec.get("launch") or {}
        argv = [str(java)]
        if launch.get("argfile") and (directory / "user_jvm_args.txt").is_file():
            argv.append("@user_jvm_args.txt")  # the pack's own options first, so the memory below wins
        memory = int(spec.get("memory_mb") or 4096)
        argv += [f"-Xms{memory}M", f"-Xmx{memory}M", *default_jvm_flags(int(spec.get("java") or 21))]
        argv += shlex.split(spec.get("jvm_args", ""))
        argv += launch_arguments(launch)
        if not rec.is_proxy:
            argv.append("nogui")  # (BungeeCord has no window to leave out)
        return argv

    async def prepare_start(self, rec: ServerRecord, ctx: JobContext) -> JavaLaunch:
        svc = self.svc
        spec = rec.spec
        directory = svc.server_dir(rec.id)
        major = int(spec.get("java") or 0)
        if not major:
            raise self._error("This server has no Java version assigned.")
        if self.runtimes.get(major) is None:
            ctx.step(f"Downloading Java {major}")
        java = await self.runtimes.ensure(major, self._java_progress(ctx, major))

        proxy = rec.is_proxy
        if not proxy and not self._eula_accepted(directory):  # a proxy runs no Minecraft server: there is no EULA to accept
            if not spec.get("eula_accepted_at"):
                raise self._error(f"The Minecraft EULA ({EULA_URL}) has not been accepted for this server.")
            self._write_eula(directory)
        self.ensure_config(rec, ctx)

        launch = spec.get("launch") or {}
        wanted = directory / (launch.get("argfile") or launch.get("jar") or "-")
        if not wanted.is_file():
            if spec.get("software"):
                rec = await self._reinstall_software(rec, java, ctx)  # the manager knows exactly what belongs here
                spec = rec.spec
            elif (spec.get("pack") or {}).get("source") == gtnh.PROJECT:
                raise self._error(
                    f"{launch.get('jar') or 'The file that starts GT New Horizons'} is missing from the server folder. Restore a backup, or "
                    "create the server again from the pack."
                )
            else:
                found = find_launch(directory, (spec.get("loader") or {}).get("kind", ""))
                if found is None:
                    raise self._error(
                        "The files that start the mod loader are missing from the server folder. "
                        "Restore a backup, or create the server again from its modpack."
                    )
                spec = {**spec, "launch": found}
                svc.repo.update_server(rec.id, spec=spec)
                rec = svc.get(rec.id)
        env = svc.child_env(rec)
        env.pop("RUST_BACKTRACE", None)
        env.update(JAVA_HOME=str(java.path), LANG="C.UTF-8", PATH=os.pathsep.join([str(java.java.parent), env.get("PATH", "")]).rstrip(os.pathsep))
        if spec.get("software"):
            label = f"{software_label(spec['software'])} on Java {major}"
        else:
            modpack, _ = self.describe(rec)
            label = f"{modpack['title'] or 'the modpack'} ({modpack['loader']['label']}, Java {major})"
        argv = self.build_argv(rec, java.java, directory)
        if proxy:
            return JavaLaunch(
                argv=self.argv_wrapper(argv) if self.argv_wrapper else argv, env=env, ready_re=PROXY_READY_RE, fail_re=PROXY_FAIL_RE, label=label,
                stop_command=PROXY_STOP_COMMAND,
            )
        return JavaLaunch(
            argv=self.argv_wrapper(argv) if self.argv_wrapper else argv, env=env, ready_re=READY_RE, fail_re=FAIL_RE, label=label,
            prompt_re=FML_PROMPT_RE, on_prompt=lambda lines: self._answer_prompt(rec, lines),
        )

    async def _answer_prompt(self, rec: ServerRecord, lines: list[str]) -> str | None:
        """Forge asked what to do with the blocks and items of the world that its mods no longer have. In the restart of an update from
        the pack's own server files they are meant to go (GT New Horizons removes some in most versions): the server is told to confirm,
        unless there are so many that something else must be wrong. Anywhere else the person is told what the server waits for."""
        found = FML_COUNT_RE.search("\n".join(lines))
        count = int(found.group(1)) if found else None
        names: list[str] = []
        stripped = [line.strip() for line in lines]
        if "Missing Blocks/Items:" in stripped:
            for line in stripped[stripped.index("Missing Blocks/Items:") + 1:]:
                if not line:
                    break
                if line not in names:
                    names.append(line)
        answered = rec.id in self.confirm_missing and count is not None and count <= MAX_CONFIRMED_MISSING
        self.prompts[rec.id] = {"count": count, "names": names[:8], "answered": answered}
        if answered:
            return FML_ANSWER
        what = f"{count} blocks or items" if count is not None else "blocks or items"
        reason = (
            f"that is more than an update removes (over {MAX_CONFIRMED_MISSING}), so the manager did not answer by itself"
            if rec.id in self.confirm_missing and count is not None else "the manager does not answer such questions by itself"
        )
        self.svc.reporter.record(
            "warning", "start",
            f"'{rec.name}' stopped at startup to ask a question: its world has {what} that its mods no longer have"
            f"{' (' + ', '.join(names[:4]) + (', …' if len(names) > 4 else '') + ')' if names else ''}; {reason}. Open its console and type "
            f"'{FML_ANSWER}' to remove them from the world (Forge makes a backup of the world first) or '/fml cancel' to stop the server.", rec.id,
        )
        return None

    async def _reinstall_software(self, rec: ServerRecord, java: Any, ctx: JobContext) -> ServerRecord:
        """The jar of a Vanilla, Paper, Fabric, Spigot or BungeeCord server is gone (deleted, or lost with a restore): fetch exactly the build it
        runs again. For Spigot that is a new build (SpigotMC keeps no old ones to choose), which takes as long as the first one did."""
        svc = self.svc
        software = rec.spec["software"]
        if software["kind"] == "spigot":
            self.software.invalidate()  # what is built is what SpigotMC offers now, so that is what is written down, not what was known minutes ago
        build, _ = await self.software.resolve(
            software["kind"], software["minecraft"] or None, None if software["kind"] == "spigot" else software.get("build") or None,
        )
        what = "building it again" if build.kind == "spigot" else "downloading them again"
        ctx.step(f"The files of {build.label} are missing from the server folder; {what}")
        launch = await self.installer.fetch(
            build, svc.server_dir(rec.id), java.java, lambda message: ctx.progress(None, message), self.fetch_progress(ctx, build, 0.2, 0.3),
            java_major=int(rec.spec.get("java") or 0) or None, jdk_progress=self._java_progress(ctx, int(rec.spec.get("java") or 0), kit=True),
        )
        new_software = {**software, "build": build.build} if build.build != software.get("build") else software
        svc.repo.update_server(rec.id, spec={**rec.spec, "launch": launch, "software": new_software})
        return svc.get(rec.id)

    # ------------------------------------------------------------ player lists
    def lists_dir(self, rec: ServerRecord) -> Path:
        return self.svc.server_dir(rec.id)  # vanilla keeps these files in the server folder itself

    @staticmethod
    def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
        if "bypassesPlayerLimit" in entry:
            entry = {**entry, "bypasses_player_limit": entry["bypassesPlayerLimit"]}
        return entry

    # ------------------------------------------------------------ runtimes api
    def runtime_overview(self) -> list[dict[str, Any]]:
        used: dict[int, list[str]] = {}
        for rec in self.svc.repo.list_servers():
            if rec.is_java and rec.spec.get("java"):
                used.setdefault(int(rec.spec["java"]), []).append(rec.id)
        return [{**j.public(), "used_by": used.get(j.major, [])} for j in self.runtimes.installed()]

    def lock_of(self, rec: ServerRecord) -> dict[str, Any] | None:
        try:
            return json.loads((self.svc.server_dir(rec.id) / LOCK_DIR / LOCK_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


__all__ = ["JavaServerService", "JavaLaunch", "PackError", "PackIndex", "READY_RE", "client_only_info"]
