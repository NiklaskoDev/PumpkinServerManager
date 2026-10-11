"""In-process pub/sub used to push live updates (server state, jobs, console) to WebSocket clients."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[dict[str, Any]]
    topics: set[str] = field(default_factory=set)
    dropped: int = 0

    def wants(self, topic: str) -> bool:
        return topic in self.topics or "*" in self.topics


class EventBus:
    """Fan-out of small JSON events. Publishing never blocks: slow consumers lose old events."""

    def __init__(self, queue_size: int = 2000):
        self._subs: set[Subscription] = set()
        self._queue_size = queue_size

    def subscribe(self, topics: set[str] | None = None) -> Subscription:
        sub = Subscription(queue=asyncio.Queue(maxsize=self._queue_size), topics=set(topics or ()))
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def publish(self, topic: str, event_type: str, data: Any) -> None:
        message = {"topic": topic, "type": event_type, "data": data}
        for sub in list(self._subs):
            if not sub.wants(topic):
                continue
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                sub.dropped += 1

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
