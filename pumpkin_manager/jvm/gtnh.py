"""GT New Horizons (GTNH), installed from its own download site.

GTNH is not a modpack that can be installed from a list of mods. Its page on CurseForge is only a list *for reference*: 44 of the
pack's mods, without GregTech itself (a server made from it dies in the first mixin that needs GregTech), and the pack's own README
says to install from downloads.gtnewhorizons.com. There the pack is published as zips of complete servers, one for Java 8 and one for
Java 17 and newer (with the lwjgl3ify launcher). A zip holds everything a server needs: the mods, the configuration, the Forge
files. So the manager unpacks one as it is (see ``archive``) and starts it the way the pack's own start script does.

The list of versions is the pack's version history page: there is no directory listing, no API and no checksum, so the addresses
are read from that page, only ever from downloads.gtnewhorizons.com, and the zip is checked like any pack file (names, sizes,
CRC of every entry while unpacking).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..util import atomic_write_text
from .archive import ServerPack
from .loaders import LoaderSpec
from .mojang import AVAILABLE_JAVA
from .mrpack import LOCK_DIR, LOCK_NAME, PackError, PackIndex
from .net import DownloadError, ProgressCallback, download, get_text, require_https

PROJECT = "gtnh"
TITLE = "GT New Horizons"
SLUG = "gt-new-horizons"
SUMMARY = (
    "A large, progressive expert-mode modpack for Minecraft 1.7.10, balanced around GregTech: thousands of recipe changes, a quest book "
    "with over 3000 quests and many mods made for the pack. Its servers need at least 6 GiB of memory."
)
SITE = "https://www.gtnewhorizons.com"
HISTORY_URL = f"{SITE}/version-history/"
DOWNLOAD_HOSTS = frozenset({"downloads.gtnewhorizons.com"})
MINECRAFT = "1.7.10"
FORGE = "10.13.4.1614"  # what GTNH is built on; the server files name theirs, this only serves when they do not
MEMORY_MB = 6144  # what GTNH's own start scripts give a server
START_TIMEOUT = 1800  # the first start unpacks and generates for several minutes
CACHE_SECONDS = 3600
RECHECK_SECONDS = 60  # a version the list does not know is looked for on a fresh page when the list is older than this
MAX_PAGE_BYTES = 8 * 1024 * 1024  # the page is a few hundred KB
MAX_DOWNLOAD_BYTES = 4 * 1024**3  # the server files are about 450 MB

VARIANTS = ("modern", "legacy")
VARIANT_NAMES = {"modern": "Java 17 and newer", "legacy": "Java 8"}
LAUNCHER = "lwjgl3ify-forgePatches.jar"  # what starts the pack on Java 17 and newer
ARGUMENTS = "java9args.txt"  # its Java options (the class loader and the --add-opens that modern Java wants)
FML_TIMEOUT = "-Dfml.readTimeout=180"  # in both of the pack's start scripts: Forge waits that long for a slow mod
LEGACY_FLAGS = ("-XX:+UseStringDeduplication", "-XX:+UseCompressedOops", "-XX:+UseCodeCacheFlushing")  # the Java 8 script's own
KEEP_CONFIG = ("config/JourneyMapServer/",)  # the pack's update guide: this one stays (its loss makes the players lose their maps)
SERVERUTILITIES_CONFIG = "serverutilities/serverutilities.cfg"  # holds the settings of the mod that makes backups of its own

# The CurseForge project that only lists GTNH's mods for reference: what a server made from it gets told.
REFERENCE_ONLY = {
    "252507": (
        "The CurseForge page of GT New Horizons is only a list for reference: it holds 44 of the pack's mods and not GregTech itself, so a "
        "server made from it cannot start. Use New server → GT New Horizons instead: it installs the pack's official server files from "
        "gtnewhorizons.com."
    ),
}

_BLOCK = re.compile(r"<details\b.*?</details>", re.S | re.I)
_SUMMARY = re.compile(r"<summary\b.*?</summary>", re.S | re.I)
_SERVER_LINK = re.compile(
    r'href="https://downloads\.gtnewhorizons\.com/ServerPacks/(betas/)?GT_New_Horizons_([0-9][0-9A-Za-z.\-]{0,38})_Server_Java_(8|17-[0-9]{2})\.zip"'
)
_DATE = re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b")
_FORGE_JAR = re.compile(r"^forge-1\.7\.10-([0-9][0-9.]*)-1\.7\.10-universal\.jar$")
_SERIES = re.compile(r"^(\d+)\.(\d+)")
_BACKUPS_BLOCK = re.compile(r"(?ms)^backups\s*\{.*?^\}")
_BACKUPS_ENABLED = re.compile(r"(?m)^(\s*B:enable_backups\s*=\s*)true\b")


@dataclass(frozen=True)
class Release:
    """One version of GTNH as the version history lists it."""

    version: str
    kind: str  # release | beta (betas and release candidates)
    published: str  # "2025-12-23"; empty when the page does not say
    modern: str | None  # the address of the server zip for Java 17 and newer
    java_max: int | None  # the newest Java that zip is made for ("17-25")
    legacy: str | None  # the address of the server zip for Java 8

    def url(self, variant: str) -> str:
        found = self.modern if variant == "modern" else self.legacy
        if not found:
            raise PackError(f"GT New Horizons {self.version} has no server files for {VARIANT_NAMES.get(variant, variant)}.")
        return found

    def builds(self) -> list[dict[str, Any]]:
        """The builds of the server files this version has, with the Java each of them would run on."""
        found = []
        if self.modern:
            found.append({"variant": "modern", "name": VARIANT_NAMES["modern"], "runs_on": _runs_on("modern", self.java_max)})
        if self.legacy:
            found.append({"variant": "legacy", "name": VARIANT_NAMES["legacy"], "runs_on": 8})
        return found

    def public(self) -> dict[str, Any]:
        """In the shape the dashboard shows versions in (the same as for the packs of Modrinth and CurseForge)."""
        return {
            "id": self.version, "number": self.version, "type": self.kind, "published": self.published or None, "minecraft": [MINECRAFT],
            "loaders": ["forge"], "server_pack": True, "size": 0, "builds": self.builds(),
        }


def _scan(blocks: list[str]) -> list[Release]:
    found: dict[str, dict[str, Any]] = {}
    for block in blocks:
        links = list(_SERVER_LINK.finditer(block))
        versions = {m.group(2) for m in links}
        summary = _SUMMARY.search(block)
        date = _DATE.search(summary.group(0)) if summary and len(versions) == 1 else None  # only when the block is about one version
        for m in links:
            beta, version, variant = bool(m.group(1)), m.group(2), m.group(3)
            entry = found.setdefault(version, {"kind": "release", "published": "", "modern": None, "java_max": None, "legacy": None})
            if beta:
                entry["kind"] = "beta"
            if date and not entry["published"]:
                entry["published"] = f"{date.group(1)}-{date.group(2)}-{date.group(3)}"
            url = "https://downloads.gtnewhorizons.com/ServerPacks/" + ("betas/" if beta else "") + f"GT_New_Horizons_{version}_Server_Java_{variant}.zip"
            if variant == "8":
                entry["legacy"] = url
            else:
                entry["modern"], entry["java_max"] = url, int(variant[3:])
    return [Release(version=v, **entry) for v, entry in found.items()]


def parse_history(page: str) -> list[Release]:
    """The versions of the version history page, newest first (the order of the page). Only the server zips matter, and only
    addresses on downloads.gtnewhorizons.com: everything else on the page is ignored. Each version is a folding block with its
    date and its links; when the page is built differently, the links alone still give the versions (without dates)."""
    for blocks in (_BLOCK.findall(page), [page]):
        releases = _scan(blocks)
        if releases:
            return releases
    return []


def pick_variant(release: Release, wanted: str | None) -> str:
    """The build to install: the one asked for, otherwise the one for Java 17 and newer (what GTNH recommends)."""
    if not wanted:
        wanted = "modern" if release.modern else "legacy"
    if wanted not in VARIANTS:
        raise PackError("Choose the server files for Java 17 and newer, or the ones for Java 8.")
    release.url(wanted)  # says what is missing
    return wanted


def java_for(variant: str | None, java_max: int | None) -> int:
    """The Java a build runs on: 8 for the Java 8 build, otherwise the newest Java we can install that the build is made for."""
    if variant == "legacy":
        return 8
    usable = [j for j in AVAILABLE_JAVA if 17 <= j <= (java_max or 21)]
    if not usable:
        raise PackError(f"These server files are made for Java 17 to {java_max}, which this manager cannot install.")
    return max(usable)


def _runs_on(variant: str, java_max: int | None) -> int | None:
    try:
        return java_for(variant, java_max)
    except PackError:
        return None


def forge_jar_of(pack: ServerPack) -> tuple[str, str] | None:
    """(file name, Forge version) of the Forge jar in the server files, which sits in the top folder."""
    for rel, _ in pack.entries:
        found = _FORGE_JAR.match(rel)
        if found:
            return rel, found.group(1)
    return None


def launch_of(pack: ServerPack, variant: str) -> dict[str, Any]:
    """How the server files start: what the pack's own start scripts run, without the memory options (the manager sets those)."""
    names = {rel for rel, _ in pack.entries}
    if variant == "modern":
        if LAUNCHER not in names or ARGUMENTS not in names:
            raise PackError(f"These server files for Java 17 and newer hold no {LAUNCHER} and {ARGUMENTS}, so the manager does not know how to start them.")
        return {"jar": LAUNCHER, "before": [FML_TIMEOUT, f"@{ARGUMENTS}"]}
    forge = forge_jar_of(pack)
    if forge is None:
        raise PackError("These server files for Java 8 hold no Forge jar for Minecraft 1.7.10, so the manager does not know how to start them.")
    return {"jar": forge[0], "before": [FML_TIMEOUT, *LEGACY_FLAGS]}


