"""Background loops: monitoring, update checks, automatic updates, scheduled backups, housekeeping."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import ServerRecord
from .util import parse_iso, utcnow

log = logging.getLogger(__name__)


def load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown timezone '%s'; falling back to UTC", name)
        return UTC


def backup_due(rec: ServerRecord, now_utc: datetime, tz: tzinfo) -> bool:
    """Is a scheduled backup of ``rec`` overdue at ``now_utc``?"""
    schedule = rec.policy.backup_schedule
    if schedule.type == "off":
        return False
    last = parse_iso(rec.last_backup_at) if rec.last_backup_at else None
    if schedule.type == "interval":
        return last is None or now_utc - last >= timedelta(hours=schedule.hours)
    hour, minute = (int(part) for part in schedule.at.split(":"))
    now_local = now_utc.astimezone(tz)
    slot = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_local < slot:
        slot -= timedelta(days=1)  # the most recent daily slot that has already passed
    return last is None or last < slot.astimezone(UTC)


class Scheduler:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self._tasks: list[asyncio.Task] = []
        self._tz = load_timezone(ctx.settings.timezone)

    def start(self) -> None:
        loops: list[tuple[str, float, Callable[[], Awaitable[None]]]] = [
            ("monitor", 5, self._monitor),
            ("updates", 30, self._update_check),
            ("pending-updates", 60, self._pending_updates),
            ("backups", 30, self._scheduled_backups),
            ("housekeeping", 300, self._housekeeping),
        ]
        for name, interval, fn in loops:
            self._tasks.append(asyncio.create_task(self._loop(name, interval, fn), name=f"sched-{name}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    async def _loop(self, name: str, interval: float, fn: Callable[[], Awaitable[None]]) -> None:
        # Stagger the first run slightly so everything does not fire at the same instant on boot.
        await asyncio.sleep({"monitor": 2, "updates": 20, "pending-updates": 45, "backups": 25, "housekeeping": 60}.get(name, 5))
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler loop '%s' failed; it will retry", name)
            await asyncio.sleep(interval)

    # --------------------------------------------------------------------- loops
    async def _monitor(self) -> None:
        await self.ctx.servers.monitor_tick()

    _next_update_check = 0.0

    async def _update_check(self) -> None:
        cfg = self.ctx.app_settings()
        if not cfg.update_check_enabled:
            return
        now = time.monotonic()
        if now < self._next_update_check:
            return
        self._next_update_check = now + cfg.update_check_minutes * 60
        result = await self.ctx.updater.check()
        if result["errors"]:
            log.info("Update check finished with errors: %s", result["errors"])
        packs = await self.ctx.pack_updates.check()
        if packs["errors"]:
            log.info("Modpack update check finished with errors: %s", packs["errors"])

    def reset_update_timer(self) -> None:
        self._next_update_check = 0.0

    async def _pending_updates(self) -> None:
        if self.ctx.app_settings().update_check_enabled:
            await self.ctx.updater.process_pending()

    async def _scheduled_backups(self) -> None:
        now = utcnow()
        for rec in self.ctx.repo.list_servers():
            if not backup_due(rec, now, self._tz) or self.ctx.jobs.is_busy(rec.id):
                continue
            if not (self.ctx.settings.servers_dir / rec.id).is_dir():
                continue
            try:
                self.ctx.backups.submit_backup(rec, "scheduled")
            except Exception as exc:  # busy or similar: try again on the next tick
                log.debug("Scheduled backup of %s postponed: %s", rec.id, exc)

    async def _housekeeping(self) -> None:
        ctx = self.ctx
        ctx.auth.housekeeping()
        ctx.repo.prune_jobs()
        ctx.repo.prune_activity()
        ctx.servers.jvm.purge_old_uploads()  # modpack files that were uploaded but never used
        await ctx.servers.refresh_disk_usage()
