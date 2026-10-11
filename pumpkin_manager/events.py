"""In-process pub/sub used to push live updates (server state, jobs, console) to WebSocket clients."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EventView = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[dict[str, Any]]
    topics: set[str] = field(default_factory=set)
    dropped: int = 0
    # Who is listening. The administrator (and internal listeners) see every event as published; other accounts get each
    # event through ``view``, which may reshape it or drop it.
    user_id: int | None = None
    unrestricted: bool = True
    view: EventView | None = None

    def wants(self, topic: str) -> bool:
        return topic in self.topics or "*" in self.topics


class EventBus:
    """Fan-out of small JSON events. Publishing never blocks: slow consumers lose old events."""

    def __init__(self, queue_size: int = 2000):
        self._subs: set[Subscription] = set()
        self._queue_size = queue_size

    def subscribe(
        self, topics: set[str] | None = None, *, user_id: int | None = None, view: EventView | None = None,
        unrestricted: bool | None = None,
    ) -> Subscription:
        """Every event goes through ``view`` first when one is given. A subscription with a view is restricted (events addressed
        to other accounts are skipped) unless ``unrestricted`` says otherwise, as it does for the administrator."""
        sub = Subscription(
            queue=asyncio.Queue(maxsize=self._queue_size), topics=set(topics or ()), user_id=user_id,
            unrestricted=(view is None) if unrestricted is None else unrestricted, view=view,
        )
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def publish(self, topic: str, event_type: str, data: Any, audience: set[int] | None = None) -> None:
        """Send an event to everyone subscribed to ``topic``. With ``audience`` (account ids), restricted listeners that are
        not in it do not get it; the administrator always does."""
        message = {"topic": topic, "type": event_type, "data": data}
        for sub in list(self._subs):
            if not sub.wants(topic):
                continue
            if audience is not None and not sub.unrestricted and sub.user_id not in audience:
                continue
            try:
                outgoing: dict[str, Any] | None = message if sub.view is None else sub.view(message)
            except Exception:  # a listener's filter must never break whoever publishes; withhold the event from that listener
                log.exception("An event filter failed; the event was not delivered to that listener")
                continue
            if outgoing is None:
                continue
            try:
                sub.queue.put_nowait(outgoing)
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(outgoing)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                sub.dropped += 1

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
