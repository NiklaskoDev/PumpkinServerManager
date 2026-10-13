"""Modrinth modpacks (``.mrpack``): reading them safely and installing them into a server folder.

A pack file is untrusted input. Everything in it that ends up on disk or on the network is checked first: paths may not
leave the server folder, downloads may only come from the hosts the format allows and must match their checksums, and
sizes and counts are capped. The format: https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import uuid
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..errors import UserError
from ..util import atomic_write_text, dir_size, free_bytes
from .loaders import LoaderError, LoaderSpec
from .modrinth import ALLOWED_DOWNLOAD_HOSTS
from .net import BadDownloadError, DownloadError, HeadersFor, download, require_https

log = logging.getLogger(__name__)

INDEX_NAME = "modrinth.index.json"
LOCK_DIR = ".manager"
LOCK_NAME = "pack.json"
MAX_FILES = 5000
MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_INDEX_BYTES = 20 * 1024**2
MAX_OVERRIDE_BYTES = 4 * 1024**3
MAX_OVERRIDE_ENTRIES = 100_000
MAX_OVERRIDE_MODS = 2000  # mod jars inside the override folders that are looked up on Modrinth
MAX_OVERRIDE_MOD_BYTES = 1024**3
STAGING_DIR = "curseforge-staging"
# Where the files of CurseForge packs are hosted (the address in an API answer first goes through edge.forgecdn.net).
CURSEFORGE_DOWNLOAD_HOSTS = frozenset({"edge.forgecdn.net", "mediafilez.forgecdn.net", "media.forgecdn.net"})
LOADER_DEPENDENCIES = {"fabric-loader": "fabric", "quilt-loader": "quilt", "forge": "forge", "neoforge": "neoforge"}
_HEX = {"sha1": re.compile(r"^[0-9a-f]{40}$"), "sha512": re.compile(r"^[0-9a-f]{128}$")}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Files the manager looks after itself: a pack may not replace them.
_PROTECTED = ("eula.txt",)


class PackError(UserError):
    """The modpack is unusable; the message is safe to show to users."""


class PackDownloadError(PackError):
    """A mod could not be downloaded (the network, or the file is gone for now); trying again later may work."""


@dataclass
class PackFile:
    path: str
    sha1: str
    sha512: str  # empty for files CurseForge describes: they are identified by their SHA-1
    size: int
    urls: list[str]
    server: str  # required | optional | unsupported
    # What only the files of a CurseForge pack carry:
    project: str = ""  # "cf:<project id>": what other files call this one when they require it
    requires: tuple[str, ...] = ()  # the same kind of ids, of the projects this file needs
    client_hint: bool = False  # CurseForge tags this file as meant for clients only
    name: str = ""  # something to show a person
    page: str = ""  # where to get the file by hand, when the author does not allow automatic downloads
    manual: bool = False

    @property
    def key(self) -> str:
        """What identifies the file's content: its SHA-512, or ``sha1:<hex>`` where that is all the source tells."""
        return self.sha512 or f"sha1:{self.sha1}"


@dataclass
class PackIndex:
    name: str
    version_id: str
    summary: str
    minecraft: str
    loader: LoaderSpec
    files: list[PackFile] = field(default_factory=list)
    client_only: list[str] = field(default_factory=list)  # left out because Modrinth says the mod does not work on a server
    skip_overrides: set[str] = field(default_factory=set)  # mods the pack ships inside overrides/ that are left out for the same reason
    source: str = "modrinth"  # modrinth | curseforge | gtnh
    stage: str = field(default_factory=lambda: uuid.uuid4().hex)  # names this install's share of the CurseForge staging area
    override_prefixes: tuple[str, ...] = ("overrides/", "server-overrides/")  # the folders of the pack zip that hold configuration
    archive: Any = None  # an archive.ServerPack: the pack's own server files, with the mods and configuration, used instead
    archive_notes: dict[str, int] = field(default_factory=dict)  # what the server files held that was not taken over, and why
    archive_replace: Callable[[str], bool] | None = None  # the files of the server files that an update replaces even when they were changed

    @property
    def server_files(self) -> list[PackFile]:
        return [f for f in self.files if f.server != "unsupported"]

    @property
    def manual(self) -> list[PackFile]:
        """Files that have to be added by hand because their authors do not allow downloading them automatically."""
        return [f for f in self.server_files if f.manual] if self.archive is None else []

    def leave_out(self, paths: Iterable[str]) -> None:
        """Treat these files as client-only, as if the pack had said so: they are not downloaded or installed."""
        wanted = set(paths)
        for file in self.files:
            if file.path in wanted and file.server != "unsupported":
                file.server = "unsupported"
                self.client_only.append(file.path)

    def leave_out_overrides(self, paths: Iterable[str]) -> None:
        """Do not unpack these mods from the pack's override folders."""
        for path in paths:
            if path not in self.skip_overrides:
                self.skip_overrides.add(path)
                self.client_only.append(path)

    def summary_public(self) -> dict[str, Any]:
        files = self.server_files
        return {
            "name": self.name, "version": self.version_id, "summary": self.summary, "minecraft": self.minecraft,
            "loader": {"kind": self.loader.kind, "version": self.loader.version, "label": self.loader.label},
            "mods": self.archive.mod_count if self.archive is not None else len(files),
            "download_bytes": 0 if self.archive is not None else sum(f.size for f in files if not f.manual), "manual": len(self.manual),
            "source": self.source,
        }


