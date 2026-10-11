"""Update checking and the safe update procedure (download -> stop -> backup -> switch -> verify -> rollback)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .backups import BackupError, BackupService
from .binaries import BuildStore
from .jobs import JobContext, JobService
from .mcnet import MsmpClient, MsmpError, PingError, server_list_ping
from .models import CHANNELS, AppSettings, BuildRecord, ServerRecord
from .process import ProcessManager
from .releases import BadBuildError, ReleaseError, ReleaseSource
from .repo import Repo
from .reporter import Reporter
from .servers import ServerError, ServerService
from .util import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

STABILITY_SECONDS = 15
AUTO_RETRY_MINUTES = 30
AUTO_RETRY_MAX_MINUTES = 12 * 60


def retry_delay_minutes(failures: int) -> int:
    """How long automatic updates wait after ``failures`` consecutive failed attempts: 30, 30, 60, 120 ... 720 min."""
    return min(AUTO_RETRY_MINUTES * 2 ** max(failures - 1, 0), AUTO_RETRY_MAX_MINUTES)


def is_newer(candidate: BuildRecord, current: BuildRecord | None) -> bool:
    """Is ``candidate`` a build the server should be offered?

    Rolling channels keep the same tag, so identity is the asset digest. Timestamps guard against
    offering an *older* build (for example after a release was withdrawn).
    """
    if current is None:
        return True
    if candidate.id == current.id or candidate.digest == current.digest:
        return False
    if candidate.channel != current.channel:
        return True  # the user moved the server to another channel: that is an explicit switch
    if candidate.channel == "stable":
        a, b = candidate.published_at or candidate.updated_at or "", current.published_at or current.updated_at or ""
    else:
        a, b = candidate.updated_at or candidate.published_at or "", current.updated_at or current.published_at or ""
    return a > b if a and b else True


class UpdateService:
    def __init__(
        self,
        repo: Repo,
        builds: BuildStore,
        pm: ProcessManager,
        servers: ServerService,
        backups: BackupService,
        jobs: JobService,
        reporter: Reporter,
        source_getter: Callable[[], ReleaseSource],
        app_settings: Callable[[], AppSettings],
        stability_seconds: float = STABILITY_SECONDS,
    ):
        self.repo = repo
        self.builds = builds
        self.pm = pm
        self.servers = servers
        self.backups = backups
        self.jobs = jobs
        self.reporter = reporter
        self._source = source_getter
        self._app_settings = app_settings
        self.stability_seconds = stability_seconds
        self.latest: dict[str, BuildRecord] = {}
        self.last_check: str | None = None
        self.last_errors: dict[str, str] = {}
        self._check_lock = asyncio.Lock()

    # ----------------------------------------------------------------- checking
    def restore_state(self) -> None:
        """Seed 'latest known build per channel' from the database so a restart does not forget it."""
        for build in self.repo.list_builds():  # newest first
            self.latest.setdefault(build.channel, build)
        self.last_check = self.repo.get_kv("updates:last_check")

    async def check(self) -> dict[str, Any]:
        """Refresh what GitHub offers on every channel and flag servers that can be updated."""
        async with self._check_lock:
            errors: dict[str, str] = {}
            for channel in CHANNELS:
                try:
                    self.latest[channel] = self.builds.register(await self._source().latest(channel))
                except ReleaseError as exc:
                    errors[channel] = str(exc)
                    log.warning("Update check for %s failed: %s", channel, exc)
            self.last_errors = errors
            self.last_check = iso()
            self.repo.set_kv("updates:last_check", self.last_check)
            flagged = 0
            for rec in self.repo.list_servers():
                if self._evaluate(rec):
                    flagged += 1
            return {"checked_at": self.last_check, "errors": errors, "servers_with_updates": flagged}

    def _skipped_build(self, server_id: str) -> str | None:
        return self.repo.get_kv(f"skip:{server_id}")

    def _evaluate(self, rec: ServerRecord) -> bool:
        """Set or clear the pending-update flag of one server. Returns True if an update is pending."""
        if rec.engine != "pumpkin":
            return False  # servers of other engines are not on a Pumpkin channel; their own updates are checked elsewhere
        now = iso()
        if rec.pinned_tag or rec.policy.update_mode == "off":
            self.repo.update_server(rec.id, pending_build_id=None, pending_since=None, last_update_check_at=now)
            return False
        candidate = self.latest.get(rec.channel)
        if candidate is None:  # that channel could not be checked this time: keep the previous verdict
            return rec.pending_build_id is not None
        current = self.repo.get_build(rec.build_id)
        if is_newer(candidate, current) and self._skipped_build(rec.id) != candidate.id:
            if rec.pending_build_id != candidate.id:
                self.repo.update_server(rec.id, pending_build_id=candidate.id, pending_since=now, last_update_check_at=now)
                notify = "update_available" if rec.policy.update_mode == "notify" else None
                self.reporter.record(
                    "info", "update", f"Update available for '{rec.name}': {candidate.label}", rec.id,
                    notify=notify, title=f"Update available for {rec.name}",
                )
            else:
                self.repo.update_server(rec.id, last_update_check_at=now)
            self.servers.publish_state(rec.id)
            return True
        had_pending = rec.pending_build_id is not None
        self.repo.update_server(rec.id, pending_build_id=None, pending_since=None, last_update_check_at=now)
        if had_pending:
            self.servers.publish_state(rec.id)
        return False

    # -------------------------------------------------------------- auto updates
    async def process_pending(self) -> int:
        """Start updates that are due. Called every minute; cheap when nothing is pending."""
        started = 0
        now = utcnow()
        for rec in self.repo.list_servers():
            if rec.engine != "pumpkin" or not rec.pending_build_id or rec.policy.update_mode != "auto" or rec.pinned_tag:
                continue
            if self.jobs.is_busy(rec.id):
                continue
            target = self.repo.get_build(rec.pending_build_id)
            current = self.repo.get_build(rec.build_id)
            if target is None or (current is not None and current.channel != target.channel):
                continue  # switching channels is never done automatically
            attempt = self.repo.get_kv(f"autoattempt:{rec.id}")
            failures = int(self.repo.get_kv(f"autofails:{rec.id}") or 0)
            if attempt and (now - parse_iso(attempt)).total_seconds() < retry_delay_minutes(failures) * 60:
                continue  # a recent attempt failed: wait, with a delay that grows while the trouble persists
            rt = self.pm.runtime(rec.id)
            players = rt.players_online if rt.state == "running" else 0
            waited_hours = (now - parse_iso(rec.pending_since)).total_seconds() / 3600 if rec.pending_since else 0
            if players > 0 and rec.policy.update_only_when_empty and waited_hours < rec.policy.update_max_defer_hours:
                continue
            self.repo.set_kv(f"autoattempt:{rec.id}", iso())
            announce = 60 if players > 0 else 0
            try:
                self.submit_update(rec, target, trigger="auto", announce_seconds=announce)
                started += 1
            except Exception as exc:
                log.warning("Could not start the automatic update of %s: %s", rec.id, exc)
        return started

    # ------------------------------------------------------------------ applying
    def submit_update(
        self, rec: ServerRecord, target: BuildRecord, *, trigger: str = "manual", announce_seconds: int = 0, pin: bool = False
    ) -> dict[str, Any]:
        return self.jobs.submit(
            "update", f"Update {rec.name} to {target.label}", rec.id,
            lambda ctx: self.apply(rec, target, ctx, trigger=trigger, announce_seconds=announce_seconds, pin=pin),
        )

    async def apply(
        self, rec: ServerRecord, target: BuildRecord, ctx: JobContext, *, trigger: str, announce_seconds: int = 0, pin: bool = False
    ) -> None:
        rec = self.servers.get(rec.id)
        old = self.repo.get_build(rec.build_id)
        if old is not None and old.id == target.id:
            raise ServerError("The server already runs that version.")
        rt = self.pm.runtime(rec.id)
        was_running = rt.state in ("starting", "running")
        original_previous = rec.previous_build_id
        update_id = self.repo.insert_update(
            server_id=rec.id, from_build_id=old.id if old else None, to_build_id=target.id,
            from_tag=old.tag if old else None, to_tag=target.tag, trigger=trigger,
        )
        ctx.step(f"Updating '{rec.name}': {old.label if old else 'no version'} -> {target.label}")
        backup_row: dict[str, Any] | None = None
        switched = False
        try:
            # 1. Fetch the new binary while the server keeps running; a failure here costs no downtime.
            if not self.builds.is_available(target):
                ctx.step("Downloading the new version (the server keeps running meanwhile)")
                target = await self.builds.ensure(target, self.servers._download_progress(ctx, span=0.4))
            ctx.progress(0.4)

            # 2. Tell players what is about to happen.
            if rt.state == "running" and announce_seconds > 0 and rt.players_online > 0:
                await self._announce(rec, announce_seconds, ctx)

            # 3. Stop gracefully so the world is saved and files are quiet for the backup.
            if rt.is_active:
                await self.servers._do_stop(rec, ctx)
            ctx.progress(0.5)

            # 4. Mandatory backup. Without a verified backup nothing is changed.
            ctx.step("Creating the backup that protects this update")
            backup_row = await self.backups.create(
                rec, "pre-update", ctx, mode="cold", note=f"Before updating to {target.label}",
                build_tag=old.tag if old else None,
            )
            self.repo.update_update(update_id, backup_id=backup_row["id"])
            ctx.progress(0.75)

            # 5. Point the server at the new binary.
            self.repo.update_server(
                rec.id, build_id=target.id, previous_build_id=old.id if old else None, pending_build_id=None,
                pending_since=None, channel=target.channel, pinned_tag=target.tag if pin else None,
            )
            switched = True
            self.servers.publish_state(rec.id)

            # 6. Bring it back up and make sure it stays up.
            if was_running:
                ctx.step("Starting the updated server")
                await self.servers._do_start(self.servers.get(rec.id), ctx)
                await self._watch_stability(rec, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(
                rec, old, target, original_previous, backup_row, update_id, was_running, switched, ctx, exc
            )
            return

        self.repo.update_update(update_id, status="success", message="Updated", finished_at=iso())
        ctx.step(f"Update complete: now running {target.label}")
        self.reporter.record(
            "success", "update", f"'{rec.name}' updated to {target.label}", rec.id,
            notify="update_applied", title=f"{rec.name} updated",
        )
        self.repo.set_kv(f"skip:{rec.id}", "")
        self.repo.set_kv(f"autofails:{rec.id}", "0")
        self.prune_builds()
        self.servers.publish_state(rec.id)

    async def _announce(self, rec: ServerRecord, seconds: int, ctx: JobContext) -> None:
        ctx.step(f"Warning {self.pm.runtime(rec.id).players_online} player(s): restarting in {seconds}s for an update")
        try:
            async with MsmpClient(rec.mgmt_port, rec.mgmt_secret) as client:
                await client.broadcast(f"Server update in {seconds} seconds - please finish up. The server will be back in a minute.")
        except MsmpError:
            await self.pm.send_command(rec.id, f"say Server update in {seconds} seconds, the server will restart.", echo=False)
        await asyncio.sleep(seconds)

    async def _watch_stability(self, rec: ServerRecord, ctx: JobContext) -> None:
        if self.stability_seconds <= 0:
            return
        ctx.step(f"Watching the new version for {int(self.stability_seconds)} seconds")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.stability_seconds
        while loop.time() < deadline:
            rt = self.pm.runtime(rec.id)
            if rt.state != "running":
                raise ServerError(f"The new version stopped by itself ({rt.last_error or rt.state}).")
            await asyncio.sleep(1)
        try:
            await server_list_ping("127.0.0.1", self.servers.get(rec.id).java_port, timeout=3)
        except PingError as exc:
            raise ServerError(f"The new version no longer answers on its port ({exc}).") from exc

    async def _handle_failure(
        self,
        rec: ServerRecord,
        old: BuildRecord | None,
        target: BuildRecord,
        original_previous: str | None,
        backup_row: dict[str, Any] | None,
        update_id: str,
        was_running: bool,
        switched: bool,
        ctx: JobContext,
        exc: Exception,
    ) -> None:
        reason = str(exc) if isinstance(exc, (ServerError, BackupError, ReleaseError)) else f"Unexpected error: {exc}"
        if not isinstance(exc, (ServerError, BackupError, ReleaseError)):
            log.exception("Update of %s failed", rec.id)
        ctx.step(f"Update failed: {reason}", "error")
        rt = self.pm.runtime(rec.id)

        if not switched:
            # Nothing was changed. Put the server back the way it was.
            self.repo.update_update(update_id, status="failed", message=reason, finished_at=iso())
            if was_running and not rt.is_active:
                ctx.step("Restarting the server on the previous version")
                try:
                    await self.servers._do_start(self.servers.get(rec.id), ctx)
                except Exception as restart_exc:
                    ctx.step(f"The previous version could not be restarted: {restart_exc}", "error")
            retry_note = ""
            if isinstance(exc, BadBuildError):
                # The file itself is unusable, and will be tomorrow as well: stop offering exactly this build.
                # A different (newer) build is offered again as usual.
                self.repo.set_kv(f"skip:{rec.id}", target.id)
                self.repo.update_server(rec.id, pending_build_id=None, pending_since=None)
                retry_note = " This build will not be tried again automatically; a newer one will be."
            else:
                self.repo.set_kv(f"skip:{rec.id}", "")
                failures = int(self.repo.get_kv(f"autofails:{rec.id}") or 0) + 1
                self.repo.set_kv(f"autofails:{rec.id}", str(failures))
                if rec.policy.update_mode == "auto":
                    retry_note = f" The next automatic attempt is in about {retry_delay_minutes(failures)} minutes."
            self.reporter.record(
                "error", "update", f"Update of '{rec.name}' to {target.label} was not applied: {reason}{retry_note}", rec.id,
                notify="update_failed", title=f"Update of {rec.name} failed",
            )
            self.servers.publish_state(rec.id)
            return

        # The new version was switched in but did not start properly: roll everything back.
        self.repo.set_kv(f"skip:{rec.id}", target.id)  # never retry this exact build automatically
        ctx.step(f"Rolling back to {old.label if old else 'the previous version'}", "warning")
        rolled_back = False
        try:
            if rt.is_active:
                await self.pm.stop(rec.id, 20)
            if backup_row is not None:
                await self.backups.restore(
                    self.servers.get(rec.id), backup_row["id"], ctx, safety_backup=False, restart=False,
                    reapply_config=lambda r: self.servers.ensure_config(r, ctx),
                )
            if old is not None:
                self.repo.update_server(
                    rec.id, build_id=old.id, previous_build_id=original_previous, pending_build_id=None, pending_since=None,
                    channel=old.channel,
                )
            if was_running:
                await self.servers._do_start(self.servers.get(rec.id), ctx)
            rolled_back = True
        except Exception as rollback_exc:
            ctx.step(f"The rollback failed: {rollback_exc}", "error")
            log.exception("Rollback of %s failed", rec.id)

        if rolled_back:
            self.repo.update_update(update_id, status="rolled_back", message=reason, finished_at=iso())
            self.reporter.record(
                "warning", "update",
                f"Update of '{rec.name}' to {target.label} failed ({reason}); rolled back to {old.label if old else 'the previous version'}",
                rec.id, notify="update_failed", title=f"Update of {rec.name} rolled back",
            )
        else:
            self.repo.update_update(update_id, status="failed", message=f"{reason}; rollback failed", finished_at=iso())
            self.reporter.record(
                "error", "update",
                f"Update of '{rec.name}' failed and the automatic rollback did not complete. The backup "
                f"{backup_row['file_name'] if backup_row else ''} is intact; restore it from the Backups tab.",
                rec.id, notify="update_failed", title=f"Update of {rec.name} failed - action needed",
            )
        self.servers.publish_state(rec.id)

    # ------------------------------------------------------------------ rollback
    def submit_rollback(self, rec: ServerRecord, restore_data: bool) -> dict[str, Any]:
        return self.jobs.submit("rollback", f"Roll back {rec.name}", rec.id, lambda ctx: self.rollback(rec, ctx, restore_data))

    async def rollback(self, rec: ServerRecord, ctx: JobContext, restore_data: bool) -> None:
        """User-requested return to the previously installed version, optionally with its old data."""
        rec = self.servers.get(rec.id)
        previous = self.repo.get_build(rec.previous_build_id)
        current = self.repo.get_build(rec.build_id)
        if previous is None:
            raise ServerError("No previous version is known for this server.")
        last_update = self.repo.latest_update(rec.id, ("success",))
        backup_id = last_update.get("backup_id") if last_update else None
        if restore_data and not (backup_id and self.repo.get_backup(backup_id)):
            raise ServerError("The backup that was made before the last update no longer exists.")
        if not self.builds.is_available(previous):
            ctx.step(f"Downloading {previous.label} again")
            try:
                previous = await self.builds.ensure(previous, self.servers._download_progress(ctx, span=0.4))
            except ReleaseError as exc:
                raise ServerError(f"The previous version is no longer available: {exc}") from exc

        update_id = self.repo.insert_update(
            server_id=rec.id, from_build_id=current.id if current else None, to_build_id=previous.id,
            from_tag=current.tag if current else None, to_tag=previous.tag, trigger="rollback",
        )
        was_running = self.pm.runtime(rec.id).is_active
        try:
            if was_running:
                await self.servers._do_stop(rec, ctx)
            if restore_data and backup_id:
                ctx.step("Restoring the data from before the update")
                await self.backups.restore(
                    rec, backup_id, ctx, safety_backup=True, restart=False,
                    reapply_config=lambda r: self.servers.ensure_config(r, ctx),
                )
            else:
                ctx.step("Creating a safety backup first")
                await self.backups.create(rec, "pre-restore", ctx, mode="cold", note="Before rolling back the version")
            self.repo.update_server(
                rec.id, build_id=previous.id, previous_build_id=current.id if current else None, channel=previous.channel,
                pinned_tag=previous.tag, pending_build_id=None, pending_since=None,
            )
            if was_running:
                await self.servers._do_start(self.servers.get(rec.id), ctx)
        except Exception as exc:
            self.repo.update_update(update_id, status="failed", message=str(exc), finished_at=iso())
            raise
        self.repo.update_update(update_id, status="success", message="Rolled back", finished_at=iso())
        self.reporter.record(
            "warning", "update", f"'{rec.name}' rolled back to {previous.label}"
            f"{' (data restored)' if restore_data else ''}; automatic updates are paused until you unpin the version",
            rec.id,
        )
        self.servers.publish_state(rec.id)

    # ------------------------------------------------------------------ cleanup
    def _build_users(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """Servers per build id: those that run it or can roll back to it, and those waiting to update to it."""
        using: dict[str, list[str]] = {}
        waiting: dict[str, list[str]] = {}
        for rec in self.repo.list_servers():
            for build_id in (rec.build_id, rec.previous_build_id):
                if build_id:
                    using.setdefault(build_id, []).append(rec.id)
            if rec.pending_build_id:
                waiting.setdefault(rec.pending_build_id, []).append(rec.id)
        return using, waiting

    def prune_builds(self, *, keep_spares: bool = True) -> list[str]:
        """Remove downloaded builds that no server needs. Returns the ids that were removed.

        What a server runs, can roll back to or is about to update to is never touched. Right after an update the
        newest ``keep_old_builds`` builds of each channel also stay as spares; the Clean up button asks for
        ``keep_spares=False`` and removes those as well.
        """
        using, waiting = self._build_users()
        spares = self._app_settings().keep_old_builds if keep_spares else 0
        return self.builds.prune(set(using) | set(waiting), spares)

    def ensure_builds_not_in_flight(self) -> None:
        """Refuse to remove builds while a server is being created or updated.

        Such a job may have fetched a build a moment ago without any server recording it yet, so it would look unused.
        """
        if self.jobs.is_running("create", "update", "rollback"):
            raise ServerError("A server is being created or updated right now. Try again when it has finished.", 409)

    # ------------------------------------------------------------------ overview
    def _channel_entry(self, channel: str) -> dict[str, Any] | None:
        known = self.latest.get(channel)
        if known is None:
            return None
        build = self.repo.get_build(known.id) or known  # the copy kept from the last check does not know about downloads since
        entry = build.public()
        entry["downloaded"] = self.builds.is_available(build)
        return entry

    def overview(self) -> dict[str, Any]:
        using, waiting = self._build_users()
        builds = []
        for build in self.repo.list_builds():
            entry = build.public()
            entry["used_by"] = using.get(build.id, [])
            entry["wanted_by"] = waiting.get(build.id, [])
            entry["available"] = self.builds.is_available(build)
            builds.append(entry)
        return {
            "channels": {ch: self._channel_entry(ch) for ch in CHANNELS},
            "last_check": self.last_check,
            "errors": self.last_errors,
            "builds": builds,
            "disk_bytes": self.builds.disk_usage(),
        }
