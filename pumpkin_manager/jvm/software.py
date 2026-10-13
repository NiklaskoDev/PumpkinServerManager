"""Server software the manager downloads, installs and updates by itself: Vanilla, Paper, Fabric, Spigot and BungeeCord.

A modpack brings its own mod loader and mods. Here there is nothing to unpack: the person picks the software and a Minecraft
version, the manager fetches the file from where its maker publishes it (Mojang, PaperMC, Fabric, SpigotMC), checks it against the
checksum that was published with it, and later offers newer builds. ``SoftwareCatalog`` knows what exists, ``SoftwareInstaller`` puts it
on disk. Spigot is the odd one out: it is not downloaded but built (see ``spigot.py``). BungeeCord is a proxy and has no Minecraft version.
"""

from __future__ import annotations

import logging
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..errors import UserError
from ..models import PROXY_KINDS, SOFTWARE_NAMES, software_label
from ..util import parse_iso
from .loaders import FABRIC_META, LoaderInstaller, LoaderSpec
from .modrinth import ModrinthClient
from .mojang import MojangMeta
from .net import BadDownloadError, DownloadError, ProgressCallback, download, get_json
from .spigot import BUNGEE_JAR_NAME, MAX_BUNGEE_BYTES, SPIGOT_JAR, SpigotBuilder, SpigotHub, java_floor
from .spigot import HOSTS as SPIGOT_HOSTS

log = logging.getLogger(__name__)

KINDS = tuple(SOFTWARE_NAMES)
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
MOJANG_HOSTS = frozenset({"piston-data.mojang.com", "launcher.mojang.com"})
PAPER_HOSTS = frozenset({"fill-data.papermc.io", "fill.papermc.io"})
DOWNLOAD_HOSTS = {"vanilla": MOJANG_HOSTS, "paper": PAPER_HOSTS, "bungeecord": SPIGOT_HOSTS}  # where a kind's jar may be fetched from
JAR_NAMES = {
    "vanilla": "server.jar", "paper": "paper.jar", "fabric": "fabric-server-launch.jar", "spigot": SPIGOT_JAR, "bungeecord": BUNGEE_JAR_NAME,
}
# BungeeCord's classes are made for Java 17 (its small launcher class for 11, so that it can say so). It runs on the current long-term
# release like everything else the manager starts, which keeps the number of runtimes down.
PROXY_JAVA = 21
PROXY_JAVA_MINIMUM = 17
FABRIC_API_PROJECT = "fabric-api"  # nearly every Fabric mod needs it
# Mojang published no server for the first Minecraft releases; from 1.2.5 on there is one for every release.
NO_SERVER = frozenset({"1.0", "1.1", "1.2.1", "1.2.2", "1.2.3", "1.2.4"})
STABLE_CHANNELS = frozenset({"stable", "recommended", "release"})
MAX_LISTED_BUILDS = 60  # how many builds one answer lists (Paper has hundreds for a Minecraft version)

_RELEASE_RE = re.compile(r"^\d+(?:\.\d+){1,2}$")  # 1.21, 1.21.8, 26.3, 26.1.2: no snapshots, release candidates or pre-releases
_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")  # versions end up in download addresses
_JAR_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,120}\.jar$")
_ZIP_MAGIC = b"PK\x03\x04"


class SoftwareError(UserError):
    """The software or version asked for does not exist, or its maker's service could not be reached."""


def version_key(version: str) -> tuple[int, ...]:
    """A sort key for Minecraft and loader version numbers: 1.21 < 1.21.1 < 1.21.10 < 26.3."""
    numbers = [int(n) for n in re.findall(r"\d+", version.split("-")[0])]
    return tuple(numbers + [0] * (3 - len(numbers)))


def is_release(version: str) -> bool:
    return bool(_RELEASE_RE.match(version))


def check_version_name(version: Any, what: str = "version") -> str:
    if not isinstance(version, str) or not _NAME_RE.match(version) or ".." in version:
        raise SoftwareError(f"'{version}' is not a valid {what} number.")
    return version


def _upstream(exc: DownloadError, missing: str | None = None) -> SoftwareError:
    """A failed lookup as a message for the person: not found when the service says it does not know the version, else unreachable."""
    if missing and exc.status in (400, 404):
        return SoftwareError(missing, 404)
    return SoftwareError(str(exc), 502)


