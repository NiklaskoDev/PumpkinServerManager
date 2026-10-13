"""Installing from a modpack's own server files: one zip that holds the mods and configuration of a ready server.

CurseForge packs often come with such a file (the pack author's own server pack). It is the best source for a server: the
author chose its mods, including the ones that may not be downloaded one by one. What differs from pack to pack is the layout:
some zips wrap everything in one folder and bring start scripts and a mod loader installer, others are just ``mods/`` and
``config/`` at the top. The manager installs the loader itself, so only the content is taken over.

A *whole* server zip (GT New Horizons publishes its server like that) also holds the loader: its libraries and jars. The
manager then takes those over as well, and only leaves out the start scripts and the files it looks after itself.

The zip is untrusted input like any pack: paths may not leave the server folder, links are ignored, and sizes and counts are
capped. Nothing is run.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .mrpack import MAX_OVERRIDE_ENTRIES, PackError, safe_relative_path

MAX_SERVER_PACK_BYTES = 16 * 1024**3
MAX_SERVER_PACK_FILE_BYTES = 4 * 1024**3
# Folders that can hold a pack's content as well as a wrapper: a zip with only "mods/" at its top is not wrapped in "mods".
_CONTENT_DIRS = frozenset({
    "mods", "config", "defaultconfigs", "kubejs", "scripts", "resourcepacks", "datapacks", "world", "plugins", "packmenu",
    "structures", "resources", "global_packs", "serverconfig", "patchouli_books",
})
# Start scripts and loader installers: the manager starts the server and installs the loader itself.
_ROOT_SKIP_SUFFIXES = (".sh", ".bat", ".cmd", ".ps1", ".command", ".jar")
# The manager looks after these (the lists are managed from the Players tab, the memory options from the Java settings).
_ROOT_SKIP_NAMES = frozenset({
    "eula.txt", "user_jvm_args.txt", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json", "usercache.json",
    "run.sh", "run.bat", "start.sh", "start.bat", "install.sh", "install.bat",
})
# What an installer or an earlier run leaves behind; the manager's own loader install creates its own.
_SKIP_DIRS = frozenset({"libraries", "versions", ".fabric", ".mixin.out", "logs", "crash-reports", ".manager", "__macosx"})
# A whole server zip brings its loader: its libraries are wanted, only the traces of earlier runs are not.
_WHOLE_SKIP_DIRS = _SKIP_DIRS - {"libraries", "versions", ".fabric"}
_SCRIPT_SUFFIXES = (".sh", ".bat", ".cmd", ".ps1", ".command")
_DRIVE = re.compile(r"^[A-Za-z]:")
Progress = Callable[[float, str], None]


def _hostile(name: str) -> bool:
    """Whether a name in the zip aims outside the folder it is unpacked into (an absolute path, a drive, a ``..`` part)."""
    path = name.replace("\\", "/")
    return path.startswith("/") or bool(_DRIVE.match(path)) or ".." in path.split("/")


def _wrapper(names: list[str]) -> str:
    """The folder every entry lives in ("Server-Files-1.1.1/"), or "" when the zip has no such wrapper."""
    files = [n for n in names if not n.endswith("/")]
    if not files or any("/" not in n for n in files):
        return ""
    firsts = {n.split("/", 1)[0] for n in files}
    if len(firsts) != 1:
        return ""
    only = next(iter(firsts))
    return "" if only.lower() in _CONTENT_DIRS else only + "/"


def _skipped(rel: str, whole: bool = False) -> str | None:
    """Why a file is not taken over (None: it is). ``rel`` may not be checked yet: this only looks at the names."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    if parts[0].lower() in (_WHOLE_SKIP_DIRS if whole else _SKIP_DIRS):
        return "logs and leftovers of earlier runs" if whole else "the loader installation and logs of a ready server are made again here"
    if "/".join(parts).lower() == "config/missing_mods_checker.json":
        return "the list for the Missing Mods Checker, which would open a window: the mods are in the server files"
    if len(parts) == 1:
        lower = parts[0].lower()
        if lower in _ROOT_SKIP_NAMES:
            return "the manager looks after it"
        if lower.endswith(_SCRIPT_SUFFIXES if whole else _ROOT_SKIP_SUFFIXES):
            return "a start script: the manager starts the server itself" if whole else "a start script or installer: the manager installs the loader and starts the server"
    return None