def safe_relative_path(raw: Any, *, what: str = "A file in the modpack") -> str:
    """A path inside the server folder, as a normalised ``a/b/c`` string; anything that could leave it is refused."""
    if not isinstance(raw, str) or not raw or len(raw) > 300 or "\x00" in raw:
        raise PackError(f"{what} has no usable path.")
    path = raw.replace("\\", "/")
    if path.startswith("/") or _DRIVE_RE.match(path):
        raise PackError(f"{what} has an absolute path ({raw!r}), so the modpack was refused.")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise PackError(f"{what} points outside the server folder ({raw!r}), so the modpack was refused.")
    if parts[0].lower() == LOCK_DIR or PurePosixPath(*parts).name.lower() in _PROTECTED:
        raise PackError(f"{what} wants to replace a file the manager looks after ({raw!r}), so the modpack was refused.")
    return "/".join(parts)


def read_pack(pack: Path) -> PackIndex:
    """Parse and validate ``modrinth.index.json`` of an .mrpack."""
    try:
        with zipfile.ZipFile(pack) as zf:
            try:
                info = zf.getinfo(INDEX_NAME)
            except KeyError:
                raise PackError("That file is not a Modrinth modpack (it has no modrinth.index.json).") from None
            if info.file_size > MAX_INDEX_BYTES:
                raise PackError("The modpack's index is unreasonably large.")
            data = json.loads(zf.read(info).decode("utf-8"))
    except zipfile.BadZipFile:
        raise PackError("That file is not a Modrinth modpack (it is not a valid zip file).") from None
    except (ValueError, UnicodeDecodeError):
        raise PackError("The modpack's modrinth.index.json cannot be read.") from None
    if not isinstance(data, dict) or data.get("formatVersion") != 1 or data.get("game") != "minecraft":
        raise PackError("This modpack uses a format this manager does not understand (only Minecraft packs, format 1).")
    dependencies = data.get("dependencies") or {}
    minecraft = str(dependencies.get("minecraft") or "")
    loaders = [(kind, str(dependencies[dep])) for dep, kind in LOADER_DEPENDENCIES.items() if dep in dependencies]
    if not minecraft or len(loaders) != 1:
        raise PackError("The modpack must name one Minecraft version and exactly one mod loader (Fabric, Quilt, Forge or NeoForge).")
    try:
        loader = LoaderSpec(loaders[0][0], loaders[0][1], minecraft)
    except LoaderError as exc:
        raise PackError(str(exc)) from exc

    raw_files = data.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
        raise PackError(f"The modpack lists no files or more than {MAX_FILES}.")
    files: list[PackFile] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise PackError("The modpack's file list is damaged.")
        path = safe_relative_path(item.get("path"))
        if path.lower() in seen:
            raise PackError(f"The modpack lists {path} twice.")
        seen.add(path.lower())
        hashes = item.get("hashes") if isinstance(item.get("hashes"), dict) else {}
        sha1, sha512 = str(hashes.get("sha1", "")).lower(), str(hashes.get("sha512", "")).lower()
        if not (_HEX["sha1"].match(sha1) and _HEX["sha512"].match(sha512)):
            raise PackError(f"{path} has no valid checksums, so it could not be verified.")
        size = item.get("fileSize")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_FILE_BYTES:
            raise PackError(f"{path} has an unreasonable size.")
        urls = [u for u in item.get("downloads", []) if isinstance(u, str)] if isinstance(item.get("downloads"), list) else []
        allowed = []
        for url in urls:
            try:
                require_https(url, ALLOWED_DOWNLOAD_HOSTS)
            except DownloadError:
                continue
            allowed.append(url)
        env = item.get("env") if isinstance(item.get("env"), dict) else {}
        server = env.get("server", "required")
        if server not in ("required", "optional", "unsupported"):
            server = "required"
        if server != "unsupported" and not allowed:
            raise PackError(f"{path} is not downloadable from an allowed host (only Modrinth, GitHub and GitLab are), so the modpack was refused.")
        files.append(PackFile(path=path, sha1=sha1, sha512=sha512, size=size, urls=allowed, server=server))
    if sum(f.size for f in files) > MAX_TOTAL_BYTES:
        raise PackError("The modpack is unreasonably large.")
    return PackIndex(
        name=str(data.get("name") or "Modpack")[:120], version_id=str(data.get("versionId") or "")[:120],
        summary=str(data.get("summary") or "")[:500], minecraft=minecraft, loader=loader, files=files,
    )