# ----------------------------------------------------------------------------------------------------------- what exists
@dataclass(frozen=True)
class Build:
    """One installable thing: a Minecraft server jar, a Paper or Spigot build, a Fabric loader for a Minecraft version, or a BungeeCord build."""

    kind: str  # vanilla | paper | fabric | spigot | bungeecord
    minecraft: str  # empty for a proxy (BungeeCord has no Minecraft version of its own)
    # Paper and BungeeCord: the build number; Spigot: SpigotMC's name for the build (a number, sometimes with a letter); Fabric: the loader
    # version; Vanilla: empty (the Minecraft version is all there is)
    build: str = ""
    channel: str = "stable"  # stable | beta | alpha for Paper builds, stable | unstable for Fabric loaders, release | snapshot for Vanilla
    published: str | None = None
    url: str | None = None  # Vanilla, Paper and BungeeCord: where the jar is; Fabric downloads through its own installer, Spigot is built
    size: int | None = None
    sha1: str | None = None
    sha256: str | None = None

    @property
    def stable(self) -> bool:
        return self.channel in STABLE_CHANNELS

    @property
    def label(self) -> str:
        return software_label({"kind": self.kind, "minecraft": self.minecraft, "build": self.build})

    @property
    def build_label(self) -> str:
        if not self.build:
            return ""
        return f"loader {self.build}" if self.kind == "fabric" else f"build {self.build}" if self.kind != "vanilla" else ""

    @property
    def identity(self) -> str:
        """What names this build for the 'do not offer it again' marker of a version that failed."""
        return f"{self.kind}:{self.minecraft}:{self.build}"

    def public(self) -> dict[str, Any]:
        return {
            "build": self.build, "label": self.build_label or self.minecraft, "channel": self.channel, "stable": self.stable,
            "published": self.published, "size": self.size,
        }


@dataclass(frozen=True)
class Options:
    """Everything one Minecraft version offers for one kind of software: its builds (newest first) and what the maker says about it."""

    kind: str
    minecraft: str
    builds: tuple[Build, ...]
    support: dict[str, Any] | None = None  # Paper: {"status": "SUPPORTED" | "UNSUPPORTED", "end": "2026-06-15"}
    flags: tuple[str, ...] = ()  # the Java options Paper recommends for this version
    java_minimum: int | None = None

    def stable_builds(self) -> list[Build]:
        return [b for b in self.builds if b.stable]

    def default(self) -> Build | None:
        """The build to install when nobody chose: the newest stable one, or the newest of all when nothing is stable yet."""
        return next(iter(self.stable_builds()), self.builds[0] if self.builds else None)

    def public(self, experimental: bool = False) -> dict[str, Any]:
        shown = list(self.builds if experimental else (self.stable_builds() or self.builds))[:MAX_LISTED_BUILDS]
        default = self.default()
        return {
            "kind": self.kind, "minecraft": self.minecraft, "builds": [b.public() for b in shown],
            "default": default.build if default else None, "stable_only": bool(default and default.stable),
            "has_experimental": len(self.stable_builds()) < len(self.builds), "support": self.support,
            "java_minimum": self.java_minimum, "recommended_flags": list(self.flags),
        }


