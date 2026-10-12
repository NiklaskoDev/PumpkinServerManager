"""What Mojang says about a Minecraft version: mainly which Java it needs.

The answer comes from Mojang's own version metadata (``javaVersion.majorVersion``) and is remembered, because it never
changes for a released version. Without network (or for a version Mojang does not list) a table of what Minecraft has
needed so far is used instead.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

import httpx

from .net import DownloadError, get_json, new_client

log = logging.getLogger(__name__)

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
# Java versions that Temurin (Adoptium) ships as a JRE; Mojang names others too (16 for Minecraft 1.17).
AVAILABLE_JAVA = (8, 11, 17, 21, 25)


class KeyValueStore(Protocol):
    def get_kv(self, key: str) -> str | None: ...

    def set_kv(self, key: str, value: str) -> None: ...


def available_java(major: int) -> int:
    """The Java we can actually install for what Mojang asks for: the same major, or the next one up."""
    for candidate in AVAILABLE_JAVA:
        if candidate >= major:
            return candidate
    raise DownloadError(f"Minecraft asks for Java {major}, which this manager cannot install yet.")


def guess_java_major(mc_version: str) -> int:
    """What Minecraft versions have needed so far, for when Mojang's metadata cannot be reached."""
    numbers = [int(part) for part in re.findall(r"\d+", mc_version.split("-")[0])]
    if not numbers:
        return 21
    if numbers[0] >= 26:  # the year-based versions (26.1 and later)
        return 25
    minor = numbers[1] if len(numbers) > 1 else 0
    patch = numbers[2] if len(numbers) > 2 else 0
    if minor <= 16:
        return 8
    if minor < 20 or (minor == 20 and patch < 5):
        return 17
    return 21


class MojangMeta:
    def __init__(
        self, client: httpx.AsyncClient | None = None, store: KeyValueStore | None = None, manifest_ttl: float = 3600.0,
        retry_delay: float = 1.5,
    ):
        self._client = client or new_client()
        self._own_client = client is None
        self._store = store
        self._ttl = manifest_ttl
        self._delay = retry_delay
        self._manifest: tuple[float, dict[str, Any]] | None = None

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _versions(self) -> dict[str, Any]:
        if self._manifest and time.monotonic() - self._manifest[0] < self._ttl:
            return self._manifest[1]
        manifest = await get_json(self._client, MANIFEST_URL, what="Mojang's version list", delay=self._delay)
        by_id = {entry["id"]: entry for entry in manifest.get("versions", [])}
        self._manifest = (time.monotonic(), by_id)
        return by_id

    def refresh(self) -> None:
        """Forget the version list, so that the next question reads Mojang's current one (it is kept for an hour otherwise)."""
        self._manifest = None

    async def manifest(self) -> list[dict[str, Any]]:
        """Every Minecraft version Mojang lists, newest first: ``{"id", "type" (release | snapshot | old_*), "published"}``."""
        return [
            {"id": entry["id"], "type": entry.get("type", ""), "published": entry.get("releaseTime")}
            for entry in (await self._versions()).values()
        ]

    async def version_meta(self, mc_version: str) -> dict[str, Any]:
        """Mojang's data for one Minecraft version (downloads, Java version, ...)."""
        entry = (await self._versions()).get(mc_version)
        if entry is None:
            unknown = DownloadError(f"Mojang does not list Minecraft {mc_version}.")
            unknown.status = 404
            raise unknown
        meta = await get_json(self._client, entry["url"], what=f"Mojang's data for Minecraft {mc_version}", delay=self._delay)
        if not isinstance(meta, dict):
            raise DownloadError(f"Mojang's data for Minecraft {mc_version} could not be read.")
        return meta

    async def java_major(self, mc_version: str) -> int:
        """The Java major version this Minecraft version asks for (not yet mapped to what can be installed)."""
        key = f"mc-java:{mc_version}"
        cached = self._store.get_kv(key) if self._store else None
        if cached and cached.isdigit():
            return int(cached)
        try:
            entry = (await self._versions()).get(mc_version)
            if entry is None:
                raise DownloadError(f"Mojang does not list Minecraft {mc_version}.")
            meta = await get_json(self._client, entry["url"], what=f"Mojang's data for Minecraft {mc_version}", delay=self._delay)
        except DownloadError as exc:
            guess = guess_java_major(mc_version)
            log.warning("%s Assuming Java %d for Minecraft %s.", exc, guess, mc_version)
            return guess
        major = int((meta.get("javaVersion") or {}).get("majorVersion") or 8)  # versions before 1.17 do not say: Java 8
        if self._store:
            self._store.set_kv(key, str(major))
        return major