class ServerPack:
    """What a server pack zip holds, worked out before anything is written. ``whole``: the zip is a complete server that brings
    its own loader (libraries and jars), which is then taken over instead of being installed by the manager."""

    def __init__(self, path: Path, *, whole: bool = False):
        self.path = path
        self.whole = whole
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
        except (zipfile.BadZipFile, OSError) as exc:
            raise PackError(f"The server files of the modpack are not a usable zip file ({exc}).") from None
        if len(infos) > MAX_OVERRIDE_ENTRIES:
            raise PackError("The server files of the modpack contain an unreasonable number of files.")
        for info in infos:  # before the wrapper is worked out: a name like "../x/mods/a.jar" must not pass as a folder called ".."
            if _hostile(info.filename):
                raise PackError(f"The server files of the modpack have a file that points outside the server folder ({info.filename!r}), so they were refused.")
        self.prefix = _wrapper([i.filename for i in infos])
        self.entries: list[tuple[str, zipfile.ZipInfo]] = []  # (path in the server folder, entry)
        self.skipped: dict[str, int] = {}
        total = 0
        seen: set[str] = set()
        for info in infos:
            if info.is_dir() or not info.filename.startswith(self.prefix) or stat.S_ISLNK(info.external_attr >> 16):
                continue
            raw = info.filename[len(self.prefix):]
            reason = _skipped(raw, whole)  # before the path is checked: a server pack may carry the eula.txt the manager writes itself
            if reason is not None:
                self.skipped[reason] = self.skipped.get(reason, 0) + 1
                continue
            rel = safe_relative_path(raw, what="A file in the server files of the modpack")
            if rel.lower() in seen:
                raise PackError(f"The server files of the modpack contain {rel} twice.")
            seen.add(rel.lower())
            if info.file_size > MAX_SERVER_PACK_FILE_BYTES:
                raise PackError(f"{rel} in the server files of the modpack is unreasonably large.")
            total += info.file_size
            self.entries.append((rel, info))
        if total > MAX_SERVER_PACK_BYTES:
            raise PackError("The server files of the modpack are unreasonably large.")
        self.total_bytes = total

    @property
    def mod_count(self) -> int:
        return sum(1 for rel, _ in self.entries if is_mod(rel))

    @property
    def usable(self) -> bool:
        """A server pack that has no mods (some only carry configuration and fetch the rest with a script) is no use here."""
        return self.mod_count > 0


