"""Java runtimes: Temurin JRE builds from Adoptium, downloaded when a server needs one and shared by all Java servers.

Each Java major version lives in ``<data>/java/<major>/``. Keeping them out of the container image keeps the image
small for people who only run Pumpkin, and lets a server get exactly the Java its Minecraft version asks for.

Java 8 is the exception to "the newest build": it stays on 8u312 (see ``PINS``). Temurin never built that for Alpine, so the
container gets the same version from Azul.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import tarfile
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..util import dir_size, free_bytes, iso, plain_environment
from .net import BadDownloadError, DownloadError, ProgressCallback, download, get_json, new_client

log = logging.getLogger(__name__)

API_URL = "https://api.adoptium.net/v3"
AZUL_API_URL = "https://api.azul.com/metadata/v1/zulu/packages/"
META_FILE = ".manager-runtime.json"
REPLACE_RETRY_SECONDS = 600.0  # after a pinned build could not be put in place, wait this long before trying again


@dataclass(frozen=True)
class Pin:
    """A Java major version that stays on one exact build instead of following the newest one."""

    id: str  # recorded with the installed runtime: one installed under another pin (or none) is replaced by the next ``ensure``
    adoptium: str  # the release at Adoptium, tried first
    azul: str  # the version at Azul, for the platforms Adoptium has no such build for (Alpine)


# Forge for Minecraft 1.16.5 up to 36.2.25, and every modpack built on it, cannot start on a Java 8 newer than 8u312: the January 2022
# security update (8u321) changed java.util.jar.ManifestEntryVerifier, which that Forge calls (NoSuchMethodError). Older Minecraft
# versions run on 8u312 just as well as on any newer Java 8, so all Java 8 servers share this one build.
PINS: dict[int, Pin] = {8: Pin("8u312", "jdk8u312-b07", "8.0.312")}


def pin_id(major: int) -> str:
    return PINS[major].id if major in PINS else ""


@dataclass
class RemoteJava:
    major: int
    release: str  # e.g. "jdk-21.0.12.1+1"
    name: str  # the archive's file name
    url: str
    sha256: str
    size: int
    size_is_exact: bool = True  # Azul lists sizes rounded up to the next hundred bytes: for those it is only a ceiling


@dataclass
class InstalledJava:
    major: int
    release: str
    path: Path
    java: Path
    size: int
    installed_at: str
    pin: str = ""  # the pin (``PINS``) that was in force when it was installed

    def public(self) -> dict[str, Any]:
        return {"major": self.major, "release": self.release, "size": self.size, "installed_at": self.installed_at}


class JavaSource(Protocol):
    async def latest(self, major: int) -> RemoteJava: ...

    async def download(self, remote: RemoteJava, dest: Path, progress: ProgressCallback | None = None) -> None: ...


def _is_musl() -> bool:
    return Path("/etc/alpine-release").exists() or any(Path("/lib").glob("ld-musl-*"))


def adoptium_platform(machine: str | None = None, *, system: str | None = None, musl: bool | None = None) -> tuple[str, str]:
    """(os, architecture) in the words of the Adoptium API for the machine we run on."""
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}.get((machine or platform.machine()).lower())
    if arch is None:
        raise DownloadError(f"There is no Java download for the CPU architecture '{machine or platform.machine()}'.")
    system = (system or ("windows" if os.name == "nt" else "linux")).lower()
    if system == "linux" and (_is_musl() if musl is None else musl):
        return "alpine-linux", arch
    return system, arch


class AdoptiumSource:
    def __init__(
        self, client: httpx.AsyncClient | None = None, os_name: str | None = None, arch: str | None = None, retry_delay: float = 1.5
    ):
        self._client = client or new_client()
        self._own_client = client is None
        self._delay = retry_delay
        detected = adoptium_platform() if os_name is None or arch is None else (os_name, arch)
        self.os_name, self.arch = os_name or detected[0], arch or detected[1]

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def latest(self, major: int) -> RemoteJava:
        data = await get_json(
            self._client, f"{API_URL}/assets/latest/{major}/hotspot",
            params={"architecture": self.arch, "image_type": "jre", "os": self.os_name}, what="the Java download service",
            delay=self._delay,
        )
        if not data:
            raise DownloadError(f"Adoptium has no Java {major} runtime for {self.os_name} on {self.arch}.")
        package = data[0]["binary"]["package"]
        return RemoteJava(
            major=major, release=str(data[0].get("release_name") or f"jdk-{major}"), name=package["name"], url=package["link"],
            sha256=str(package["checksum"]).lower(), size=int(package.get("size") or 0),
        )

    async def release(self, major: int, release_name: str) -> RemoteJava | None:
        """One exact release (``jdk8u312-b07``); None when Adoptium has no such build for this platform."""
        try:
            data = await get_json(
                self._client, f"{API_URL}/assets/release_name/eclipse/{release_name}",
                params={"architecture": self.arch, "image_type": "jre", "os": self.os_name, "jvm_impl": "hotspot"},
                what="the Java download service", delay=self._delay,
            )
        except DownloadError as exc:
            if exc.status == 404:
                return None
            raise
        binaries = (data or {}).get("binaries") or []
        if not binaries:
            return None
        package = binaries[0]["package"]
        return RemoteJava(
            major=major, release=str(data.get("release_name") or release_name), name=package["name"], url=package["link"],
            sha256=str(package["checksum"]).lower(), size=int(package.get("size") or 0),
        )

    async def download(self, remote: RemoteJava, dest: Path, progress: ProgressCallback | None = None) -> None:
        exact = remote.size_is_exact
        await download(
            self._client, remote.url, dest, size=(remote.size or None) if exact else None, max_size=None if exact else remote.size or None,
            sha256=remote.sha256, progress=progress, what=f"Java {remote.major}", attempts=3, delay=self._delay,
        )


class AzulSource:
    """Zulu JRE builds from Azul: the only place that still has Java 8u312 for Alpine (musl) Linux."""

    def __init__(self, client: httpx.AsyncClient, os_name: str, arch: str, retry_delay: float = 1.5):
        self._client = client
        self._delay = retry_delay
        self.os_name, self.arch = os_name, arch

    async def release(self, major: int, version: str) -> RemoteJava | None:
        """One exact Java version (``8.0.312``); None when Azul has no such build for this platform."""
        params = {
            "java_version": version, "os": {"alpine-linux": "linux-musl", "linux": "linux-glibc"}.get(self.os_name, self.os_name),
            "arch": self.arch, "java_package_type": "jre", "archive_type": "zip" if self.os_name == "windows" else "tar.gz",
            "release_status": "ga", "availability_types": "CA", "javafx_bundled": "false", "include_fields": "sha256_hash,size",
            "page_size": 20,
        }
        try:
            data = await get_json(self._client, AZUL_API_URL, params=params, what="Azul's Java download service", delay=self._delay)
        except DownloadError as exc:
            if exc.status == 404:
                return None
            raise
        packages = [p for p in data or [] if p.get("download_url") and p.get("sha256_hash") and p.get("name")]
        if not packages:
            return None
        best = max(packages, key=lambda p: p.get("distro_version") or [])
        release = "zulu" + ".".join(map(str, best.get("distro_version") or [])) + "-ca-jre" + ".".join(map(str, best.get("java_version") or []))
        return RemoteJava(
            major=major, release=release, name=best["name"], url=best["download_url"], sha256=str(best["sha256_hash"]).lower(),
            size=int(best.get("size") or 0), size_is_exact=False,
        )


class JavaDownloads:
    """Where runtimes come from: the newest Temurin build from Adoptium, or for a Java version in ``PINS`` the exact build named there
    (from Adoptium, and from Azul where Adoptium has none, which is the case for Alpine)."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, os_name: str | None = None, arch: str | None = None, retry_delay: float = 1.5
    ):
        self._client = client or new_client()
        self._own_client = client is None
        self.adoptium = AdoptiumSource(self._client, os_name, arch, retry_delay)
        self.azul = AzulSource(self._client, self.adoptium.os_name, self.adoptium.arch, retry_delay)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def latest(self, major: int) -> RemoteJava:
        pin = PINS.get(major)
        if pin is None:
            return await self.adoptium.latest(major)
        remote = await self.adoptium.release(major, pin.adoptium) or await self.azul.release(major, pin.azul)
        if remote is None:
            log.warning(
                "There is no Java %d build %s for %s on %s; the newest one is used instead.", major, pin.id, self.adoptium.os_name,
                self.adoptium.arch,
            )
            return await self.adoptium.latest(major)
        return remote

    async def download(self, remote: RemoteJava, dest: Path, progress: ProgressCallback | None = None) -> None:
        await self.adoptium.download(remote, dest, progress)  # a download is a download, whoever listed it