def override_mods(pack: Path, prefixes: tuple[str, ...] = ("overrides/", "server-overrides/")) -> dict[str, str]:
    """The mods a pack ships inside its ``overrides/`` folders instead of listing them: {path in the server folder: SHA-1}.

    Pack makers do this a lot (the most downloaded server pack on Modrinth carries sixteen), so what Modrinth knows about a mod
    can only be asked for after hashing it. A later ``server-overrides/`` entry replaces an ``overrides/`` one, as when unpacking."""
    found: dict[str, str] = {}
    try:
        with zipfile.ZipFile(pack) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_OVERRIDE_ENTRIES:
                return {}
            for prefix in prefixes:
                for info in infos:
                    name = info.filename
                    if not name.startswith(prefix) or info.is_dir() or not name.lower().endswith(".jar"):
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16) or info.file_size > MAX_OVERRIDE_MOD_BYTES or len(found) >= MAX_OVERRIDE_MODS:
                        continue
                    try:
                        rel = safe_relative_path(name[len(prefix):], what="A mod in the modpack")
                    except PackError:
                        continue
                    digest = hashlib.sha1(usedforsecurity=False)  # Modrinth identifies files by it; nothing is trusted because of it
                    with zf.open(info) as src:
                        while chunk := src.read(1 << 20):
                            digest.update(chunk)
                    found[rel] = digest.hexdigest()
    except (zipfile.BadZipFile, RuntimeError, OSError):
        return {}
    return found


class ModCache:
    """Downloaded mod files, kept by checksum so a mod that several servers (or several versions) use is fetched once."""

    def __init__(self, base: Path):
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(base / ".partial", ignore_errors=True)
        self.forget_staged()  # leftovers of an install that was interrupted

    def path(self, key: str, stage: str = "") -> Path:
        if key.startswith("sha1:"):  # a file CurseForge described; kept apart because it is only held while it is installed
            digest = key[5:]
            return self.base / STAGING_DIR / (stage or "unstaged") / digest[:2] / digest
        return self.base / key[:2] / key

    def has(self, file: PackFile, stage: str = "") -> bool:
        # A file only gets its name here after its checksums matched; the size a pack declares is not part of the proof.
        return self.path(file.key, stage).is_file()

    def forget_staged(self, stage: str | None = None) -> None:
        """Delete what was downloaded from CurseForge (for one install, or all of it). CurseForge's terms ask for no cache of what
        its API hands out, so those files are only held for the minutes an install or update needs them: a server keeps its own."""
        shutil.rmtree(self.base / STAGING_DIR / stage if stage else self.base / STAGING_DIR, ignore_errors=True)

    def disk_usage(self) -> int:
        return dir_size(self.base)


Progress = Callable[[float, str], None]