def is_mod(rel: str) -> bool:
    return rel.startswith("mods/") and rel.count("/") == 1 and rel.lower().endswith(".jar")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def install(pack: ServerPack, server_dir: Path, progress: Progress) -> tuple[dict[str, str], dict[str, str]]:
    """Unpack the server files into ``server_dir``. Returns (mods: {path: "sha256:<hex>"}, other files: {path: sha256 hex})."""
    mods: dict[str, str] = {}
    others: dict[str, str] = {}
    root = server_dir.resolve()
    done = 0
    with zipfile.ZipFile(pack.path) as zf:
        for index, (rel, info) in enumerate(pack.entries, 1):
            dest = server_dir / rel
            if not dest.resolve().is_relative_to(root):
                raise PackError(f"{rel} points outside the server folder, so the server files were refused.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            digest = _copy(zf, info, dest)
            if is_mod(rel):
                mods[rel] = f"sha256:{digest}"
            else:
                others[rel] = digest
            done += info.file_size
            if index % 25 == 0 or index == len(pack.entries):
                progress(done / max(pack.total_bytes, 1), f"Unpacking the server files ({index} of {len(pack.entries)})")
    return mods, others


def _copy(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> str:
    digest = hashlib.sha256()
    written = 0
    try:
        with zf.open(info) as src, open(dest, "wb") as out:
            while chunk := src.read(1 << 20):
                written += len(chunk)
                if written > MAX_SERVER_PACK_FILE_BYTES:
                    raise PackError(f"{info.filename} in the server files of the modpack is unreasonably large.")
                digest.update(chunk)
                out.write(chunk)
    except (RuntimeError, OSError, zipfile.BadZipFile) as exc:  # RuntimeError: an encrypted entry
        raise PackError(f"The server files of the modpack could not be unpacked: {exc}") from None
    return digest.hexdigest()


def update(
    pack: ServerPack, server_dir: Path, old_lock: dict[str, Any], never_replace: frozenset[str], progress: Progress,
    replace: Callable[[str], bool] | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Bring a server that was installed from an earlier version of the server files up to this one.

    The same rule as for every pack update: a file is only replaced or removed when it still is what the old version put there.
    Whatever somebody changed stays; the new version of such a file is saved next to it with the ending ``.pack-new``. A pack that
    says its files must always be the new version's (``replace`` names them: GT New Horizons tells to replace its whole
    configuration) gets them replaced even when they were changed, and the files are listed as ``replaced``; the backup made before
    the update has the old ones.
    Returns (mods, other files, what happened: added / updated / removed / replaced / kept_mods / kept_configs)."""
    old: dict[str, str] = {}
    for path, recorded in {**(old_lock.get("overrides") or {}), **(old_lock.get("files") or {})}.items():
        old[path] = str(recorded).removeprefix("sha256:")
    report: dict[str, list[str]] = {"added": [], "updated": [], "removed": [], "replaced": [], "kept_mods": [], "kept_configs": []}
    mods: dict[str, str] = {}
    others: dict[str, str] = {}
    root = server_dir.resolve()
    staging = server_dir / ".manager" / f"unpack-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    done = 0
    try:
        with zipfile.ZipFile(pack.path) as zf:
            for index, (rel, info) in enumerate(pack.entries, 1):
                dest = server_dir / rel
                if not dest.resolve().is_relative_to(root):
                    raise PackError(f"{rel} points outside the server folder, so the server files were refused.")
                part = staging / f"{index}.part"
                digest = _copy(zf, info, part)
                (mods if is_mod(rel) else others)[rel] = f"sha256:{digest}" if is_mod(rel) else digest
                if rel in never_replace and dest.exists():
                    part.unlink()
                elif not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(part, dest)
                    report["added"].append(rel)
                else:
                    on_disk = _sha256_of(dest)
                    if on_disk == digest:
                        part.unlink()  # already exactly this
                    elif rel in old and on_disk == old[rel]:
                        os.replace(part, dest)  # untouched since the old version: replaced
                        report["updated"].append(rel)
                    elif is_mod(rel):
                        part.unlink()  # somebody replaced the mod: theirs stays
                        report["kept_mods"].append(rel)
                    elif replace is not None and replace(rel):
                        os.replace(part, dest)  # the pack wants its own version, whoever changed this one
                        report["replaced"].append(rel)
                    else:
                        os.replace(part, dest.with_name(dest.name + ".pack-new"))  # somebody changed it: theirs stays
                        report["kept_configs"].append(rel)
                done += info.file_size
                if index % 25 == 0 or index == len(pack.entries):
                    progress(done / max(pack.total_bytes, 1), f"Unpacking the server files ({index} of {len(pack.entries)})")
        for rel, recorded in old.items():
            if rel in mods or rel in others:
                continue
            target = server_dir / rel
            if not target.is_file():
                continue
            if _sha256_of(target) == recorded:
                target.unlink()
                if is_mod(rel):
                    report["removed"].append(rel)  # only mods are reported: configuration files that went away are not news
            elif is_mod(rel):
                report["kept_mods"].append(rel)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return mods, others, report