def pack_wins(rel: str) -> bool:
    """Which files an update replaces by the new version's even when they were changed: GTNH's update guide tells to replace the
    whole configuration and the libraries ("do not just copy old configs over"). The mods' own rewriting of their configuration
    makes such files differ from what the pack shipped without anybody having meant to."""
    if rel.startswith(KEEP_CONFIG):
        return False
    if rel.startswith(("config/", "libraries/")):
        return True
    return "/" not in rel and (rel.endswith(".jar") or rel == ARGUMENTS)


def build_index(release: Release, pack: ServerPack) -> PackIndex:
    forge = forge_jar_of(pack)
    index = PackIndex(
        name=TITLE, version_id=release.version, summary=SUMMARY, minecraft=MINECRAFT, loader=LoaderSpec("forge", forge[1] if forge else FORGE, MINECRAFT),
        source="gtnh", override_prefixes=(), archive=pack, archive_notes=dict(pack.skipped),
    )
    index.archive_replace = pack_wins
    return index


def pack_info(release: Release, variant: str) -> dict[str, Any]:
    """What the server remembers about the pack it was made from."""
    return {
        "source": "gtnh", "project": PROJECT, "slug": SLUG, "title": TITLE, "icon": None, "version_id": release.version,
        "version_number": release.version, "published": release.published or None, "version_type": release.kind, "server_pack": True,
        "variant": variant, "java_max": release.java_max,
    }