Verifier = Callable[[Path], Awaitable[str]]


async def run_java_version(java: Path) -> str:
    """Start the freshly installed Java once: a runtime that cannot run (missing system libraries) is found out now."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(java), "-version", env=plain_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
    except (OSError, TimeoutError) as exc:
        raise DownloadError(f"The downloaded Java does not start: {exc}") from exc
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise DownloadError(f"The downloaded Java does not start (exit code {proc.returncode}): {text[:300]}")
    return text.splitlines()[0] if text else "java"


def _safe_extract(archive: Path, target: Path) -> None:
    try:
        if archive.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(target, filter="data")  # refuses absolute paths, links that leave the folder and device files
        elif archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                root = target.resolve()
                for member in zf.namelist():
                    if not (root / member).resolve().is_relative_to(root):
                        raise BadDownloadError("The Java archive contains a path that leaves its folder, so it was refused.")
                zf.extractall(target)
        else:
            raise BadDownloadError(f"Unknown Java archive format: {archive.name}")
    except (tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise BadDownloadError(f"The Java archive is damaged or unsafe and was refused: {exc}") from exc


class JavaRuntimes:
    def __init__(
        self, base: Path, source_getter: Callable[[], JavaSource], verifier: Verifier | None = run_java_version,
        free_space: Callable[[Path], int] = free_bytes, in_use: Callable[[int], bool] | None = None,
    ):
        self.base = base
        self._source_getter = source_getter
        self._verify = verifier
        self._free_space = free_space
        self._in_use = in_use  # whether a server runs on this Java: a runtime is never swapped out from under a running server
        self._locks: dict[int, asyncio.Lock] = {}
        self._retry_at: dict[int, float] = {}
        base.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(base / ".partial", ignore_errors=True)  # leftovers of an interrupted download

    # ------------------------------------------------------------------ lookup
    def home(self, major: int) -> Path:
        return self.base / str(major)

    def executable(self, major: int) -> Path:
        return self.home(major) / "bin" / ("java.exe" if os.name == "nt" else "java")

    def get(self, major: int) -> InstalledJava | None:
        meta_path = self.home(major) / META_FILE
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not self.executable(major).is_file():
            return None
        return InstalledJava(
            major=major, release=str(meta.get("release", "")), path=self.home(major), java=self.executable(major),
            size=int(meta.get("size", 0)), installed_at=str(meta.get("installed_at", "")), pin=str(meta.get("pin", "")),
        )

    def installed(self) -> list[InstalledJava]:
        found = [self.get(int(p.name)) for p in sorted(self.base.iterdir()) if p.is_dir() and p.name.isdigit()]
        return sorted((j for j in found if j), key=lambda j: j.major)

    def disk_usage(self) -> int:
        return dir_size(self.base)

    # ----------------------------------------------------------------- install
    async def ensure(self, major: int, progress: ProgressCallback | None = None, *, force: bool = False) -> InstalledJava:
        """Make sure Java ``major`` is installed, downloading and checking it if needed.

        A runtime installed before the pin for its version existed (or under another pin) is replaced, unless a server runs on it or the
        replacement fails: servers must keep starting, on the Java they have, whatever the network does."""
        async with self._locks.setdefault(major, asyncio.Lock()):
            existing = self.get(major)
            if existing and not force:
                if existing.pin == pin_id(major):
                    return existing
                return await self._replace_outdated(existing, progress)
            return await self._install(major, progress)

    async def _replace_outdated(self, existing: InstalledJava, progress: ProgressCallback | None) -> InstalledJava:
        major = existing.major
        if time.monotonic() < self._retry_at.get(major, 0.0):
            return existing
        if self._in_use is not None and self._in_use(major):
            log.info("Java %d is not the pinned build %s, but a server runs on it; it is replaced when none does.", major, pin_id(major))
            return existing
        try:
            return await self._install(major, progress)
        except (DownloadError, OSError) as exc:
            self._retry_at[major] = time.monotonic() + REPLACE_RETRY_SECONDS
            log.warning("Java %d could not be replaced by the pinned build %s (%s); the installed one is used.", major, pin_id(major), exc)
            return existing

    async def _install(self, major: int, progress: ProgressCallback | None) -> InstalledJava:
        source = self._source_getter()
        remote = await source.latest(major)
        if self._free_space(self.base) < remote.size * 4 + 64 * 1024 * 1024:
            raise DownloadError(
                f"Not enough free disk space in {self.base} for Java {major} (about {(remote.size * 4) // (1024 * 1024) + 64} MiB needed)."
            )
        work = self.base / ".partial" / uuid.uuid4().hex
        work.mkdir(parents=True)
        try:
            archive = work / remote.name
            await source.download(remote, archive, progress)
            extracted = work / "extracted"
            extracted.mkdir()
            await asyncio.to_thread(_safe_extract, archive, extracted)
            children = [p for p in extracted.iterdir() if p.is_dir()]
            if len(children) != 1:
                raise BadDownloadError("The Java archive does not have the expected layout.")
            staged = children[0]
            java = staged / "bin" / ("java.exe" if os.name == "nt" else "java")
            if not java.is_file():
                raise BadDownloadError("The Java archive does not contain bin/java.")
            if self._verify:
                banner = await self._verify(java)
                log.info("Java %d installed: %s", major, banner)
            meta = {**asdict(remote), "size": await asyncio.to_thread(dir_size, staged), "installed_at": iso(), "pin": pin_id(major)}
            (staged / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
            self._swap_in(staged, self.home(major))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        installed = self.get(major)
        if installed is None:  # cannot happen unless the swap above failed silently
            raise DownloadError(f"Java {major} could not be installed.")
        return installed

    def _swap_in(self, staged: Path, final: Path) -> None:
        old = None
        if final.exists():
            old = self.base / f".old-{uuid.uuid4().hex}"
            os.replace(final, old)
        try:
            os.replace(staged, final)
        except OSError:
            if old is not None:
                os.replace(old, final)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)

    def remove(self, major: int) -> None:
        shutil.rmtree(self.home(major), ignore_errors=True)
        with contextlib.suppress(KeyError):
            del self._locks[major]
