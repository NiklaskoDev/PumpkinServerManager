"""Java servers: creating modpack servers, starting them and managing their settings.

A Java server is an ordinary server of the manager (same console, backups, players, accounts) that runs a different
engine: the manager downloads the Java it needs, installs the modpack and its mod loader, and starts it with the right
command line. ``ServerService`` hands over to this class wherever the two engines differ.
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
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..jobs import JobContext
from ..models import ServerPolicy, ServerRecord
from ..pumpkin_config import ConfigError
from ..servers import ServerError
from ..util import atomic_write_text, iso
from . import properties as props
from .loaders import LoaderInstaller, find_launch, launch_arguments
from .modrinth import ModrinthClient, parse_project_reference, summarize_version
from .mojang import MojangMeta, available_java
from .mrpack import LOCK_DIR, LOCK_NAME, PackError, PackIndex, PackInstaller, override_mods, read_pack
from .net import DownloadError
from .runtime import JavaRuntimes

if TYPE_CHECKING:
    from ..servers import ServerService

log = logging.getLogger(__name__)

READY_RE = re.compile(r"\bDone \([0-9.,]+s\)!")  # "Done (12.345s)! For help, type "help"": printed by vanilla and every loader
# What vanilla, Forge and NeoForge log when a server cannot start (a mod that fails to load, for instance). Forge and NeoForge can
# stay alive afterwards on threads of their own, so the manager acts on this line instead of waiting for the startup timeout.
FAIL_RE = re.compile(r"Failed to start the minecraft server")
EULA_URL = "https://aka.ms/MinecraftEULA"
_SECRET_ALPHABET = string.ascii_letters + string.digits
MIN_MEMORY_MB, MAX_MEMORY_MB = 512, 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
# Modded servers start slowly, save slowly and write a lot of files the world does not need to be restored.
JAVA_POLICY_DEFAULTS: dict[str, Any] = {
    "start_timeout": 900, "stop_timeout": 180, "update_mode": "notify", "backup_exclude": ["logs", "crash-reports", "cache"],
}


def mods_text(count: int) -> str:
    return f"{count} mod" if count == 1 else f"{count} mods"


def client_only_info(index: PackIndex) -> dict[str, Any]:
    """What the server remembers about the mods that were left out because they only work on a client (for the dashboard)."""
    return {"client_only": sorted(index.client_only)[:400], "client_only_count": len(index.client_only)}


def default_jvm_flags(java_major: int) -> list[str]:
    flags = ["-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8"]
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


class JavaServerService:
    def __init__(
        self, svc: ServerService, runtimes: JavaRuntimes, mojang: MojangMeta, modrinth: ModrinthClient, loaders: LoaderInstaller,
        packs: PackInstaller, uploads_dir: Path,
    ):
        self.svc = svc
        self.runtimes = runtimes
        self.mojang = mojang
        self.modrinth = modrinth
        self.loaders = loaders
        self.packs = packs
        self.uploads_dir = uploads_dir
        self._icons: dict[str, tuple[bytes, str]] = {}
        self.argv_wrapper: Callable[[list[str]], list[str]] | None = None  # a test seam, like ServerService.command_builder
        uploads_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _error(message: str) -> ServerError:
        return ServerError(message)

    def properties_path(self, rec_or_id: ServerRecord | str) -> Path:
        server_id = rec_or_id if isinstance(rec_or_id, str) else rec_or_id.id
        return self.svc.server_dir(server_id) / props.FILE_NAME

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
    def describe(self, rec: ServerRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        """(modpack, java) parts of the server view: what it runs, and how it is run."""
        spec = rec.spec
        pack = spec.get("pack") or {}
        loader = spec.get("loader") or {}
        label = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}.get(loader.get("kind", ""), loader.get("kind", ""))
        modpack = {
            "source": pack.get("source"), "title": pack.get("title"), "slug": pack.get("slug"), "icon": pack.get("icon"),
            "version_number": pack.get("version_number"), "pending": pack.get("pending"), "can_roll_back": bool(spec.get("previous")),
            "client_only": pack.get("client_only", []), "client_only_count": pack.get("client_only_count", 0),
            "leave_out_client_only": spec.get("leave_out_client_only", True), "minecraft": spec.get("minecraft"),
            "loader": {"kind": loader.get("kind"), "version": loader.get("version"), "label": f"{label} {loader.get('version', '')}".strip()},
        }
        installed = self.runtimes.get(int(spec.get("java") or 0))
        java = {
            "major": spec.get("java"), "memory_mb": spec.get("memory_mb"), "jvm_args": spec.get("jvm_args", ""),
            "runtime": installed.release if installed else None,
        }
        return modpack, java

    # ------------------------------------------------------------------ create
    async def create(self, spec: dict[str, Any], ctx: JobContext) -> ServerRecord:
        svc = self.svc
        name = " ".join(str(spec["name"]).split())
        if not 1 <= len(name) <= 40:
            raise self._error("The server name must be 1-40 characters long.")
        if any(r.name.lower() == name.lower() for r in svc.repo.list_servers()):
            raise self._error("A server with that name already exists.")
        if spec.get("eula") is not True:
            raise self._error(f"Running a Minecraft server means accepting the Minecraft EULA ({EULA_URL}). Tick the box to confirm.")
        try:
            memory = self.parse_memory(4096 if spec.get("memory_mb") is None else spec["memory_mb"])  # the API sends None when it was left out
            jvm_args = self.parse_jvm_args(spec.get("jvm_args"))
            values = props.validate_values({"op-permission-level": props.DEFAULT_OP_LEVEL, **(spec.get("settings") or {})})
            policy = ServerPolicy.model_validate({**JAVA_POLICY_DEFAULTS, **(spec.get("policy") or {})})
        except ConfigError as exc:
            raise self._error(f"Invalid settings: {exc}") from exc
        except ValueError as exc:
            raise self._error(f"Invalid policy: {exc}") from exc

        modpack = spec.get("modpack") or {}
        staging = svc.settings.servers_dir / f".create-{uuid.uuid4().hex}"
        pack_path: Path | None = None
        downloaded = False
        try:
            ctx.step("Looking up the modpack")
            pack_path, pack_info, downloaded = await self._obtain_pack(modpack, ctx)
            index = read_pack(pack_path)
            leave_out = spec.get("leave_out_client_only") is not False
            if leave_out:
                await self.leave_out_client_only(index, ctx, pack_path)
            pack_info = {**pack_info, **client_only_info(index)}
            overview = index.summary_public()
            ctx.step(f"{index.name} {index.version_id}: Minecraft {index.minecraft}, {index.loader.label}, {mods_text(overview['mods'])}")

            major = available_java(await self.mojang.java_major(index.minecraft))
            ctx.step(f"Minecraft {index.minecraft} needs Java {major}")
            java = await self.runtimes.ensure(major, self._java_progress(ctx, major))

            staging.mkdir(parents=True)
            ctx.step(f"Installing {mods_text(overview['mods'])}")
            await self.packs.install(index, pack_path, staging, lambda fraction, message: ctx.progress(0.25 + 0.5 * fraction, message))
            ctx.step(f"Installing {index.loader.label}")
            launch = await self.loaders.install(index.loader, staging, java.java, lambda message: ctx.progress(None, message))
            ctx.progress(0.9, "Creating the server")
            self._write_eula(staging)
            text = props.apply((staging / props.FILE_NAME).read_text(encoding="utf-8") if (staging / props.FILE_NAME).is_file() else "", values)

            async with svc._create_lock:
                if any(r.name.lower() == name.lower() for r in svc.repo.list_servers()):  # another creation may have finished meanwhile
                    raise self._error("A server with that name already exists.")
                server_id = svc._unique_id(name)
                java_port = svc.allocate_port("java", spec.get("java_port"))
                mgmt_port = svc.allocate_port("mgmt")
                directory = svc.server_dir(server_id)
                rec = ServerRecord(
                    id=server_id, name=name, created_at=iso(), channel="", pinned_tag=None, build_id=None, previous_build_id=None,
                    java_port=java_port, bedrock_port=None, mgmt_port=mgmt_port,
                    mgmt_secret="".join(secrets.choice(_SECRET_ALPHABET) for _ in range(40)), policy=policy, desired_running=False,
                    last_backup_at=iso(), last_update_check_at=None, pending_build_id=None, pending_since=None, engine="java",
                    spec={
                        "kind": "modpack", "minecraft": index.minecraft, "loader": {"kind": index.loader.kind, "version": index.loader.version},
                        "java": major, "memory_mb": memory, "jvm_args": jvm_args, "launch": launch, "pack": pack_info,
                        "leave_out_client_only": leave_out, "eula_accepted_at": iso(),
                    },
                )
                try:
                    os.replace(staging, directory)
                    atomic_write_text(directory / props.FILE_NAME, props.apply_managed(text, java_port)[0])
                    svc.repo.insert_server(rec)
                except BaseException:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if pack_path is not None and downloaded:
                pack_path.unlink(missing_ok=True)
        if modpack.get("source") == "upload":
            self._forget_upload(modpack.get("upload"))
        ctx.claim(rec.id)
        ctx.step(f"Server '{name}' created on port {java_port}")
        svc.reporter.record("success", "server", f"Server '{name}' created ({index.name}, port {java_port})", rec.id)
        svc.publish_state(rec.id)
        if spec.get("start", True):
            ctx.step("Starting the server for the first time (the world is generated now, which takes a while)")
            await svc._do_start(rec, ctx)
        return rec

    async def leave_out_client_only(self, index: PackIndex, ctx: JobContext, pack_path: Path | None = None) -> None:
        """Drop the mods Modrinth says do not work on a server, although the pack lists them for it.

        Packs exported from a client launcher mark nearly every file "required" on the server, client-only mods (minimaps, shaders,
        menus, ...) included, and many of those stop a server from starting. The mod's own project knows better. Mods that the
        pack ships inside its override folders are looked up too, and a mod that another mod needs stays, whatever its own
        project says about servers (a client-only library that a server mod depends on is still needed there)."""
        candidates = [f for f in index.server_files if any(urlsplit(u).hostname == "cdn.modrinth.com" for u in f.urls)]
        shipped = await asyncio.to_thread(override_mods, pack_path) if pack_path is not None else {}
        hashes = [f.sha1 for f in candidates] + list(shipped.values())
        if not hashes:
            return
        ctx.step("Asking Modrinth which mods only work on a client")
        try:
            support = await self.modrinth.server_support(hashes)
        except DownloadError as exc:
            raise self._error(f"Could not ask Modrinth which mods are client-only: {exc}") from exc
        client_only = {sha1 for sha1 in hashes if support.get(sha1, {}).get("side") == "unsupported"}
        needed = {need for sha1 in hashes if sha1 not in client_only for need in support.get(sha1, {}).get("requires", [])}
        kept: set[str] = set()
        grew = True
        while grew:  # something that stays can need a client-only library, which can need another one
            grew = False
            for sha1 in client_only - kept:
                if support[sha1]["project"] in needed:
                    kept.add(sha1)
                    needed.update(support[sha1]["requires"])
                    grew = True
        out = client_only - kept
        paths = [f.path for f in candidates if f.sha1 in out]
        shipped_paths = [path for path, sha1 in shipped.items() if sha1 in out]
        index.leave_out(paths)
        index.leave_out_overrides(shipped_paths)
        count = len(paths) + len(shipped_paths)
        if count:
            ctx.step(f"Leaving out {mods_text(count)} that Modrinth says {'does' if count == 1 else 'do'} not work on a server")
        if kept:
            ctx.step(f"Keeping {mods_text(len(kept))} that only work{'s' if len(kept) == 1 else ''} on a client, because other mods in the pack need {'it' if len(kept) == 1 else 'them'}")

    def _java_progress(self, ctx: JobContext, major: int) -> Callable[[int, int], None]:
        def callback(done: int, total: int) -> None:
            ctx.progress(0.05 + 0.15 * (done / total if total else 0), f"Downloading Java {major} ({done // 1048576} of {total // 1048576} MiB)")

        return callback

    async def _obtain_pack(self, modpack: dict[str, Any], ctx: JobContext) -> tuple[Path, dict[str, Any], bool]:
        """The .mrpack to install: (file, what the server remembers about it, whether the file is ours to delete)."""
        source = modpack.get("source")
        if source == "upload":
            path = self._upload_path(modpack.get("upload"))
            index = read_pack(path)
            return path, {"source": "upload", "title": index.name, "version_number": index.version_id}, False
        if source != "modrinth":
            raise self._error("Choose a modpack: search Modrinth, paste a link, or upload a .mrpack file.")
        try:
            reference = parse_project_reference(str(modpack.get("project", "")))
            project = await self.modrinth.project(reference)
            versions = await self.modrinth.versions(project["id"])
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        if not versions:
            raise self._error(f"'{project['title']}' has no versions that can be installed.")
        wanted = modpack.get("version")
        if wanted:
            version = next((v for v in versions if v["id"] == wanted), None)
            if version is None:
                raise self._error("That version of the modpack does not exist (any more).")
        else:
            version = next((v for v in versions if v.get("version_type") == "release"), versions[0])
        ctx.step(f"Downloading {project['title']} {version.get('version_number')}")
        path = self.uploads_dir / f"{uuid.uuid4().hex}.mrpack"
        try:
            await self.modrinth.download_pack(
                version, path, lambda done, total: ctx.progress(0.2 + 0.05 * (done / total if total else 0), "Downloading the modpack file")
            )
        except DownloadError as exc:
            path.unlink(missing_ok=True)
            raise self._error(str(exc)) from exc
        info = {
            "source": "modrinth", "project": project["id"], "slug": project.get("slug"), "title": project["title"],
            "icon": project.get("icon_url"), "version_id": version["id"], "version_number": version.get("version_number"),
            "published": version.get("date_published"), "version_type": version.get("version_type", "release"),
        }
        return path, info, True

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
            return {"upload": upload_id, "pack": read_pack(path).summary_public()}
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
            icon = await self.modrinth.fetch_image(url)
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        if len(self._icons) >= 300:
            self._icons.pop(next(iter(self._icons)))
        self._icons[url] = icon
        return icon

    async def search_packs(self, query: str, offset: int = 0) -> dict[str, Any]:
        try:
            return await self.modrinth.search(query, offset=offset)
        except DownloadError as exc:
            raise self._error(str(exc)) from exc

    async def pack_details(self, reference: str) -> dict[str, Any]:
        try:
            project = await self.modrinth.project(reference)
            versions = await self.modrinth.versions(project["id"])
        except DownloadError as exc:
            raise self._error(str(exc)) from exc
        return {
            "id": project["id"], "slug": project.get("slug"), "title": project["title"], "description": project.get("description"),
            "icon_url": project.get("icon_url"), "downloads": project.get("downloads", 0),
            "versions": [summarize_version(v) for v in versions],
        }

    # ------------------------------------------------------------------- config
    def read_config(self, rec: ServerRecord) -> dict[str, Any]:
        path = self.properties_path(rec)
        return {
            "values": props.read_values(self._read(rec)), "schema": props.schema(), "exists": path.is_file(), "error": None,
            "restart_required_hint": self.svc.pm.runtime(rec.id).is_active,
        }

    def update_config(self, rec: ServerRecord, values: dict[str, Any]) -> dict[str, Any]:
        text = self._read(rec)
        self._write(rec, text, props.apply_managed(props.apply_values(text, values), rec.java_port)[0])
        return {"saved": True, "restart_required": self.svc.pm.runtime(rec.id).is_active}

    def read_raw_config(self, rec: ServerRecord) -> dict[str, Any]:
        return {"content": self._read(rec), "managed_keys": list(props.MANAGED_KEYS), "file": props.FILE_NAME}

    def write_raw_config(self, rec: ServerRecord, content: str) -> dict[str, Any]:
        if len(content.encode("utf-8")) > 512 * 1024:
            raise self._error("server.properties is limited to 512 KiB.")
        new_text, changed = props.apply_managed(content, rec.java_port)
        self._write(rec, self._read(rec), new_text)
        return {"saved": True, "reset_managed_keys": changed, "restart_required": self.svc.pm.runtime(rec.id).is_active}

    def _write(self, rec: ServerRecord, old_text: str, new_text: str) -> None:
        path = self.properties_path(rec)
        if old_text and old_text != new_text:
            with contextlib.suppress(OSError):
                atomic_write_text(path.with_name("server.properties.manager-bak"), old_text)
        atomic_write_text(path, new_text)

    def ensure_config(self, rec: ServerRecord, ctx: JobContext | None = None) -> None:
        """Re-assert what the manager owns (the port) before every start."""
        text = self._read(rec)
        new_text, changed = props.apply_managed(text, rec.java_port)
        if changed or not self.properties_path(rec).is_file():
            atomic_write_text(self.properties_path(rec), new_text)
            if ctx and changed:
                ctx.step(f"Updated manager-controlled settings in server.properties ({', '.join(changed)})")

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
        argv.append("nogui")
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

        if not self._eula_accepted(directory):
            if not spec.get("eula_accepted_at"):
                raise self._error(f"The Minecraft EULA ({EULA_URL}) has not been accepted for this server.")
            self._write_eula(directory)
        self.ensure_config(rec, ctx)

        launch = spec.get("launch") or {}
        wanted = directory / (launch.get("argfile") or launch.get("jar") or "-")
        if not wanted.is_file():
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
        modpack, _ = self.describe(rec)
        label = f"{modpack['title'] or 'the modpack'} ({modpack['loader']['label']}, Java {major})"
        argv = self.build_argv(rec, java.java, directory)
        return JavaLaunch(argv=self.argv_wrapper(argv) if self.argv_wrapper else argv, env=env, ready_re=READY_RE, fail_re=FAIL_RE, label=label)

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
