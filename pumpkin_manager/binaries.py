"""Local cache of downloaded Pumpkin binaries, shared by all servers."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .models import BuildRecord
from .releases import BadBuildError, ProgressCallback, ReleaseError, ReleaseSource, RemoteBuild
from .repo import Repo
from .util import free_bytes

log = logging.getLogger(__name__)

_ELF_MAGIC = b"\x7fELF"


class BuildStore:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        source_getter: Callable[[], ReleaseSource],
        verify_elf: bool = True,
    ):
        self.settings = settings
        self.repo = repo
        self._source_getter = source_getter
        self.verify_elf = verify_elf
        self._locks: dict[str, asyncio.Lock] = {}
        settings.binaries_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_partials()

    # ------------------------------------------------------------------- paths
    def binary_path(self, build_id: str) -> Path:
        return self.settings.binaries_dir / build_id / "pumpkin"

    def is_available(self, build: BuildRecord | None) -> bool:
        if build is None:
            return False
        path = self.binary_path(build.id)
        try:
            return path.is_file() and (build.size == 0 or path.stat().st_size == build.size)
        except OSError:
            return False

    def _cleanup_partials(self) -> None:
        partial = self.settings.binaries_dir / ".partial"
        shutil.rmtree(partial, ignore_errors=True)

    # ---------------------------------------------------------------- registry
    def register(self, remote: RemoteBuild) -> BuildRecord:
        """Remember a build seen on GitHub (does not download anything)."""
        return self.repo.upsert_build(remote.as_row())

    # ---------------------------------------------------------------- download
    async def ensure(self, build: BuildRecord, progress: ProgressCallback | None = None) -> BuildRecord:
        """Make sure the binary for ``build`` is on disk, downloading and verifying it if needed."""
        lock = self._locks.setdefault(build.id, asyncio.Lock())
        async with lock:
            fresh = self.repo.get_build(build.id) or build
            if self.is_available(fresh):
                if not fresh.downloaded:
                    self.repo.mark_downloaded(fresh.id, str(self.binary_path(fresh.id)))
                return self.repo.get_build(fresh.id) or fresh

            needed = int(fresh.size * 1.15) + 64 * 1024 * 1024
            if free_bytes(self.settings.binaries_dir) < needed:
                raise ReleaseError(
                    f"Not enough free disk space in {self.settings.binaries_dir} to download this build "
                    f"(about {needed // (1024 * 1024)} MiB needed)."
                )

            partial_dir = self.settings.binaries_dir / ".partial"
            partial_dir.mkdir(parents=True, exist_ok=True)
            partial = partial_dir / f"{uuid.uuid4().hex}.part"
            try:
                await self._source_getter().download(fresh, partial, progress)
                self._sanity_check(partial)
                final_dir = self.settings.binaries_dir / fresh.id
                final_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(partial, 0o755)
                os.replace(partial, final_dir / "pumpkin")
            finally:
                partial.unlink(missing_ok=True)
            self.repo.mark_downloaded(fresh.id, str(self.binary_path(fresh.id)))
            log.info("Downloaded Pumpkin build %s", fresh.id)
            return self.repo.get_build(fresh.id) or fresh

    def _sanity_check(self, path: Path) -> None:
        if path.stat().st_size < 1024:
            raise BadBuildError("The downloaded file is far too small to be a Pumpkin binary.")
        if self.verify_elf:
            with open(path, "rb") as fh:
                if fh.read(4) != _ELF_MAGIC:
                    raise BadBuildError("The downloaded file is not a Linux executable (bad ELF header).")

    # ------------------------------------------------------------------ removal
    def delete_files(self, build_id: str) -> None:
        shutil.rmtree(self.settings.binaries_dir / build_id, ignore_errors=True)
        self.repo.mark_not_downloaded(build_id)

    def prune(self, keep_ids: set[str], keep_newest_per_channel: int) -> list[str]:
        """Delete cached binaries nobody needs. Returns the ids that were removed."""
        removed: list[str] = []
        builds = [b for b in self.repo.list_builds() if b.downloaded]
        newest_by_channel: dict[str, list[str]] = {}
        for build in builds:  # list_builds() is ordered newest first
            newest_by_channel.setdefault(build.channel, []).append(build.id)
        protected = set(keep_ids)
        for ids in newest_by_channel.values():
            protected.update(ids[:keep_newest_per_channel])
        for build in builds:
            if build.id not in protected:
                self.delete_files(build.id)
                removed.append(build.id)
        return removed

    def disk_usage(self) -> int:
        from .util import dir_size

        return dir_size(self.settings.binaries_dir)
