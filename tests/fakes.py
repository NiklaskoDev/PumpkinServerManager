"""Test doubles shared by the test-suite and the local dev runner (no pytest dependency)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pumpkin_manager.releases import ReleaseError, RemoteBuild

FAKE = Path(__file__).parent / "fake_pumpkin.py"
_BASE_TIME = datetime(2026, 10, 1, tzinfo=UTC)


class FakeReleaseSource:
    """Stands in for GitHub: tests publish builds and the manager discovers/downloads them."""

    def __init__(self) -> None:
        self._counter = 0
        self.latest_by_channel: dict[str, RemoteBuild] = {}
        self.history: list[RemoteBuild] = []
        self.payloads: dict[str, bytes] = {}
        self.offline = False
        self.fail_download = False
        self.downloads = 0

    def publish(self, channel: str, version: str, **behavior: Any) -> RemoteBuild:
        self._counter += 1
        content = json.dumps({"version": version, **behavior}).encode().ljust(4096, b" ")
        digest = hashlib.sha256(content).hexdigest()
        stamp = (_BASE_TIME + timedelta(minutes=self._counter)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rolling = channel in ("nightly", "canary")
        build = RemoteBuild(
            channel=channel, tag=channel if rolling else f"{version}+26.3-26.51", version=None if rolling else version,
            mc_version=None if rolling else "26.3", digest=digest, size=len(content), asset_name="pumpkin-X64-Linux-musl",
            asset_id=1000 + self._counter, download_url=f"fake://{digest}", release_url=f"https://example.invalid/{version}",
            published_at=stamp, updated_at=stamp, body=f"Release notes for {version}",
        )
        self.payloads[digest] = content
        self.latest_by_channel[channel] = build
        self.history.append(build)
        return build

    async def latest(self, channel: str) -> RemoteBuild:
        if self.offline:
            raise ReleaseError("Could not reach GitHub: simulated outage")
        if channel not in self.latest_by_channel:
            raise ReleaseError(f"No release published on {channel}")
        return self.latest_by_channel[channel]

    async def list_stable(self, limit: int = 15) -> list[RemoteBuild]:
        if self.offline:
            raise ReleaseError("Could not reach GitHub: simulated outage")
        return [b for b in reversed(self.history) if b.channel == "stable"][:limit]

    async def by_tag(self, tag: str, channel: str | None = None) -> RemoteBuild:
        if self.offline:
            raise ReleaseError("Could not reach GitHub: simulated outage")
        for build in reversed(self.history):
            if build.tag == tag:
                return build
        raise ReleaseError("GitHub could not find that release (HTTP 404).")

    async def download(self, build: Any, dest: Path, progress: Callable[[int, int], None] | None = None) -> None:
        if self.fail_download:
            raise ReleaseError("Download failed: simulated network error")
        self.downloads += 1
        content = self.payloads[build.digest]
        dest.write_bytes(content)
        if progress:
            progress(len(content), len(content))

    async def aclose(self) -> None:
        return None
