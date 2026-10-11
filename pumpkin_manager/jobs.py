"""Background jobs: progress/step tracking and per-server mutual exclusion."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .errors import UserError
from .events import EventBus
from .repo import Repo
from .util import iso, utcnow

log = logging.getLogger(__name__)


class Busy(UserError):
    status = 409

    def __init__(self, server_id: str, operation: str):
        super().__init__(f"The server is busy ({operation}). Wait for it to finish and try again.")
        self.server_id = server_id
        self.operation = operation


class JobFailed(UserError):
    """Raised inside a job to fail it with a message that is shown to the user."""


class JobContext:
    def __init__(self, service: "JobService", job: dict[str, Any]):
        self._service = service
        self.id: str = job["id"]
        self.server_id: str | None = job["server_id"]
        self._steps: list[dict[str, Any]] = []
        self._last_progress = 0.0
        self._last_progress_at = 0.0

    def claim(self, server_id: str) -> None:
        """Attach this job to a server that did not exist yet when it was submitted."""
        if server_id in self._service._busy:
            raise Busy(server_id, self._service._busy[server_id][1])
        self._service._busy[server_id] = (self.id, self._service.repo.get_job(self.id)["kind"])  # type: ignore[index]
        self.server_id = server_id
        self._service._update(self.id, server_id=server_id)

    def step(self, text: str, level: str = "info") -> None:
        log.info("[job %s] %s", self.id, text)
        self._steps.append({"ts": iso(utcnow()), "level": level, "text": text})
        self._service._update(self.id, steps=self._steps[-200:], message=text)

    def warn(self, text: str) -> None:
        self.step(text, "warning")

    def progress(self, fraction: float | None, message: str | None = None) -> None:
        """Report progress in [0, 1]; updates are throttled so downloads do not flood the UI."""
        now = time.monotonic()
        done = fraction is not None and fraction >= 1.0
        if not done and now - self._last_progress_at < 0.4 and (
            fraction is None or abs(fraction - self._last_progress) < 0.01
        ):
            return
        self._last_progress_at = now
        self._last_progress = fraction or 0.0
        fields: dict[str, Any] = {"progress": fraction}
        if message:
            fields["message"] = message
        self._service._update(self.id, **fields)


JobFn = Callable[[JobContext], Awaitable[Any]]


class JobService:
    def __init__(self, repo: Repo, events: EventBus):
        self.repo = repo
        self.events = events
        self._busy: dict[str, tuple[str, str]] = {}  # server id -> (job id, kind)
        self._running: dict[str, str] = {}  # job id -> kind, also for jobs that are not tied to a server (yet)
        self._tasks: set[asyncio.Task] = set()
        self.on_finish: Callable[[dict[str, Any]], None] | None = None

    # -------------------------------------------------------------------- state
    def busy_operation(self, server_id: str) -> str | None:
        entry = self._busy.get(server_id)
        return entry[1] if entry else None

    def is_busy(self, server_id: str) -> bool:
        return server_id in self._busy

    def active_count(self) -> int:
        return len(self._tasks)

    def is_running(self, *kinds: str) -> bool:
        """Is a job of one of these kinds running right now, whatever server it belongs to?"""
        return any(kind in kinds for kind in self._running.values())

    # ---------------------------------------------------------------- submitting
    def submit(self, kind: str, title: str, server_id: str | None, fn: JobFn) -> dict[str, Any]:
        """Start ``fn`` in the background. Raises ``Busy`` if the server already runs an operation."""
        if server_id is not None and server_id in self._busy:
            raise Busy(server_id, self._busy[server_id][1])
        job = self.repo.insert_job(kind, title, server_id)
        if server_id is not None:
            self._busy[server_id] = (job["id"], kind)
        self._publish(job)
        self._running[job["id"]] = kind
        task = asyncio.create_task(self._run(job, fn), name=f"job-{kind}-{job['id']}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: dict[str, Any], fn: JobFn) -> None:
        ctx = JobContext(self, job)
        status, message = "success", None
        try:
            await fn(ctx)
        except UserError as exc:
            status, message = "failed", str(exc)
            ctx.step(message, "error")
        except asyncio.CancelledError:
            status, message = "cancelled", "Cancelled"
            raise
        except Exception as exc:
            log.exception("Job %s (%s) crashed", job["id"], job["kind"])
            status, message = "failed", f"Unexpected error: {exc}"
            ctx.step(message, "error")
        finally:
            self._running.pop(job["id"], None)  # before the row says "finished", so whoever sees that can rely on it
            if ctx.server_id is not None and self._busy.get(ctx.server_id, (None,))[0] == job["id"]:
                self._busy.pop(ctx.server_id, None)
            fields: dict[str, Any] = {"status": status, "finished_at": iso(), "progress": 1.0 if status == "success" else None}
            if message:
                fields["message"] = message
            elif status == "success":
                fields["message"] = "Done"
            self._update(job["id"], **fields)
            if self.on_finish:
                try:
                    self.on_finish(self.repo.get_job(job["id"]) or job)
                except Exception:
                    log.exception("job finish hook failed")

    async def cancel_all(self, timeout: float = 10.0) -> int:
        """Cancel running jobs (used on shutdown, before servers are stopped, so a stop never looks like a failed update)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return len(tasks)

    async def wait_idle(self, timeout: float = 30.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    # ------------------------------------------------------------------ plumbing
    def _update(self, job_id: str, **fields: Any) -> None:
        self.repo.update_job(job_id, **fields)
        job = self.repo.get_job(job_id)
        if job:
            self._publish(job)

    def _publish(self, job: dict[str, Any]) -> None:
        self.events.publish("jobs", "job", job)