def switch_off_own_backups(directory: Path) -> bool:
    """ServerUtilities, a mod of the pack, backs the world up every half hour while players are online and keeps twelve of them in
    the server folder. The manager looks after backups itself, so that the world is not copied twelve more times: its ``enable_backups``
    is switched off (``backups/`` stays out of the manager's backups either way). The lock learns the file's new content, so that a
    later update does not take the manager's own change for somebody's. Returns whether it changed anything."""
    path = directory / SERVERUTILITIES_CONFIG
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    block = _BACKUPS_BLOCK.search(text)
    if block is None:
        return False
    changed, count = _BACKUPS_ENABLED.subn(r"\1false", block.group(0), count=1)
    if not count:
        return False
    patched = (text[: block.start()] + changed + text[block.end():]).encode("utf-8")
    path.write_bytes(patched)
    lock_path = directory / LOCK_DIR / LOCK_NAME
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        if SERVERUTILITIES_CONFIG in (lock.get("overrides") or {}):
            lock["overrides"][SERVERUTILITIES_CONFIG] = hashlib.sha256(patched).hexdigest()
            atomic_write_text(lock_path, json.dumps(lock, indent=1))
    except (OSError, ValueError):
        pass
    return True


def jump_warning(old: str, new: str) -> str | None:
    """GTNH advises updating one major version at a time (2.7 to 2.8, not 2.6 to 2.8); an older version usually cannot open the world."""
    a, b = _SERIES.match(old), _SERIES.match(new)
    if not a or not b:
        return None
    before, after = (int(a.group(1)), int(a.group(2))), (int(b.group(1)), int(b.group(2)))
    if after < before:
        return (
            f"GT New Horizons {new} is older than {old}. A world that a newer version has opened usually cannot be loaded by an older one; "
            "the backup made now is the way back."
        )
    if after[0] > before[0] or after[1] - before[1] > 1:
        return (
            "GT New Horizons advises updating one major version at a time (2.7 to 2.8, not 2.6 to 2.8): the versions in between have "
            "changes that only work step by step. The backup made now is the way back."
        )
    return None


