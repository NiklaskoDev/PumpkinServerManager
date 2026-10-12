"""Packs that ship the *Missing Mods Checker* mod (Better MC does): installing the mods its list asks for.

Some mods may not be put into a Modrinth pack (they are only on CurseForge), so a pack lists them in
``config/missing_mods_checker.json`` and includes the Missing Mods Checker mod. When the game starts, that mod looks for the listed
files and, if some are missing, opens a window that helps the player download them. A server has no screen: the mod then fails with
"No X11 DISPLAY variable was set" and the server ends before it has started.

The manager does what the window would have done. The listed mods are looked up through CurseForge's API (every entry names the file
by number) and installed like the mods of any pack, checked against CurseForge's checksums. Where that is not possible (no API key,
or an author who does not allow downloads) they are listed under *Mods to add yourself*. The mod's own list is not put into the
server, so the mod finds nothing to ask about.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .curseforge import CLASS_MODS, CurseForgeClient, clean_page, pack_file_of
from .mrpack import PackError, PackFile, PackIndex
from .net import DownloadError

log = logging.getLogger(__name__)

CONFIG = "config/missing_mods_checker.json"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_ENTRIES = 500
# "https://www.curseforge.com/minecraft/mc-mods/balm-fabric/download/7420614": the file's number is all that is needed.
_FILE_ADDRESS = re.compile(r"^https://(?:www\.)?curseforge\.com/minecraft/[a-z0-9-]+/[a-z0-9][a-z0-9_-]{0,100}/download/(\d{1,10})/?$", re.IGNORECASE)
_PLAIN_NAME = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]{1,200}\.jar$")


@dataclass(frozen=True)
class Wanted:
    """One entry of the list: a mod the pack wants in a folder of the game."""

    name: str
    pattern: str  # a file name or a regular expression, as the pack's author wrote it
    url: str  # where a player would get it
    destination: str
    file_id: int | None  # CurseForge's number for the file, when the address names one

    @property
    def literal(self) -> str:
        """The file name, when the pattern is one (the usual case); a regular expression cannot name the file."""
        return self.pattern if _PLAIN_NAME.match(self.pattern) else ""


def read_wanted(pack: Path, prefixes: tuple[str, ...]) -> list[Wanted] | None:
    """The mods a pack's Missing Mods Checker wants in the mods folder; None when the pack has no such list. Untrusted input."""
    try:
        with zipfile.ZipFile(pack) as zf:
            text = None
            for prefix in prefixes:  # a later folder wins, as when the files are unpacked
                try:
                    info = zf.getinfo(prefix + CONFIG)
                except KeyError:
                    continue
                if info.file_size > MAX_CONFIG_BYTES:
                    raise PackError("The list of mods that the modpack's Missing Mods Checker wants is unreasonably large.")
                text = zf.read(info).decode("utf-8-sig")
    except (zipfile.BadZipFile, OSError):
        return None
    except (UnicodeDecodeError, RuntimeError):
        raise PackError("The modpack's Missing Mods Checker list cannot be read.") from None
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        raise PackError("The modpack's Missing Mods Checker list is not valid JSON.") from None
    if not isinstance(data, list) or len(data) > MAX_ENTRIES:
        raise PackError(f"The modpack's Missing Mods Checker list is not a list of mods, or has more than {MAX_ENTRIES} entries.")
    found: list[Wanted] = []
    for entry in data:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("displayName", "pattern", "url")):
            continue
        address = _FILE_ADDRESS.match(entry["url"].strip())
        destination = str(entry.get("destination") or "mods").strip().strip("/").lower()
        found.append(Wanted(
            name=entry["displayName"].strip()[:120] or entry["pattern"][:120], pattern=entry["pattern"].strip(), url=entry["url"].strip(),
            destination=destination, file_id=int(address.group(1)) if address else None,
        ))
    return found


async def add_wanted(
    index: PackIndex, wanted: list[Wanted], present: set[str], curseforge: CurseForgeClient,
    say: Callable[[str], None], warn: Callable[[str], None],
) -> None:
    """Add the wanted mods to the pack's files: installable ones through CurseForge, the rest as files to add by hand.

    ``present`` holds the paths (lower case) the pack already provides, in its list and in its override folders."""
    paths = {f.path.lower() for f in index.files} | present
    todo = [w for w in wanted if w.destination == "mods" and not (w.literal and f"mods/{w.literal}".lower() in paths)]
    if not todo:
        return
    files: dict[int, dict] = {}
    projects: dict[int, dict] = {}
    ids = sorted({w.file_id for w in todo if w.file_id})
    if ids and curseforge.enabled:
        try:
            files = await curseforge.resolve_files(ids)
            projects = await curseforge.resolve_projects(sorted({f["modId"] for f in files.values() if isinstance(f.get("modId"), int)}))
        except DownloadError as exc:
            warn(f"CurseForge could not be asked for the mods the modpack's Missing Mods Checker wants ({exc}); they are listed to add by hand")
    installed = by_hand = unknown = 0
    for w in todo:
        file = files.get(w.file_id) if w.file_id else None
        if file is not None:
            mod = projects.get(file.get("modId")) or {}
            if mod.get("classId") not in (CLASS_MODS, None):
                continue
            made = pack_file_of(file, mod, int(file["modId"]))
            if made is None or made.path.lower() in paths:
                continue
            made.name = w.name or made.name
            made.page = made.page or clean_page(w.url)
            index.files.append(made)
            paths.add(made.path.lower())
            installed += 1 if not made.manual else 0
            by_hand += 1 if made.manual else 0
        elif w.literal:  # not resolved (no key, or CurseForge does not have it any more): the person gets it from the page
            path = f"mods/{w.literal}"
            index.files.append(PackFile(path=path, sha1="", sha512="", size=0, urls=[], server="required", name=w.name, page=clean_page(w.url), manual=True))
            paths.add(path.lower())
            by_hand += 1
        else:
            unknown += 1
    if installed or by_hand:
        say(
            f"The modpack's Missing Mods Checker lists {len(todo)} mods that are not in the pack: {installed} are installed from CurseForge"
            + (f", {by_hand} have to be added by hand" if by_hand else "")
        )
    if by_hand and not curseforge.enabled:
        warn("Add a CurseForge API key in Settings and the manager installs such mods itself; without one they are listed to add by hand.")
    if unknown:
        warn(f"{unknown} of the entries name their mod by a pattern the manager cannot turn into a file, so they are left out")