def _digest_of(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _sha512_of(path: Path) -> str:
    return _digest_of(path, "sha512")


def _sha256_of(path: Path) -> str:
    return _digest_of(path, "sha256")


def _digest_matches(path: Path, recorded: str) -> bool:
    """Whether a file still is what the lock recorded: a bare SHA-512, or ``sha1:<hex>`` / ``sha256:<hex>``."""
    algorithm, colon, value = recorded.partition(":")
    if not colon:
        algorithm, value = "sha512", recorded
    return _digest_of(path, algorithm) == value


class PackInstaller:
    def __init__(self, client: httpx.AsyncClient, cache: ModCache, *, retry_delay: float = 2.0, concurrency: int = 6,
                 free_space: Callable[[Path], int] = free_bytes, headers_for: HeadersFor | None = None):
        self._client = client
        self._headers_for = headers_for  # what to send to CurseForge's download servers (its API key); nobody else gets anything
        self.cache = cache
        self._delay = retry_delay
        self._concurrency = concurrency
        self._free_space = free_space

    # ------------------------------------------------------------------- install
    async def prefetch(self, index: PackIndex, progress: Progress) -> list[PackFile]:
        """Download what the pack needs into the cache without putting anything in a server. Returns the files to install."""
        if index.archive is not None:
            return []  # the server files hold the mods
        wanted = [f for f in index.server_files if not f.manual]
        unique = {f.key: f for f in wanted}  # two paths with the same content are downloaded once
        missing = [f for f in unique.values() if not self.cache.has(f, index.stage)]
        needed = sum(f.size for f in missing) + sum(f.size for f in wanted) + 256 * 1024 * 1024
        if self._free_space(self.cache.base) < needed:
            raise PackError(f"There is not enough free disk space for this modpack (about {needed // (1024 * 1024)} MiB are needed).")
        await self._fetch_all(missing, progress, total=len(unique), already=len(unique) - len(missing), stage=index.stage)
        return wanted

    def _check_space(self, size: int, where: Path) -> None:
        needed = size + 256 * 1024 * 1024
        if self._free_space(where) < needed:
            raise PackError(f"There is not enough free disk space for this modpack (about {needed // (1024 * 1024)} MiB are needed).")

    @staticmethod
    def _lock_of(index: PackIndex, files: dict[str, str], overrides: dict[str, str]) -> dict[str, Any]:
        lock: dict[str, Any] = {
            "name": index.name, "version_id": index.version_id, "minecraft": index.minecraft,
            "loader": {"kind": index.loader.kind, "version": index.loader.version}, "files": files, "overrides": overrides,
            "client_only": sorted(index.client_only), "mode": "archive" if index.archive is not None else "files", "source": index.source,
        }
        if index.manual:
            lock["manual"] = [{"path": f.path, "name": f.name, "page": f.page} for f in index.manual]
        return lock

    async def install(self, index: PackIndex, pack: Path, server_dir: Path, progress: Progress) -> dict[str, Any]:
        """Put the pack's mods and overrides into ``server_dir``. Returns the lock: what was installed, for later updates."""
        from . import archive

        if index.archive is not None:
            self._check_space(index.archive.total_bytes, server_dir)
            mods, others = await asyncio.to_thread(archive.install, index.archive, server_dir, progress)
            lock = self._lock_of(index, mods, others)
            atomic_write_text(server_dir / LOCK_DIR / LOCK_NAME, json.dumps(lock, indent=1))
            return lock
        try:
            wanted = await self.prefetch(index, progress)
            placed: dict[str, str] = {}
            for pack_file in wanted:
                await asyncio.to_thread(self._place, pack_file, server_dir, index.stage)
                placed[pack_file.path] = pack_file.key
        finally:
            self.cache.forget_staged(index.stage)
        progress(0.95, "Applying the modpack's own configuration")
        overrides, _ = await asyncio.to_thread(
            self._apply_overrides, pack, server_dir, skip=index.skip_overrides, prefixes=index.override_prefixes
        )
        lock = self._lock_of(index, placed, overrides)
        atomic_write_text(server_dir / LOCK_DIR / LOCK_NAME, json.dumps(lock, indent=1))
        return lock

    async def update(
        self, old_lock: dict[str, Any], index: PackIndex, pack: Path, server_dir: Path, progress: Progress
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """Move a server from the pack version described by ``old_lock`` to ``index``.

        One rule keeps people's work safe: a file is only replaced or removed when it still is what the old pack version put
        there. Whatever somebody changed by hand stays; for a configuration file the pack's new version is saved next to it
        with the ending ``.pack-new``. Returns (the new lock, what happened: added / updated / removed / kept files).
        """
        from . import archive

        was_archive = old_lock.get("mode") == "archive"
        if was_archive != (index.archive is not None):
            raise PackError(
                "This version does not come the way the server was installed (one has the pack's own server files, the other only "
                "a list of mods), so it cannot be applied as an update. Create the server again from that version instead."
            )
        if index.archive is not None:
            self._check_space(index.archive.total_bytes, server_dir)
            mods, others, report = await asyncio.to_thread(
                archive.update, index.archive, server_dir, old_lock, frozenset({"server.properties"}), progress, index.archive_replace
            )
            lock = self._lock_of(index, mods, others)
            atomic_write_text(server_dir / LOCK_DIR / LOCK_NAME, json.dumps(lock, indent=1))
            return lock, report
        try:
            wanted = await self.prefetch(index, progress)
            old_files: dict[str, str] = old_lock.get("files", {})
            report = {"added": [], "updated": [], "removed": [], "kept_mods": [], "kept_configs": []}
            wanted_paths = {f.path for f in wanted}
            for path, recorded in old_files.items():
                if path in wanted_paths:
                    continue
                target = server_dir / path
                if not target.is_file():
                    continue
                if not await asyncio.to_thread(_digest_matches, target, recorded):
                    report["kept_mods"].append(path)  # somebody replaced it by hand
                    continue
                target.unlink()
                report["removed"].append(path)
            placed: dict[str, str] = {}
            for pack_file in wanted:
                path = pack_file.path
                target = server_dir / path
                placed[path] = pack_file.key
                if path in old_files and old_files[path] == pack_file.key and target.is_file():
                    continue  # the same file as before
                if path in old_files and target.is_file() and not await asyncio.to_thread(_digest_matches, target, old_files[path]):
                    report["kept_mods"].append(path)
                    continue
                await asyncio.to_thread(self._place, pack_file, server_dir, index.stage)
                report["updated" if path in old_files else "added"].append(path)
        finally:
            self.cache.forget_staged(index.stage)
        progress(0.95, "Applying the modpack's own configuration")
        overrides, kept = await asyncio.to_thread(
            self._apply_overrides, pack, server_dir, protect=old_lock.get("overrides", {}), never_replace=frozenset({"server.properties"}),
            skip=index.skip_overrides, prefixes=index.override_prefixes,
        )
        report["kept_configs"] = kept
        for rel in index.skip_overrides:  # files the pack ships that the server must not have: an earlier install may have put them there
            recorded = old_lock.get("overrides", {}).get(rel)
            target = server_dir / rel
            if recorded and target.is_file() and await asyncio.to_thread(_sha256_of, target) == recorded:
                target.unlink()  # still what the pack put there, nobody changed it
        lock = self._lock_of(index, placed, overrides)
        atomic_write_text(server_dir / LOCK_DIR / LOCK_NAME, json.dumps(lock, indent=1))
        return lock, report

    async def _fetch_all(self, files: list[PackFile], progress: Progress, *, total: int, already: int, stage: str = "") -> None:
        done = already
        semaphore = asyncio.Semaphore(self._concurrency)
        lock = asyncio.Lock()

        async def one(pack_file: PackFile) -> None:
            nonlocal done
            async with semaphore:
                await self._fetch(pack_file, stage)
            async with lock:
                done += 1
                progress(0.9 * done / max(total, 1), f"Downloading mods ({done} of {total})")

        progress(0.0, f"Downloading mods ({already} of {total})")
        tasks = [asyncio.create_task(one(f)) for f in files]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _fetch(self, pack_file: PackFile, stage: str = "") -> None:
        target = self.cache.path(pack_file.key, stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = self.cache.base / ".partial" / uuid.uuid4().hex
        part.parent.mkdir(parents=True, exist_ok=True)
        last: DownloadError | None = None
        # The checksums are the proof. The size a pack declares is not trusted to the byte (Better MC, with two million downloads,
        # lists a mod one byte too small and no launcher minds), so it only sets a ceiling for what is accepted.
        declared = pack_file.size
        ceiling = MAX_FILE_BYTES if declared <= 0 else min(MAX_FILE_BYTES, max(2 * declared, 16 * 1024**2))
        for url in pack_file.urls:
            from_curseforge = (urlsplit(url).hostname or "").lower() in CURSEFORGE_DOWNLOAD_HOSTS
            try:
                await download(
                    self._client, url, part, sha1=pack_file.sha1, sha512=pack_file.sha512 or None, max_size=ceiling,
                    allowed_hosts=CURSEFORGE_DOWNLOAD_HOSTS if from_curseforge else ALLOWED_DOWNLOAD_HOSTS, what=pack_file.path.rsplit("/", 1)[-1],
                    attempts=3, delay=self._delay, headers_for=self._headers_for if from_curseforge else None,
                )
            except DownloadError as exc:
                last = exc
                continue
            os.replace(part, target)
            return
        failure = PackError if isinstance(last, BadDownloadError) else PackDownloadError  # a wrong checksum will be wrong tomorrow too
        raise failure(f"{pack_file.path} could not be downloaded: {last}") from last

    def _place(self, pack_file: PackFile, server_dir: Path, stage: str = "") -> None:
        source = self.cache.path(pack_file.key, stage)
        dest = server_dir / pack_file.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.unlink(missing_ok=True)
        try:
            os.link(source, dest)  # the same bytes on disk, however many servers use them
        except OSError:
            shutil.copy2(source, dest)

    # ----------------------------------------------------------------- overrides
    def _apply_overrides(
        self, pack: Path, server_dir: Path, protect: dict[str, str] | None = None, never_replace: frozenset[str] = frozenset(),
        skip: Iterable[str] = (), prefixes: tuple[str, ...] = ("overrides/", "server-overrides/"),
    ) -> tuple[dict[str, str], list[str]]:
        """Copy ``overrides/`` and then ``server-overrides/`` (which win) into the server folder.

        With ``protect`` (an update: {path: sha256 the previous pack version wrote}) a file that exists is only replaced when
        it still has that content; otherwise the new version is saved beside it as ``<name>.pack-new``. Files in
        ``never_replace`` are left alone, and files in ``skip`` are not unpacked at all. Returns ({path: sha256 of the pack's
        version}, the files that were kept).
        """
        skipped = frozenset(skip)
        written: dict[str, str] = {}
        kept: list[str] = []
        total = 0
        root = server_dir.resolve()
        try:
            with zipfile.ZipFile(pack) as zf:
                infos = zf.infolist()
                if len(infos) > MAX_OVERRIDE_ENTRIES:
                    raise PackError("The modpack contains an unreasonable number of files.")
                for prefix in prefixes:
                    for info in infos:
                        if not info.filename.startswith(prefix) or info.is_dir():
                            continue
                        if stat.S_ISLNK(info.external_attr >> 16):
                            continue  # a link inside a pack could point anywhere
                        rel = safe_relative_path(info.filename[len(prefix):], what="A configuration file in the modpack")
                        if rel in skipped:
                            continue
                        dest = server_dir / rel
                        if not dest.resolve().is_relative_to(root):
                            raise PackError(f"{rel} points outside the server folder, so the modpack was refused.")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if protect is not None:
                            if rel in kept:  # decided in the first pass: server-overrides land beside the file as well
                                dest = dest.with_name(dest.name + ".pack-new")
                            elif rel not in written and dest.exists():  # the first time this update meets the path
                                if rel in never_replace:
                                    continue
                                if _sha256_of(dest) != protect.get(rel):
                                    kept.append(rel)
                                    dest = dest.with_name(dest.name + ".pack-new")
                        digest = hashlib.sha256()
                        with zf.open(info) as src, open(dest, "wb") as out:
                            while chunk := src.read(1 << 20):
                                total += len(chunk)
                                if total > MAX_OVERRIDE_BYTES:
                                    raise PackError("The modpack's configuration files are unreasonably large.")
                                digest.update(chunk)
                                out.write(chunk)
                        written[rel] = digest.hexdigest()
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:  # RuntimeError: an encrypted entry
            raise PackError(f"The modpack's configuration files could not be unpacked: {exc}") from exc
        return written, kept