class GtnhCatalog:
    """What the version history page lists, and the downloads it leads to."""

    def __init__(self, client: httpx.AsyncClient, retry_delay: float = 1.5, clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._delay = retry_delay
        self._clock = clock
        self._cached: tuple[float, list[Release]] | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cached = None

    async def releases(self) -> list[Release]:
        """Every version with server files, newest first."""
        if self._cached and self._clock() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        async with self._lock:
            if self._cached and self._clock() - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
            page = await get_text(self._client, HISTORY_URL, what="the GT New Horizons website", delay=self._delay, max_bytes=MAX_PAGE_BYTES)
            releases = parse_history(page)
            if not releases:
                raise DownloadError(
                    "The GT New Horizons website lists no server downloads (the page may have changed), so the pack cannot be installed from here right now."
                )
            self._cached = (self._clock(), releases)
            return releases

    async def release(self, version: str | None = None) -> Release:
        """One version, or the newest stable one when none is named. A version the list does not know may have been published since it
        was read: the page is read again then (when the list is more than a minute old) before the version is called unknown."""
        releases = await self.releases()
        if not version:
            return next((r for r in releases if r.kind == "release"), releases[0])
        found = next((r for r in releases if r.version == str(version)), None)
        if found is None and self._cached and self._clock() - self._cached[0] > RECHECK_SECONDS:
            self.invalidate()
            found = next((r for r in await self.releases() if r.version == str(version)), None)
        if found is None:
            raise PackError("That version of GT New Horizons does not exist (any more).")
        return found

    async def project(self) -> dict[str, Any]:
        """The pack in the shape the dashboard shows a modpack and its versions in."""
        return {
            "id": PROJECT, "slug": SLUG, "title": TITLE, "description": SUMMARY, "icon_url": None, "downloads": 0, "source": "gtnh", "page": SITE,
            "versions": [r.public() for r in await self.releases()],
        }

    async def download(self, release: Release, variant: str, dest: Path, progress: ProgressCallback | None = None) -> None:
        """Download one build of the server files. Nothing but the pack's download host is ever asked, redirects included."""
        url = release.url(variant)
        require_https(url, DOWNLOAD_HOSTS)
        await download(
            self._client, url, dest, allowed_hosts=DOWNLOAD_HOSTS, what=f"GT New Horizons {release.version}", max_size=MAX_DOWNLOAD_BYTES,
            progress=progress, delay=self._delay, headers_for=lambda _address: {},  # redirects are followed by hand, host by host
        )