class SoftwareCatalog:
    """What Mojang, PaperMC, Fabric and SpigotMC offer. Answers are remembered for a few minutes; a check for updates starts afresh."""

    def __init__(self, client: httpx.AsyncClient, mojang: MojangMeta, retry_delay: float = 1.5, cache_seconds: float = 300.0):
        self._client = client
        self.mojang = mojang
        self.hub = SpigotHub(client, retry_delay)
        self._delay = retry_delay
        self._ttl = cache_seconds
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    def invalidate(self) -> None:
        """Forget what was learned: the next questions are asked of Mojang, PaperMC, Fabric and SpigotMC again."""
        self._cache.clear()
        self.mojang.refresh()

    async def _cached(self, key: tuple[Any, ...], load: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        value = await load()
        self._cache[key] = (time.monotonic(), value)
        return value

    async def _json(self, url: str, what: str) -> Any:
        return await get_json(self._client, url, what=what, delay=self._delay)

    @staticmethod
    def check_kind(kind: Any) -> str:
        if kind not in KINDS:
            raise SoftwareError(f"Unknown server software '{kind}'. Choose one of: {', '.join(SOFTWARE_NAMES.values())}.")
        return str(kind)

    # ---------------------------------------------------------------------------------------- Minecraft versions
    async def versions(self, kind: str, *, snapshots: bool = False) -> list[dict[str, Any]]:
        """The Minecraft versions this software runs, newest first: ``{"id", "type" (release | snapshot), "published"}``.

        Snapshots are only the ones newer than the newest release: older ones are of no use to anybody."""
        self.check_kind(kind)
        return await self._cached(("versions", kind, snapshots), lambda: self._load_versions(kind, snapshots))  # type: ignore[no-any-return]

    async def _hub_index(self) -> list[tuple[str, str | None]]:
        """Every version SpigotMC has a file for, with when that file last changed (one question per few minutes)."""
        return await self._cached(("hub-index",), self.hub.versions)  # type: ignore[no-any-return]

    async def _load_versions(self, kind: str, snapshots: bool) -> list[dict[str, Any]]:
        if kind in PROXY_KINDS:
            return []  # a proxy has no Minecraft version of its own: it takes players of every version it knows
        try:
            manifest = await self.mojang.manifest()
            published = {entry["id"]: entry["published"] for entry in manifest}
            newest_release = next((e for e in manifest if e["type"] == "release"), None)

            def newer_than_release(version: str) -> bool:
                return bool(newest_release and (published.get(version) or "") > (newest_release["published"] or ""))

            if kind == "vanilla":
                found = [{"id": e["id"], "type": "release", "published": e["published"]} for e in manifest
                         if e["type"] == "release" and e["id"] not in NO_SERVER]
                if snapshots:
                    found += [{"id": e["id"], "type": "snapshot", "published": e["published"]} for e in manifest
                              if e["type"] == "snapshot" and newer_than_release(e["id"])]
                return sorted(found, key=lambda v: v["published"] or "", reverse=True)
            if kind == "paper":
                data = await self._json(PAPER_API, "PaperMC's download service")
                names = {v for group in (data.get("versions") or {}).values() for v in group if isinstance(v, str) and is_release(v)}
                return [{"id": v, "type": "release", "published": published.get(v)} for v in sorted(names, key=version_key, reverse=True)]
            if kind == "spigot":
                changed = {v: when for v, when in await self._hub_index() if is_release(v)}  # no snapshots: SpigotMC lists a few pre-releases
                return [{"id": v, "type": "release", "published": published.get(v) or changed[v]} for v in sorted(changed, key=version_key, reverse=True)]
            games = await self._json(f"{FABRIC_META}/versions/game", "Fabric's download service")
            found = []
            for game in games:
                version = game.get("version") if isinstance(game, dict) else None
                if not isinstance(version, str) or not _NAME_RE.match(version):
                    continue
                if game.get("stable") and is_release(version):
                    found.append({"id": version, "type": "release", "published": published.get(version)})
                elif snapshots and not game.get("stable") and newer_than_release(version) and version in published:
                    found.append({"id": version, "type": "snapshot", "published": published.get(version)})
            return sorted(found, key=lambda v: v["published"] or "", reverse=True)
        except DownloadError as exc:
            raise _upstream(exc) from exc

    async def published(self, minecraft: str) -> str | None:
        """When Mojang published a Minecraft version, if it is one Mojang lists."""
        try:
            return next((e["published"] for e in await self.mojang.manifest() if e["id"] == minecraft), None)
        except DownloadError:
            return None

    async def is_newer(self, candidate: str, current: str) -> bool:
        """Is Minecraft ``candidate`` newer than ``current``? Mojang's release dates decide, the numbers when it does not know one."""
        a, b = await self.published(candidate), await self.published(current)
        if a and b:
            return parse_iso(a) > parse_iso(b)
        return version_key(candidate) > version_key(current)

    # ---------------------------------------------------------------------------------------------------- builds
    async def options(self, kind: str, minecraft: str) -> Options:
        """The builds of one kind of software for one Minecraft version (the Minecraft version must exist for that software). A proxy has
        no Minecraft version: what is passed is ignored and its builds are all there is."""
        self.check_kind(kind)
        if kind in PROXY_KINDS:
            minecraft = ""
        else:
            check_version_name(minecraft, "Minecraft version")
        return await self._cached(("options", kind, minecraft), lambda: self._load_options(kind, minecraft))  # type: ignore[no-any-return]

    async def _load_options(self, kind: str, mc: str) -> Options:
        if kind == "vanilla":
            return await self._vanilla_options(mc)
        if kind == "paper":
            return await self._paper_options(mc)
        if kind == "spigot":
            return await self._spigot_options(mc)
        if kind == "bungeecord":
            return await self._bungeecord_options()
        return await self._fabric_options(mc)

    async def _spigot_options(self, mc: str) -> Options:
        """Spigot has one build for a Minecraft version at a time: SpigotMC's file for it names the newest (there are no older ones to
        choose; BuildTools builds that one)."""
        missing = f"Spigot has no build for Minecraft {mc}."
        try:
            index = dict(await self._hub_index())
            if not is_release(mc) or mc not in index:  # not "latest" (a file of its own that BuildTools takes for the newest version), no pre-release
                raise SoftwareError(missing, 404)
            info = await self.hub.version_info(mc)
        except DownloadError as exc:
            raise _upstream(exc, missing) from exc
        name = str(info["name"]).strip()
        if not _NAME_RE.match(name) or ".." in name:
            raise SoftwareError(f"SpigotMC names its build for Minecraft {mc} '{name}', which cannot be used.", 502)
        return Options("spigot", mc, (Build("spigot", mc, name, "stable", index.get(mc)),), java_minimum=java_floor(info))

    async def _bungeecord_options(self) -> Options:
        try:
            listed = await self.hub.bungee_builds()
        except DownloadError as exc:
            raise _upstream(exc) from exc
        builds = tuple(
            Build("bungeecord", "", str(b["number"]), "stable", b["published"], url=SpigotHub.bungee_url(b["number"])) for b in listed
        )
        return Options("bungeecord", "", builds, java_minimum=PROXY_JAVA_MINIMUM)

    async def _vanilla_options(self, mc: str) -> Options:
        try:
            meta = await self.mojang.version_meta(mc)
        except DownloadError as exc:
            raise _upstream(exc, f"Mojang does not list Minecraft {mc}.") from exc
        server = (meta.get("downloads") or {}).get("server")
        if not isinstance(server, dict) or not server.get("url") or not server.get("sha1"):
            raise SoftwareError(f"Mojang published no server for Minecraft {mc}.", 404)
        java = (meta.get("javaVersion") or {}).get("majorVersion")
        build = Build(
            "vanilla", mc, "", "snapshot" if meta.get("type") == "snapshot" else "release", meta.get("releaseTime"),
            url=str(server["url"]), size=int(server.get("size") or 0) or None, sha1=str(server["sha1"]).lower(),
        )
        return Options("vanilla", mc, (build,), java_minimum=int(java) if isinstance(java, int) else None)

    async def _paper_options(self, mc: str) -> Options:
        base = f"{PAPER_API}/versions/{quote(mc)}"
        missing = f"Paper has no builds for Minecraft {mc}."
        try:
            detail = await self._json(base, "PaperMC's download service")
            listed = await self._json(f"{base}/builds", "PaperMC's download service")
        except DownloadError as exc:
            raise _upstream(exc, missing) from exc
        version = detail.get("version") if isinstance(detail, dict) else None
        version = version if isinstance(version, dict) else {}
        builds: list[Build] = []
        for entry in listed if isinstance(listed, list) else []:
            download_info = ((entry.get("downloads") or {}).get("server:default") or {}) if isinstance(entry, dict) else {}
            sha256 = (download_info.get("checksums") or {}).get("sha256")
            if not (download_info.get("url") and sha256 and isinstance(entry.get("id"), int)):
                continue
            builds.append(Build(
                "paper", mc, str(entry["id"]), str(entry.get("channel") or "alpha").lower(), entry.get("time"),
                url=str(download_info["url"]), size=int(download_info.get("size") or 0) or None, sha256=str(sha256).lower(),
            ))
        if not builds:
            raise SoftwareError(missing, 404)
        builds.sort(key=lambda b: int(b.build), reverse=True)
        java = version.get("java") if isinstance(version.get("java"), dict) else {}
        minimum = ((java.get("version") or {}) if isinstance(java.get("version"), dict) else {}).get("minimum")
        flags = [f for f in ((java.get("flags") or {}).get("recommended") or []) if isinstance(f, str) and re.fullmatch(r"-[A-Za-z0-9:=+._-]{1,100}", f)]
        support = version.get("support") if isinstance(version.get("support"), dict) else None
        return Options("paper", mc, tuple(builds), support=support, flags=tuple(flags), java_minimum=minimum if isinstance(minimum, int) else None)

    async def _fabric_options(self, mc: str) -> Options:
        try:
            listed = await self._json(f"{FABRIC_META}/versions/loader/{quote(mc)}", "Fabric's download service")
        except DownloadError as exc:
            raise _upstream(exc, f"Fabric does not support Minecraft {mc}. (It starts at 1.14.)") from exc
        builds = []
        for entry in listed if isinstance(listed, list) else []:
            loader = entry.get("loader") if isinstance(entry, dict) else None
            version = loader.get("version") if isinstance(loader, dict) else None
            if isinstance(version, str) and _NAME_RE.match(version):
                builds.append(Build("fabric", mc, version, "stable" if loader.get("stable") else "unstable"))
        if not builds:
            raise SoftwareError(f"Fabric does not support Minecraft {mc}. (It starts at 1.14.)", 404)
        return Options("fabric", mc, tuple(builds))  # Fabric lists its loaders newest first

    # ---------------------------------------------------------------------------------------------------- choosing
    async def recommended_minecraft(self, kind: str) -> str:
        """The Minecraft version to start with: the newest release that has a stable build of this software (none for a proxy)."""
        if kind in PROXY_KINDS:
            return ""
        listed = await self.versions(kind)
        if not listed:
            raise SoftwareError(f"No Minecraft version is available for {SOFTWARE_NAMES[kind]} right now.", 502)
        for entry in listed[:6]:
            try:
                if (await self.options(kind, entry["id"])).stable_builds():
                    return str(entry["id"])
            except SoftwareError:
                continue
        return str(listed[0]["id"])

    async def resolve(self, kind: str, minecraft: str | None = None, build: str | None = None) -> tuple[Build, Options]:
        """The build to install: the one asked for, or the newest stable one of the Minecraft version (the newest recommended one)."""
        self.check_kind(kind)
        mc = "" if kind in PROXY_KINDS else check_version_name(minecraft, "Minecraft version") if minecraft else await self.recommended_minecraft(kind)
        options = await self.options(kind, mc)
        if build and kind != "vanilla":  # Vanilla has exactly one build per Minecraft version
            found = next((b for b in options.builds if b.build == str(build)), None)
            if found is None and kind == "bungeecord" and str(build).isdigit() and int(build) > 0:
                # only the newest builds are listed; the address of any other one is the same kind of address (it is a 404 when gone)
                found = Build("bungeecord", "", str(int(build)), "stable", None, url=SpigotHub.bungee_url(build))
            if found is None:
                word = "loader version" if kind == "fabric" else "build"
                where = "" if kind in PROXY_KINDS else f" for Minecraft {mc}"
                newest = f" The one SpigotMC offers is {options.builds[0].build}." if kind == "spigot" else ""
                raise SoftwareError(f"{SOFTWARE_NAMES[kind]} has no {word} {build}{where}.{newest}", 404)
            return found, options
        chosen = options.default()
        if chosen is None:  # cannot happen: every Options has a build
            raise SoftwareError(f"{SOFTWARE_NAMES[kind]} has no build for Minecraft {mc}.", 404)
        return chosen, options

    async def newest_build(self, kind: str, minecraft: str) -> Build | None:
        """The newest stable build for the same Minecraft version, for update checks (Vanilla has no builds)."""
        if kind == "vanilla":
            return None
        return next(iter((await self.options(kind, minecraft)).stable_builds()), None)

    async def newer_minecraft(self, kind: str, current: str, *, snapshots: bool = False) -> dict[str, Any] | None:
        """The newest Minecraft version newer than ``current`` that this software has a stable build for, if any."""
        if kind in PROXY_KINDS:
            return None
        for entry in (await self.versions(kind, snapshots=snapshots))[:8]:
            if not await self.is_newer(entry["id"], current):
                return None  # newest first: nothing further down is newer either
            if kind == "vanilla":
                return entry
            try:
                if (await self.options(kind, entry["id"])).stable_builds():
                    return entry
            except SoftwareError:
                continue
        return None


# --------------------------------------------------------------------------------------------------------------- Fabric API
@dataclass(frozen=True)
class FabricApi:
    """The Fabric API mod for one Minecraft version, as Modrinth lists it."""

    version_id: str
    number: str
    release_type: str
    file: dict[str, Any]

    @property
    def filename(self) -> str:
        return str(self.file["filename"])

    def record(self) -> dict[str, Any]:
        """What the server remembers about it: enough to replace exactly this file later and nothing else."""
        return {
            "project": FABRIC_API_PROJECT, "version_id": self.version_id, "number": self.number, "type": self.release_type,
            "file": f"mods/{self.filename}", "sha1": (self.file.get("hashes") or {}).get("sha1"),
        }


class SoftwareInstaller:
    """Puts server software into a folder: a jar from Mojang, PaperMC or SpigotMC, a Fabric launcher, Fabric API for Fabric, and Spigot,
    which it has built."""

    def __init__(
        self, client: httpx.AsyncClient, loaders: LoaderInstaller, modrinth: ModrinthClient, retry_delay: float = 2.0,
        builder: SpigotBuilder | None = None,
    ):
        self._client = client
        self._loaders = loaders
        self._modrinth = modrinth
        self._delay = retry_delay
        self.builder = builder

    async def fetch(
        self, build: Build, directory: Path, java: Path, say: Callable[[str], None], progress: ProgressCallback | None = None, *,
        java_major: int | None = None, jdk_progress: ProgressCallback | None = None,
    ) -> dict[str, str]:
        """Install ``build`` into ``directory`` (a staging folder, or a server's own) and return how to start it. ``java_major`` is the Java
        the software runs on; Spigot is built with a development kit of that version (``jdk_progress`` hears it being downloaded), and
        ``progress`` hears how far the build is as ``(done, 1000)``."""
        if build.kind == "fabric":
            say(f"Installing Fabric loader {build.build} for Minecraft {build.minecraft}")
            return await self._loaders.install(LoaderSpec("fabric", build.build, build.minecraft), directory, java, say)
        if build.kind == "spigot":
            if self.builder is None or java_major is None:
                raise SoftwareError("Spigot cannot be built here.")
            target = await self.builder.build(build.minecraft, directory, java_major, say, progress, jdk_progress)
            return {"jar": target.name}
        if not build.url:
            raise SoftwareError(f"{build.label} has nothing to download.")
        hosts = DOWNLOAD_HOSTS[build.kind]
        target = directory / JAR_NAMES[build.kind]
        say(f"Downloading {build.label}")
        try:
            # The published size is only a ceiling: the checksum that came with the file is what proves it is the right one. (SpigotMC's
            # build server publishes none for BungeeCord: the address is its own, over https, and a cut-off file is told by its directory.)
            await download(
                self._client, build.url, target, max_size=build.size or (MAX_BUNGEE_BYTES if build.kind == "bungeecord" else None),
                sha1=build.sha1, sha256=build.sha256, progress=progress, allowed_hosts=hosts, what=build.label, delay=self._delay,
            )
            with open(target, "rb") as fh:
                if fh.read(4) != _ZIP_MAGIC:
                    raise BadDownloadError(f"What was downloaded for {build.label} is not a jar file.")
            if build.kind == "bungeecord":
                try:
                    with zipfile.ZipFile(target) as jar:
                        if "META-INF/MANIFEST.MF" not in jar.namelist():
                            raise BadDownloadError(f"What was downloaded for {build.label} is not a runnable jar file.")
                except zipfile.BadZipFile as exc:
                    raise BadDownloadError(f"What was downloaded for {build.label} is damaged ({exc}).") from exc
        except DownloadError:
            target.unlink(missing_ok=True)
            raise
        return {"jar": target.name}

    # ------------------------------------------------------------------------------------------------ Fabric API
    async def fabric_api(self, minecraft: str) -> FabricApi | None:
        """The Fabric API mod to install for a Minecraft version: its newest release, else its newest build of any kind. None if
        there is none (a very new Minecraft version often has to wait for it)."""
        try:
            versions = await self._modrinth.mod_versions(FABRIC_API_PROJECT, "fabric", minecraft)
        except DownloadError as exc:
            raise _upstream(exc) from exc
        ordered = sorted(versions, key=lambda v: v.get("version_type") != "release")  # releases first; the sort keeps newest-first within
        for version in ordered:
            files = [f for f in version.get("files") or [] if isinstance(f, dict) and _JAR_FILE_RE.match(str(f.get("filename", "")))]
            file = next((f for f in files if f.get("primary")), files[0] if files else None)
            if file and file.get("url") and (file.get("hashes") or {}).get("sha512") and isinstance(version.get("id"), str):
                return FabricApi(version["id"], str(version.get("version_number") or ""), str(version.get("version_type") or "release"), file)
        return None

    async def install_fabric_api(self, api: FabricApi, directory: Path, progress: ProgressCallback | None = None) -> dict[str, Any]:
        """Download Fabric API into the mods folder of ``directory`` and return the record to keep."""
        target = directory / "mods" / api.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._modrinth.download_file(api.file, target, progress, what=f"Fabric API {api.number}")
        except DownloadError:
            target.unlink(missing_ok=True)
            raise
        return api.record()
