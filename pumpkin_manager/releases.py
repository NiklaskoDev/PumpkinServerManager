"""Discovering and downloading Pumpkin builds from GitHub releases.

Pumpkin publishes three kinds of builds:

* ``stable``  - normal tagged releases (``0.2.0+26.3-26.51``), immutable.
* ``nightly`` and ``canary`` - *rolling* prereleases whose tag never changes; the binary behind the tag
  is replaced instead. A build is therefore identified by the SHA-256 digest GitHub reports for the
  asset, never by its tag alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import platform
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from . import __version__
from .errors import UserError

log = logging.getLogger(__name__)

ROLLING_TAGS = {"nightly": "nightly", "canary": "canary"}
CHECKSUMS_ASSET = "checksums.sha256"
_STABLE_TAG_RE = re.compile(
    r"^(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\+(?P<mc>[0-9][0-9.]*)-(?P<proto>[0-9][0-9.]*)$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ProgressCallback = Callable[[int, int], None]


class ReleaseError(UserError):
    """A problem talking to GitHub or with a release; the message is safe to show to users."""


class BadBuildError(ReleaseError):
    """The file itself is unusable (wrong format or checksum), so retrying the same build cannot help.

    Transient trouble (network, disk space) stays a plain ``ReleaseError``; the updater keeps retrying those with
    a growing delay but never retries a build that raised this.
    """


def asset_name_for_machine(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    if machine in {"x86_64", "amd64"}:
        return "pumpkin-X64-Linux-musl"
    if machine in {"aarch64", "arm64"}:
        return "pumpkin-ARM64-Linux-musl"
    raise ReleaseError(f"Unsupported CPU architecture '{machine}': Pumpkin ships x86_64 and arm64 Linux builds only.")


def build_id_for(tag: str, digest: str) -> str:
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag)
    return f"{safe_tag}-{digest.split(':')[-1][:12]}"


def parse_stable_tag(tag: str) -> tuple[str | None, str | None]:
    """Split ``0.2.0+26.3-26.51`` into (pumpkin version, minecraft version)."""
    match = _STABLE_TAG_RE.match(tag)
    return (match["version"], match["mc"]) if match else (None, None)


@dataclass
class RemoteBuild:
    channel: str
    tag: str
    version: str | None
    mc_version: str | None
    digest: str
    size: int
    asset_name: str
    asset_id: int | None
    download_url: str
    release_url: str | None
    published_at: str | None
    updated_at: str | None
    body: str | None

    @property
    def id(self) -> str:
        return build_id_for(self.tag, self.digest)

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["id"] = self.id
        return row

    @property
    def has_verifiable_digest(self) -> bool:
        return bool(_SHA256_RE.match(self.digest))


class ReleaseSource(Protocol):
    async def latest(self, channel: str) -> RemoteBuild: ...

    async def list_stable(self, limit: int = 15) -> list[RemoteBuild]: ...

    async def by_tag(self, tag: str) -> RemoteBuild: ...

    async def download(self, build: Any, dest: Path, progress: ProgressCallback | None = None) -> None: ...


class GitHubReleaseSource:
    def __init__(
        self,
        repo: str,
        api_url: str = "https://api.github.com",
        token_getter: Callable[[], str | None] = lambda: None,
        machine: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self._token_getter = token_getter
        self.asset_name = asset_name_for_machine(machine)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            headers={"User-Agent": f"pumpkin-server-manager/{__version__}"},
            follow_redirects=True,
        )
        self._etags: dict[str, tuple[str, Any]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # --------------------------------------------------------------- public API
    async def latest(self, channel: str) -> RemoteBuild:
        if channel in ROLLING_TAGS:
            return await self.by_tag(ROLLING_TAGS[channel], channel=channel)
        for release in await self._stable_releases():
            build = await self._to_build(release, "stable")
            if build:
                return build
        raise ReleaseError("No stable Pumpkin release with a Linux build was found on GitHub.")

    async def list_stable(self, limit: int = 15) -> list[RemoteBuild]:
        builds: list[RemoteBuild] = []
        for release in await self._stable_releases():
            build = await self._to_build(release, "stable")
            if build:
                builds.append(build)
            if len(builds) >= limit:
                break
        return builds

    async def by_tag(self, tag: str, channel: str | None = None) -> RemoteBuild:
        release = await self._get_json(f"/repos/{self.repo}/releases/tags/{quote(tag, safe='')}")
        build = await self._to_build(release, channel or ("stable" if tag not in ROLLING_TAGS else tag))
        if not build:
            raise ReleaseError(f"Release '{tag}' has no {self.asset_name} download.")
        return build

    async def download(self, build: Any, dest: Path, progress: ProgressCallback | None = None) -> None:
        """Stream ``build.download_url`` to ``dest``, verifying size and SHA-256 when known."""
        digest_expected = build.digest.split(":")[-1] if _SHA256_RE.match(build.digest.split(":")[-1]) else None
        hasher = hashlib.sha256()
        received = 0
        last_error: Exception | None = None
        for attempt in range(1, 4):
            hasher = hashlib.sha256()
            received = 0
            try:
                # The API token is deliberately not sent: release downloads are public and the redirect
                # target (object storage) rejects foreign Authorization headers.
                async with self._client.stream("GET", build.download_url, timeout=httpx.Timeout(60.0, connect=15.0)) as resp:
                    if resp.status_code != 200:
                        raise ReleaseError(f"Download failed: GitHub answered HTTP {resp.status_code}.")
                    total = int(resp.headers.get("content-length") or build.size or 0)
                    with open(dest, "wb") as fh:
                        async for chunk in resp.aiter_bytes(1 << 20):
                            await asyncio.to_thread(fh.write, chunk)
                            hasher.update(chunk)
                            received += len(chunk)
                            if progress:
                                progress(received, total or build.size)
                last_error = None
                break
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                log.warning("Download attempt %d/3 failed: %s", attempt, exc)
                await asyncio.sleep(2 * attempt)
        if last_error is not None:
            raise ReleaseError(f"Download failed: {last_error}") from last_error
        if build.size and received != build.size:
            raise ReleaseError(f"Download was incomplete ({received} of {build.size} bytes).")
        if digest_expected and hasher.hexdigest() != digest_expected:
            raise BadBuildError("Checksum mismatch: the downloaded file does not match the SHA-256 published by GitHub.")

    # ----------------------------------------------------------------- internals
    async def _stable_releases(self) -> list[dict[str, Any]]:
        data = await self._get_json(f"/repos/{self.repo}/releases?per_page=30")
        stable = [
            r for r in data
            if not r.get("draft") and not r.get("prerelease") and r.get("tag_name") not in ROLLING_TAGS.values()
        ]
        stable.sort(key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
        return stable

    async def _to_build(self, release: dict[str, Any], channel: str) -> RemoteBuild | None:
        tag = release["tag_name"]
        asset = next(
            (a for a in release.get("assets", []) if a["name"] == self.asset_name and a.get("state", "uploaded") == "uploaded"),
            None,
        )
        if asset is None:
            return None  # assets are uploaded after the release is published; treat it as "not ready yet"

        digest = (asset.get("digest") or "").split(":", 1)[-1].lower()
        if not _SHA256_RE.match(digest):
            digest = await self._digest_from_checksums(release, self.asset_name) or ""
        if not digest:
            seed = f"{asset['id']}:{asset.get('updated_at')}:{asset.get('size')}"
            digest = "unverified-" + hashlib.sha1(seed.encode()).hexdigest()  # identity only, nothing to verify

        version, mc_version = parse_stable_tag(tag) if channel == "stable" else (None, None)
        return RemoteBuild(
            channel=channel,
            tag=tag,
            version=version,
            mc_version=mc_version,
            digest=digest,
            size=int(asset.get("size") or 0),
            asset_name=asset["name"],
            asset_id=asset.get("id"),
            download_url=asset["browser_download_url"],
            release_url=release.get("html_url"),
            published_at=release.get("published_at") or release.get("created_at"),
            updated_at=asset.get("updated_at") or release.get("updated_at"),
            body=(release.get("body") or "")[:6000] or None,
        )

    async def _digest_from_checksums(self, release: dict[str, Any], asset_name: str) -> str | None:
        sums = next((a for a in release.get("assets", []) if a["name"] == CHECKSUMS_ASSET), None)
        if not sums:
            return None
        try:
            resp = await self._client.get(sums["browser_download_url"])
            resp.raise_for_status()
        except httpx.HTTPError:
            return None
        for line in resp.text.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1].lstrip("*") == asset_name and _SHA256_RE.match(parts[0].lower()):
                return parts[0].lower()
        return None

    async def _get_json(self, path: str) -> Any:
        url = f"{self.api_url}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = self._token_getter()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        cached = self._etags.get(url)
        if cached:
            headers["If-None-Match"] = cached[0]

        resp: httpx.Response | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise ReleaseError(f"Could not reach GitHub: {exc or exc.__class__.__name__}") from exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
        assert resp is not None

        if resp.status_code == 304 and cached:
            return cached[1]
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset", "")
            hint = " Add a GitHub token in Settings (or the GITHUB_TOKEN variable) to raise the limit."
            raise ReleaseError(f"GitHub API rate limit reached (resets at unix time {reset}).{hint}")
        if resp.status_code == 404:
            raise ReleaseError("GitHub could not find that release (HTTP 404).")
        if resp.status_code == 401:
            raise ReleaseError("GitHub rejected the configured token (HTTP 401). Check or remove it in Settings.")
        if resp.status_code != 200:
            raise ReleaseError(f"GitHub answered HTTP {resp.status_code} while looking up releases.")
        data = resp.json()
        if etag := resp.headers.get("etag"):
            self._etags[url] = (etag, data)
        return data


SourceFactory = Callable[[], Awaitable[ReleaseSource]]
