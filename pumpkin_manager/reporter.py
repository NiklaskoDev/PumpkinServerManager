"""One place to record notable events: activity feed, live push to the dashboard, optional webhook."""

from __future__ import annotations

import asyncio
import logging

from .events import EventBus
from .notify import Notifier
from .repo import Repo

log = logging.getLogger(__name__)


class Reporter:
    def __init__(self, repo: Repo, events: EventBus, notifier: Notifier):
        self.repo = repo
        self.events = events
        self.notifier = notifier
        self._tasks: set[asyncio.Task] = set()

    def record(
        self,
        level: str,
        kind: str,
        message: str,
        server_id: str | None = None,
        *,
        notify: str | None = None,
        title: str | None = None,
    ) -> None:
        """Add an activity entry. ``notify`` is the webhook event name, if this should be pushed out."""
        row = self.repo.add_activity(level, kind, message, server_id)
        self.events.publish("activity", "activity", row)
        if notify:
            server = self.repo.get_server(server_id) if server_id else None
            try:
                task = asyncio.get_running_loop().create_task(
                    self.notifier.send(notify, title or kind, message, level, server.name if server else None)
                )
            except RuntimeError:
                return
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=10)
