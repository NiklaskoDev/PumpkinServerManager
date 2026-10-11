"""Backups: verified tar.gz archives of a server directory, retention, and safe restore."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import uuid
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .errors import UserError
from .jobs import JobContext, JobService
from .mcnet import MsmpClient, MsmpError
from .models import ServerRecord
from .process import ProcessError, ProcessManager
from .repo import Repo, new_id
from .reporter import Reporter
from .util import dir_size, free_bytes, human_bytes, iso, utcnow

log = logging.getLogger(__name__)

MANIFEST_NAME = ".pumpkin-manager/manifest.json"
KINDS = ("manual", "scheduled", "pre-update", "pre-restore", "pre-delete", "uploaded", "imported")
COLD_KINDS = {"pre-update", "pre-restore", "pre-delete"}
PROGRESS_STEP_BYTES = 8 * 1024 * 1024
# Never part of a backup, whatever the server's own exclusions say: Minecraft recreates session.lock at every start, and Java
# holds it with a mandatory lock on Windows, so a running server's copy cannot even be read there.
ALWAYS_EXCLUDED = ("session.lock",)
# What a Java server prints when it has done what the backup asked of it (vanilla and modded alike).
_SAVE_OFF_RE = re.compile(r"Automatic saving is now disabled", re.IGNORECASE)
_SAVED_RE = re.compile(r"Saved the (game|world)", re.IGNORECASE)


class BackupError(UserError):
    """A backup/restore problem whose message is safe to show to users."""


# ----------------------------------------------------------------------- archive IO


class _HashingWriter:
    def __init__(self, fh: Any):
        self.fh = fh
        self.sha = hashlib.sha256()
        self.written = 0

    def write(self, data: bytes) -> int:
        self.sha.update(data)
        self.written += len(data)
        return self.fh.write(data)

    def flush(self) -> None:
        self.fh.flush()


class _HashingReader:
    def __init__(self, fh: Any):
        self.fh = fh
        self.sha = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self.fh.read(n)
        self.sha.update(data)
        return data


class _FixedSizeReader:
    """Yields exactly ``size`` bytes even if the file changes while it is archived.

    A tar stream is only valid if every member has the size announced in its header, so a file that
    shrinks mid-read is zero-padded (and reported) instead of corrupting everything after it.
    """

    def __init__(self, fh: Any, size: int):
        self.fh = fh
        self.remaining = size
        self.short = False

    def read(self, n: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        n = self.remaining if n is None or n < 0 else min(n, self.remaining)
        data = self.fh.read(n)
        if len(data) < n:
            self.short = True
            data += b"\0" * (n - len(data))
        self.remaining -= len(data)
        return data


def _is_excluded(rel: str, patterns: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        base = pattern.rstrip("/")
        if rel == base or rel.startswith(base + "/") or fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def write_archive(
    src: Path,
    dest: Path,
    manifest: dict[str, Any],
    excludes: list[str],
    on_progress: Callable[[int], None],
) -> dict[str, Any]:
    """Create ``dest`` (gzip-compressed tar) from ``src``. Runs in a worker thread."""
    files = 0
    done_bytes = 0
    next_report = PROGRESS_STEP_BYTES
    changed: list[str] = []
    skipped: list[str] = []
    with open(dest, "wb") as raw:
        writer = _HashingWriter(raw)
        with gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=6, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                payload = json.dumps(manifest, indent=2).encode("utf-8")
                info = tarfile.TarInfo(MANIFEST_NAME)
                info.size, info.mtime, info.mode = len(payload), int(utcnow().timestamp()), 0o644
                tar.addfile(info, io.BytesIO(payload))

                for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
                    rel_dir = Path(dirpath).relative_to(src).as_posix()
                    rel_dir = "" if rel_dir == "." else rel_dir
                    keep_dirs = []
                    for d in sorted(dirnames):
                        rel = f"{rel_dir}/{d}".lstrip("/")
                        full = Path(dirpath) / d
                        if full.is_symlink():
                            skipped.append(rel)
                        elif not _is_excluded(rel, excludes):
                            keep_dirs.append(d)
                            tar.add(full, arcname=rel, recursive=False)
                    dirnames[:] = keep_dirs

                    for name in sorted(filenames):
                        rel = f"{rel_dir}/{name}".lstrip("/")
                        full = Path(dirpath) / name
                        if _is_excluded(rel, excludes):
                            continue
                        try:
                            st = full.lstat()
                        except OSError:
                            skipped.append(rel)
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            skipped.append(rel)  # symlinks, sockets, fifos
                            continue
                        tinfo = tar.gettarinfo(str(full), arcname=rel)
                        try:
                            with open(full, "rb") as fh:
                                reader = _FixedSizeReader(fh, tinfo.size)
                                tar.addfile(tinfo, reader)
                        except OSError:
                            skipped.append(rel)
                            raise BackupError(f"Could not read {rel} while creating the backup.") from None
                        try:
                            after = full.lstat()
                            if reader.short or after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
                                changed.append(rel)
                        except OSError:
                            changed.append(rel)
                        files += 1
                        done_bytes += tinfo.size
                        if done_bytes >= next_report:
                            next_report = done_bytes + PROGRESS_STEP_BYTES
                            on_progress(done_bytes)
        raw.flush()
        os.fsync(raw.fileno())
    return {
        "files": files, "bytes": done_bytes, "sha256": writer.sha.hexdigest(), "size": writer.written,
        "changed": changed, "skipped": skipped,
    }


@contextlib.contextmanager
def _tar_stream(fileobj: Any):
    """Sequential tar reader over a gzip layer.

    ``tarfile``'s own ``r|gz`` mode never checks the gzip trailer, but ``GzipFile`` does (CRC and
    length), so reading the stream to its end through it detects truncated or corrupted archives.
    """
    with gzip.GzipFile(fileobj=fileobj, mode="rb") as gz:
        with tarfile.open(fileobj=gz, mode="r|") as tar:
            yield tar, gz


_READ_ERRORS = (tarfile.TarError, OSError, EOFError, ValueError, gzip.BadGzipFile, zlib.error)


def verify_archive(path: Path, expected_sha256: str | None) -> dict[str, Any]:
    """Read the whole archive: gzip CRC, tar structure, manifest present, and the checksum."""
    members = 0
    manifest: dict[str, Any] | None = None
    try:
        with open(path, "rb") as raw:
            reader = _HashingReader(raw)
            with _tar_stream(reader) as (tar, gz):
                for member in tar:
                    members += 1
                    if member.isfile():
                        fh = tar.extractfile(member)
                        if member.name == MANIFEST_NAME and fh is not None:
                            manifest = json.loads(fh.read(1 << 20).decode("utf-8"))
                        elif fh is not None:
                            while fh.read(1 << 20):
                                pass
                while gz.read(1 << 20):  # drain to the end so the gzip trailer is checked
                    pass
            while reader.read(1 << 20):
                pass
    except _READ_ERRORS as exc:
        raise BackupError(f"The archive failed verification: {exc}") from exc
    if members == 0:
        raise BackupError("The archive is empty.")
    if expected_sha256 and reader.sha.hexdigest() != expected_sha256:
        raise BackupError("The archive checksum changed after it was written.")
    return {"members": members, "manifest": manifest, "sha256": reader.sha.hexdigest()}


def _extract_filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    if member.name.startswith(".pumpkin-manager"):
        return None
    if not (member.isfile() or member.isdir()):
        return None  # links, devices and fifos are never restored
    return tarfile.data_filter(member, dest)


def extract_archive(path: Path, dest: Path, byte_limit: int) -> int:
    """Safely extract ``path`` into the (empty) directory ``dest``. Returns bytes written."""
    total = 0
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "rb") as raw, _tar_stream(raw) as (tar, gz):
            for member in tar:
                if member.isfile():
                    total += member.size
                    if total > byte_limit:
                        raise BackupError("Not enough free disk space to restore this backup.")
                tar.extract(member, path=dest, filter=_extract_filter)
            while gz.read(1 << 20):
                pass
    except _READ_ERRORS as exc:
        raise BackupError(f"The backup could not be extracted: {exc}") from exc
    return total


def read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as raw, _tar_stream(raw) as (tar, _gz):
            for member in tar:
                if member.name == MANIFEST_NAME and member.isfile():
                    fh = tar.extractfile(member)
                    return json.loads(fh.read(1 << 20).decode("utf-8")) if fh else None
                break  # the manifest is always the first member
    except _READ_ERRORS:
        return None
    return None


# --------------------------------------------------------------------------- service


class BackupService:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        pm: ProcessManager,
        reporter: Reporter,
        jobs: JobService,
        start_server: Callable[[ServerRecord, JobContext], Any],
        stop_server: Callable[[ServerRecord, JobContext], Any],
    ):
        self.settings = settings
        self.repo = repo
        self.pm = pm
        self.reporter = reporter
        self.jobs = jobs
        self._start_server = start_server
        self._stop_server = stop_server
        self.java_save_timeout = 900.0  # seconds a Java server may take to save its world before a hot backup (a test can shorten it)
        settings.backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------- helpers
    def archive_path(self, row: dict[str, Any]) -> Path:
        path = (self.settings.backup_dir / row["file_name"]).resolve()
        if not path.is_relative_to(self.settings.backup_dir.resolve()):
            raise BackupError("Invalid backup path.")
        return path

    def _new_file_name(self, server_id: str, kind: str) -> str:
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        directory = self.settings.backup_dir / server_id
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{stamp}_{kind}.tar.gz"
        n = 2
        while (directory / name).exists():
            name = f"{stamp}_{kind}-{n}.tar.gz"
            n += 1
        return f"{server_id}/{name}"

    # --------------------------------------------------------------- job wrappers
    def submit_backup(self, rec: ServerRecord, kind: str = "manual", note: str | None = None) -> dict[str, Any]:
        async def run(ctx: JobContext) -> None:
            try:
                await self.create(rec, kind, ctx, note=note)
            except UserError as exc:
                self.reporter.record(
                    "error", "backup", f"{kind.title()} backup of '{rec.name}' failed: {exc}", rec.id,
                    notify="backup_failed", title=f"Backup of {rec.name} failed",
                )
                raise

        return self.jobs.submit("backup", f"Back up {rec.name}", rec.id, run)

    def submit_restore(self, rec: ServerRecord, backup_id: str, safety_backup: bool, reapply_config: Callable[[ServerRecord], Any]) -> dict[str, Any]:
        return self.jobs.submit(
            "restore", f"Restore {rec.name}", rec.id,
            lambda ctx: self.restore(rec, backup_id, ctx, safety_backup=safety_backup, reapply_config=reapply_config),
        )

    # -------------------------------------------------------------------- create
    async def create(
        self,
        rec: ServerRecord,
        kind: str,
        ctx: JobContext,
        *,
        mode: str | None = None,
        note: str | None = None,
        build_tag: str | None = None,
    ) -> dict[str, Any]:
        """Create and verify a backup. The caller holds the server's busy flag."""
        if kind not in KINDS:
            raise BackupError(f"Unknown backup kind '{kind}'.")
        src = self.settings.servers_dir / rec.id
        if not src.is_dir():
            raise BackupError("The server folder does not exist.")
        rt = self.pm.runtime(rec.id)
        mode = mode or rec.policy.backup_mode
        if kind in COLD_KINDS:
            if rt.is_active:
                raise BackupError("Internal error: the server must be stopped before this kind of backup.")
            mode = "cold"

        restart_after = False
        if rt.state == "running" and mode == "cold":
            ctx.step("Stopping the server for a consistent (cold) backup")
            restart_after = True
            await self._stop_server(rec, ctx)

        try:
            return await self._create_inner(rec, kind, ctx, src, mode, note, build_tag)
        finally:
            if restart_after:
                ctx.step("Starting the server again")
                try:
                    await self._start_server(rec, ctx)
                except Exception as exc:  # the backup result matters more than the restart error
                    ctx.warn(f"The server could not be restarted after the backup: {exc}")

    async def _create_inner(
        self, rec: ServerRecord, kind: str, ctx: JobContext, src: Path, mode: str, note: str | None, build_tag: str | None
    ) -> dict[str, Any]:
        rt = self.pm.runtime(rec.id)
        total = await asyncio.to_thread(dir_size, src)
        needed = int(total * 1.05) + 50 * 1024 * 1024
        if free_bytes(self.settings.backup_dir) < needed:
            raise BackupError(
                f"Not enough free space in the backup folder (about {needed // (1024 * 1024)} MiB needed)."
            )

        consistent = True
        saved_off = False
        if rt.state == "running":
            ctx.step("Flushing the world to disk (hot backup)")
            consistent, saved_off = await self._quiesce(rec, ctx)

        file_name = self._new_file_name(rec.id, kind)
        final = self.settings.backup_dir / file_name
        partial = final.with_name(final.name + ".partial")
        build = self.repo.get_build(rec.build_id)
        pack = rec.spec.get("pack") or {}
        pack_tag = f"{pack.get('title') or 'Modpack'} {pack.get('version_number') or ''}".strip() if rec.is_java else None
        manifest = {
            "format": 1,
            "created_at": iso(),
            "manager_version": __version__,
            "server_id": rec.id,
            "server_name": rec.name,
            "kind": kind,
            "mode": mode,
            "build_id": rec.build_id,
            "build_tag": build_tag or (build.tag if build else pack_tag),
            "build_digest": build.digest if build else None,
            "uncompressed_bytes": total,
            "note": note,
        }
        loop = asyncio.get_running_loop()

        def report(done: int) -> None:
            loop.call_soon_threadsafe(ctx.progress, min(0.9, 0.9 * done / total) if total else None)

        ctx.step(f"Creating archive ({human_bytes(total)} of data)")
        try:
            try:
                result = await asyncio.to_thread(write_archive, src, partial, manifest, [*rec.policy.backup_exclude, *ALWAYS_EXCLUDED], report)
            finally:
                if saved_off:
                    await self._resume_saving(rec, ctx)
            if result["changed"] and mode == "cold":
                raise BackupError("Files changed while the stopped server was being archived: " + ", ".join(result["changed"][:5]))
            if result["changed"]:
                consistent = False
                ctx.warn(f"{len(result['changed'])} file(s) changed while being archived; this backup is only crash-consistent")
            if result["skipped"]:
                ctx.step(f"Skipped {len(result['skipped'])} non-regular file(s) (links/sockets)")

            ctx.progress(0.92, "Verifying the archive")
            ctx.step("Verifying the archive (checksum and full read)")
            await asyncio.to_thread(verify_archive, partial, result["sha256"])
            os.replace(partial, final)
        except BaseException:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            raise

        row = self.repo.insert_backup(
            server_id=rec.id,
            server_name=rec.name,
            kind=kind,
            file_name=file_name,
            size=result["size"],
            sha256=result["sha256"],
            build_id=rec.build_id,
            build_tag=manifest["build_tag"],
            consistent=int(consistent),
            verified=1,
            note=note,
        )
        if kind not in ("pre-restore", "uploaded", "imported"):
            self.repo.update_server(rec.id, last_backup_at=iso())
        ctx.step(f"Backup created: {file_name} ({human_bytes(result['size'])}, verified)")
        self.reporter.record("success", "backup", f"{kind.capitalize()} backup created ({human_bytes(result['size'])})", rec.id)
        await asyncio.to_thread(self.prune, rec)
        return row

    async def _quiesce_java(self, rec: ServerRecord, ctx: JobContext) -> tuple[bool, bool]:
        """Pause autosave and flush the world on a Java server, waiting for the server to say that it is done.

        A big modded world can need minutes for ``save-all flush``, so unlike the fixed pauses used for Pumpkin the
        backup starts only after the server printed "Saved the game".
        """
        consistent = True
        paused = False

        async def ask(command: str, confirmation: re.Pattern[str], timeout: float) -> bool:
            after = self.pm.runtime(rec.id).seq
            await self.pm.send_command(rec.id, command, echo=False)
            return await self.pm.wait_for_output(rec.id, confirmation, after, timeout)

        try:
            paused = True  # from here on save-on must be sent, even if the confirmation never comes
            if not await ask("save-off", _SAVE_OFF_RE, 30):
                ctx.warn("The server did not confirm that autosave is paused")
                consistent = False
            ctx.step("Saving the world before the backup (large worlds take a while)")
            if not await ask("save-all flush", _SAVED_RE, self.java_save_timeout):
                ctx.warn(f"The server did not confirm within {int(self.java_save_timeout)} seconds that the world is saved")
                consistent = False
        except ProcessError as exc:
            ctx.warn(f"Could not flush the world: {exc}")
            consistent = False
        return consistent, paused

    async def _quiesce(self, rec: ServerRecord, ctx: JobContext) -> tuple[bool, bool]:
        """Best-effort flush + pause autosave. Returns (consistent, autosave_paused)."""
        if rec.is_java:
            return await self._quiesce_java(rec, ctx)
        consistent = True
        paused = False
        try:
            async with MsmpClient(rec.mgmt_port, rec.mgmt_secret) as client:
                await client.save(flush=True)
        except MsmpError as exc:
            ctx.warn(f"Management API unavailable ({exc}); using console commands instead")
            try:
                await self.pm.send_command(rec.id, "save-all flush", echo=False)
                await asyncio.sleep(3)
            except ProcessError as err:
                ctx.warn(f"Could not flush the world: {err}")
                consistent = False
        try:
            await self.pm.send_command(rec.id, "save-off", echo=False)
            paused = True
            await asyncio.sleep(1)
            # A second flush after pausing catches anything written between the first flush and save-off.
            try:
                async with MsmpClient(rec.mgmt_port, rec.mgmt_secret) as client:
                    await client.save(flush=True)
            except MsmpError:
                await self.pm.send_command(rec.id, "save-all flush", echo=False)
                await asyncio.sleep(2)
        except ProcessError as exc:
            ctx.warn(f"Could not pause autosave: {exc}")
            consistent = False
        return consistent, paused

    async def _resume_saving(self, rec: ServerRecord, ctx: JobContext) -> None:
        try:
            await self.pm.send_command(rec.id, "save-on", echo=False)
        except ProcessError as exc:
            ctx.warn(f"Could not re-enable autosave ({exc}); restart the server to restore it")

    # --------------------------------------------------------------------- prune
    def prune(self, rec: ServerRecord) -> list[str]:
        """Apply the retention policy. Protected backups and the latest update's backup are never removed."""
        rows = self.repo.list_backups(rec.id)
        latest_update = self.repo.latest_update(rec.id, ("success", "rolled_back", "running"))
        keep_forever = {latest_update["backup_id"]} if latest_update and latest_update.get("backup_id") else set()
        groups: dict[str, tuple[set[str], int]] = {
            "regular": ({"manual", "scheduled"}, rec.policy.backup_keep),
            "update": ({"pre-update"}, rec.policy.backup_keep_pre_update),
            "safety": ({"pre-restore", "pre-delete"}, 5),
        }
        removed: list[str] = []
        for kinds, keep in groups.values():
            candidates = [r for r in rows if r["kind"] in kinds and not r["protected"] and r["id"] not in keep_forever]
            for row in candidates[keep:]:  # rows are newest first
                self.delete(row["id"], quiet=True)
                removed.append(row["id"])
        if removed:
            log.info("Pruned %d old backup(s) of %s", len(removed), rec.id)
        return removed

    def delete(self, backup_id: str, quiet: bool = False) -> None:
        row = self.repo.get_backup(backup_id)
        if not row:
            return
        with contextlib.suppress(OSError, BackupError):
            self.archive_path(row).unlink(missing_ok=True)
        self.repo.delete_backup(backup_id)
        if not quiet:
            self.reporter.record("info", "backup", f"Deleted backup {row['file_name']}", row["server_id"])

    # ------------------------------------------------------------------- restore
    async def restore(
        self,
        rec: ServerRecord,
        backup_id: str,
        ctx: JobContext,
        *,
        safety_backup: bool = True,
        restart: bool | None = None,
        reapply_config: Callable[[ServerRecord], Any] | None = None,
    ) -> None:
        """Replace the server's files with a backup. The caller holds the busy flag."""
        row = self.repo.get_backup(backup_id)
        if not row:
            raise BackupError("That backup no longer exists.")
        archive = self.archive_path(row)
        if not archive.is_file():
            raise BackupError("The backup file is missing from disk.")

        rt = self.pm.runtime(rec.id)
        was_running = rt.is_active
        if restart is None:
            restart = was_running or rec.desired_running
        if was_running:
            ctx.step("Stopping the server")
            await self._stop_server(rec, ctx)

        ctx.step("Checking the backup archive")
        ctx.progress(0.05)
        await asyncio.to_thread(verify_archive, archive, row.get("sha256"))

        server_dir = self.settings.servers_dir / rec.id
        if safety_backup and server_dir.is_dir():
            ctx.step("Saving the current state as a safety backup")
            await self.create(rec, "pre-restore", ctx, mode="cold", note=f"Before restoring {row['file_name']}")

        tmp_dir = self.settings.servers_dir / f".{rec.id}.restore-{uuid.uuid4().hex[:8]}"
        old_dir = self.settings.servers_dir / f".{rec.id}.old-{uuid.uuid4().hex[:8]}"
        limit = max(free_bytes(self.settings.servers_dir) - 50 * 1024 * 1024, 0)
        ctx.step("Extracting the backup")
        ctx.progress(0.4)
        try:
            await asyncio.to_thread(extract_archive, archive, tmp_dir, limit)
            ctx.progress(0.8)
            ctx.step("Swapping in the restored files")
            if server_dir.exists():
                os.replace(server_dir, old_dir)
            try:
                os.replace(tmp_dir, server_dir)
            except OSError:
                if old_dir.exists() and not server_dir.exists():
                    os.replace(old_dir, server_dir)  # put the original back
                raise
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        await asyncio.to_thread(shutil.rmtree, old_dir, True)

        if reapply_config:
            reapply_config(rec)
        self.reporter.record("warning", "restore", f"Restored backup {row['file_name']}", rec.id)
        ctx.step("Restore complete")
        if restart:
            ctx.step("Starting the server")
            await self._start_server(rec, ctx)

    # ------------------------------------------------------------ reconcile index
    def reconcile(self) -> dict[str, int]:
        """Align the database with the files on disk (users may add or delete archives by hand)."""
        removed = added = 0
        known = {r["file_name"]: r for r in self.repo.list_backups()}
        base = self.settings.backup_dir
        for name, row in known.items():
            if not (base / name).is_file():
                self.repo.delete_backup(row["id"])
                removed += 1
        for partial in base.glob("*/*.partial"):
            with contextlib.suppress(OSError):
                partial.unlink()
        for archive in sorted(base.glob("*/*.tar.gz")):
            rel = archive.relative_to(base).as_posix()
            if rel in known:
                continue
            manifest = read_manifest(archive) or {}
            server_id = archive.parent.name
            server = self.repo.get_server(server_id)
            created = (
                manifest.get("created_at")
                or iso(datetime.fromtimestamp(archive.stat().st_mtime, tz=UTC))
            )
            self.repo.insert_backup(
                id=new_id(), server_id=server_id, server_name=manifest.get("server_name") or (server.name if server else server_id),
                kind="imported", file_name=rel, size=archive.stat().st_size, sha256=None, created_at=created,
                build_id=manifest.get("build_id"), build_tag=manifest.get("build_tag"),
                consistent=1, verified=0, note="Found on disk",
            )
            added += 1
        return {"removed": removed, "added": added}

    def total_size(self) -> int:
        return sum(int(r["size"] or 0) for r in self.repo.list_backups())
