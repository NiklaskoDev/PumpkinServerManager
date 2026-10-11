"""Supervision of Pumpkin child processes: start, console I/O, graceful stop, crash detection, stats."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from .errors import UserError
from .events import EventBus
from .mcnet import PingError, server_list_ping
from .util import iso, strip_ansi, utcnow

log = logging.getLogger(__name__)

READY_RE = re.compile(r"server is now running", re.IGNORECASE)
PUMPKIN_START_HINT = "if Pumpkin rejected pumpkin.toml it silently falls back to default settings."
MAX_LINE = 8000
MAX_LOG_FILE_BYTES = 20 * 1024 * 1024
BACKLOG_LINES = 3000

STATES = ("stopped", "starting", "running", "stopping", "crashed")


class ProcessError(UserError):
    """Raised for start/stop problems; the message is safe to show in the UI."""


@dataclass
class Runtime:
    server_id: str
    state: str = "stopped"
    proc: asyncio.subprocess.Process | None = None
    pid: int | None = None
    started_at: float | None = None
    exit_code: int | None = None
    last_error: str | None = None
    stopping_requested: bool = False
    ready_re: re.Pattern[str] = READY_RE  # the console line that says the server is up
    timeout_hint: str = PUMPKIN_START_HINT  # what to suggest when it never comes up
    strict_ping: bool = False  # a server-list reply only counts as "up" when it names a version (Forge answers early with none)
    log: deque = field(default_factory=lambda: deque(maxlen=BACKLOG_LINES))
    seq: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    # live information refreshed by the monitor
    players_online: int = 0
    players_max: int = 0
    player_names: list[str] = field(default_factory=list)
    mc_version: str | None = None
    motd: str | None = None
    ping_ok: bool = False
    msmp_ok: bool = False
    cpu_percent: float = 0.0
    rss_bytes: int = 0
    cpu_history: deque = field(default_factory=lambda: deque(maxlen=60))
    mem_history: deque = field(default_factory=lambda: deque(maxlen=60))
    crash_times: list[float] = field(default_factory=list)
    _psutil: psutil.Process | None = None
    _pending: list[dict[str, Any]] = field(default_factory=list)
    _flush_handle: asyncio.TimerHandle | None = None
    _logfile: Any = None
    _logbytes: int = 0
    _tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.state in ("starting", "running", "stopping")

    def uptime(self) -> float | None:
        if self.started_at is None or self.state not in ("running", "stopping", "starting"):
            return None
        return max(0.0, time.time() - self.started_at)


class ProcessManager:
    def __init__(self, logs_dir: Path, events: EventBus):
        self.logs_dir = logs_dir
        self.events = events
        self._runtimes: dict[str, Runtime] = {}
        self.on_change: Callable[[str], None] | None = None
        self.on_exit: Callable[[str, int | None, bool], Awaitable[None]] | None = None

    # ------------------------------------------------------------------ access
    def runtime(self, server_id: str) -> Runtime:
        rt = self._runtimes.get(server_id)
        if rt is None:
            rt = self._runtimes[server_id] = Runtime(server_id)
        return rt

    def running_ids(self) -> list[str]:
        return [sid for sid, rt in self._runtimes.items() if rt.is_active]

    def forget(self, server_id: str) -> None:
        rt = self._runtimes.pop(server_id, None)
        if rt and rt._logfile:
            with contextlib.suppress(Exception):
                rt._logfile.close()

    def backlog(self, server_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rt = self._runtimes.get(server_id)
        return list(rt.log)[-limit:] if rt else []

    # ---------------------------------------------------------------- lifecycle
    async def start(
        self,
        server_id: str,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        ready_port: int | None,
        ready_timeout: float,
        ready_re: re.Pattern[str] | None = None,
        timeout_hint: str | None = None,
        strict_ping: bool = False,
    ) -> None:
        rt = self.runtime(server_id)
        if rt.is_active:
            raise ProcessError(f"The server is already {rt.state}.")

        self._reset_for_start(rt)
        rt.ready_re = ready_re or READY_RE
        rt.timeout_hint = PUMPKIN_START_HINT if timeout_hint is None else timeout_hint
        rt.strict_ping = strict_ping
        self._open_logfile(rt)
        self._sys(rt, f"Starting: {' '.join(argv)}")
        creation: dict[str, Any] = {}
        if os.name == "nt":
            creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creation["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, limit=1 << 20, **creation,
            )
        except (OSError, ValueError) as exc:
            self._finish_failed_start(rt, f"Could not launch the server process: {exc}")
            raise ProcessError(rt.last_error or str(exc)) from exc

        rt.proc, rt.pid, rt.started_at = proc, proc.pid, time.time()
        self._set_state(rt, "starting")
        pump = asyncio.create_task(self._pump(rt, proc), name=f"pump-{server_id}")
        watcher = asyncio.create_task(self._watch(rt, proc, pump), name=f"watch-{server_id}")
        rt._tasks = [pump, watcher]

        try:
            await self._wait_ready(rt, ready_port, ready_timeout)
        except ProcessError as exc:
            if not rt.exited.is_set():
                await self._terminate_hard(rt, grace=5)
            rt.last_error = str(exc)
            raise
        self._set_state(rt, "running")

    async def stop(self, server_id: str, timeout: float = 60.0) -> int | None:
        rt = self.runtime(server_id)
        if rt.proc is None or rt.exited.is_set():
            return rt.exit_code
        rt.stopping_requested = True
        if rt.state != "stopping":
            self._set_state(rt, "stopping")
        self._sys(rt, "Stopping the server...")
        with contextlib.suppress(ProcessError):
            await self._write_stdin(rt, "stop")
        if not await self._wait_exit(rt, timeout):
            self._sys(rt, f"The server did not stop within {int(timeout)}s; asking the process to terminate.")
            await self._terminate_hard(rt, grace=15)
        return rt.exit_code

    async def kill(self, server_id: str) -> None:
        rt = self.runtime(server_id)
        if rt.proc is None or rt.exited.is_set():
            return
        rt.stopping_requested = True
        self._set_state(rt, "stopping")
        self._sys(rt, "Killing the server process.")
        with contextlib.suppress(ProcessLookupError):
            rt.proc.kill()
        await self._wait_exit(rt, 10)

    async def send_command(self, server_id: str, command: str, echo: bool = True) -> None:
        rt = self.runtime(server_id)
        command = command.strip()
        if not command:
            return
        if "\n" in command or "\r" in command or len(command) > 1000:
            raise ProcessError("Commands must be a single line of at most 1000 characters.")
        if rt.state not in ("starting", "running"):
            raise ProcessError("The server is not running.")
        await self._write_stdin(rt, command)
        if echo:
            self._append(rt, "in", command)

    async def wait_for_output(self, server_id: str, pattern: re.Pattern[str], after: int, timeout: float) -> bool:
        """Wait for a console line matching ``pattern`` that the server printed after line number ``after`` (see ``Runtime.seq``)."""
        rt = self.runtime(server_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for line in reversed(rt.log):
                if line["n"] <= after:
                    break
                if line["s"] == "out" and pattern.search(strip_ansi(line["m"])):
                    return True
            if rt.exited.is_set() or loop.time() > deadline:
                return False
            await asyncio.sleep(0.2)

    async def shutdown_all(self, timeout_for: Callable[[str], float], overall: float) -> None:
        ids = self.running_ids()
        if not ids:
            return
        log.info("Stopping %d server(s)...", len(ids))
        tasks = [asyncio.create_task(self.stop(sid, timeout_for(sid))) for sid in ids]
        done, pending = await asyncio.wait(tasks, timeout=overall)
        for task in pending:
            task.cancel()
        for sid in ids:
            rt = self.runtime(sid)
            if rt.proc is not None and not rt.exited.is_set():
                log.warning("Server %s did not stop in time; killing it", sid)
                with contextlib.suppress(ProcessLookupError):
                    rt.proc.kill()

    # --------------------------------------------------------------- internals
    def _reset_for_start(self, rt: Runtime) -> None:
        rt.exit_code = None
        rt.last_error = None
        rt.stopping_requested = False
        rt.ready = asyncio.Event()
        rt.exited = asyncio.Event()
        rt.players_online = rt.players_max = 0
        rt.player_names = []
        rt.mc_version = rt.motd = None
        rt.ping_ok = rt.msmp_ok = False
        rt.cpu_percent, rt.rss_bytes, rt._psutil = 0.0, 0, None
        rt.cpu_history.clear()
        rt.mem_history.clear()

    def _open_logfile(self, rt: Runtime) -> None:
        directory = self.logs_dir / rt.server_id
        directory.mkdir(parents=True, exist_ok=True)
        current, previous = directory / "console.log", directory / "console.prev.log"
        if rt._logfile:
            with contextlib.suppress(Exception):
                rt._logfile.close()
        with contextlib.suppress(OSError):
            if current.exists():
                os.replace(current, previous)
        try:
            rt._logfile = open(current, "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError as exc:
            log.warning("Cannot open console log for %s: %s", rt.server_id, exc)
            rt._logfile = None
        rt._logbytes = 0

    async def _write_stdin(self, rt: Runtime, text: str) -> None:
        proc = rt.proc
        if proc is None or proc.stdin is None or rt.exited.is_set():
            raise ProcessError("The server process is not accepting input.")
        try:
            proc.stdin.write(text.encode("utf-8", "replace") + b"\n")
            await asyncio.wait_for(proc.stdin.drain(), 5)
        except (TimeoutError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ProcessError(f"Could not write to the server console: {exc}") from exc

    async def _pump(self, rt: Runtime, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                self._append(rt, "sys", "[manager] A console line exceeded 1 MiB and was dropped.")
                continue
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            self._append(rt, "out", text)
            if not rt.ready.is_set() and rt.ready_re.search(strip_ansi(text)):
                rt.ready.set()

    async def _watch(self, rt: Runtime, proc: asyncio.subprocess.Process, pump: asyncio.Task) -> None:
        code = await proc.wait()
        # Let the pump drain whatever the process printed right before exiting.
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(pump), 5)
        rt.exit_code = code
        unexpected = not rt.stopping_requested
        was_starting = rt.state == "starting"
        rt.proc, rt.pid, rt._psutil = None, None, None
        rt.exited.set()
        self._flush(rt)
        if unexpected:
            self._set_state(rt, "crashed")
            self._sys(rt, f"The server process exited unexpectedly (exit code {code}).")
            rt.last_error = rt.last_error or f"Exited unexpectedly with code {code}"
        else:
            self._set_state(rt, "stopped")
            self._sys(rt, f"The server stopped (exit code {code}).")
        with contextlib.suppress(Exception):
            if rt._logfile:
                rt._logfile.flush()
        if self.on_exit and not was_starting:
            # During startup the caller of start() reports the failure; only later crashes go to the policy.
            try:
                await self.on_exit(rt.server_id, code, unexpected)
            except Exception:
                log.exception("on_exit handler failed for %s", rt.server_id)

    @staticmethod
    async def _pause(rt: Runtime, seconds: float) -> None:
        """Sleep, but wake up as soon as the console says the server is up or the process ends."""
        waiting = [asyncio.ensure_future(rt.ready.wait()), asyncio.ensure_future(rt.exited.wait())]
        try:
            await asyncio.wait(waiting, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiting:
                task.cancel()

    async def _wait_ready(self, rt: Runtime, port: int | None, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # A Java server announces on its console when it is up. Pinging it meanwhile only fills that console (Forge logs every
        # ping it answers before the world is loaded), so it is asked rarely, and the console line ends the wait at once.
        pause = 5.0 if rt.strict_ping else 0.4
        while True:
            if rt.exited.is_set():
                raise ProcessError(self._describe_early_exit(rt))
            if rt.ready.is_set():
                return
            if port:
                try:
                    info = await server_list_ping("127.0.0.1", port, timeout=1.5)
                    if info["version"] or not rt.strict_ping:
                        return
                except PingError:
                    pass
            if loop.time() > deadline:
                raise ProcessError(
                    f"The server did not become ready within {int(timeout)} seconds. Check the console output"
                    f"{'; ' + rt.timeout_hint if rt.timeout_hint else '.'}"
                )
            await self._pause(rt, pause)

    def _describe_early_exit(self, rt: Runtime) -> str:
        tail = [strip_ansi(line["m"]) for line in list(rt.log)[-6:] if line["s"] == "out"]
        detail = f" Last output: {' | '.join(tail)}" if tail else ""
        return f"The server process exited during startup (exit code {rt.exit_code}).{detail}"

    async def _wait_exit(self, rt: Runtime, timeout: float) -> bool:
        try:
            await asyncio.wait_for(rt.exited.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def _terminate_hard(self, rt: Runtime, grace: float) -> None:
        proc = rt.proc
        if proc is None:
            return
        rt.stopping_requested = rt.stopping_requested or rt.state == "stopping"
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        if not await self._wait_exit(rt, grace):
            self._sys(rt, "The process ignored the termination request; killing it.")
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await self._wait_exit(rt, 10)

    def _finish_failed_start(self, rt: Runtime, message: str) -> None:
        rt.last_error = message
        self._sys(rt, message)
        self._set_state(rt, "stopped")

    def _set_state(self, rt: Runtime, state: str) -> None:
        if rt.state != state:
            rt.state = state
            if self.on_change:
                self.on_change(rt.server_id)

    # ------------------------------------------------------------- console log
    def _sys(self, rt: Runtime, text: str) -> None:
        self._append(rt, "sys", f"[manager] {text}")

    def _append(self, rt: Runtime, stream: str, text: str) -> None:
        if len(text) > MAX_LINE:
            text = text[:MAX_LINE] + "…"
        rt.seq += 1
        line = {"n": rt.seq, "t": round(time.time(), 3), "s": stream, "m": text}
        rt.log.append(line)
        if rt._logfile is not None and rt._logbytes < MAX_LOG_FILE_BYTES:
            try:
                stamp = iso(utcnow())
                payload = f"{stamp} [{stream}] {text}\n"
                rt._logfile.write(payload)
                rt._logbytes += len(payload)
                if rt._logbytes >= MAX_LOG_FILE_BYTES:
                    rt._logfile.write(f"{stamp} [sys] [manager] Console log size limit reached; no more lines are saved.\n")
            except (OSError, ValueError):
                rt._logfile = None
        rt._pending.append(line)
        if rt._flush_handle is None:
            try:
                rt._flush_handle = asyncio.get_running_loop().call_later(0.05, self._flush, rt)
            except RuntimeError:
                pass

    def _flush(self, rt: Runtime) -> None:
        rt._flush_handle = None
        if rt._pending:
            lines, rt._pending = rt._pending, []
            self.events.publish(f"console:{rt.server_id}", "console", {"server_id": rt.server_id, "lines": lines})

    # ------------------------------------------------------------------- stats
    def sample_stats(self, server_id: str) -> None:
        rt = self._runtimes.get(server_id)
        if rt is None or rt.pid is None or rt.state not in ("starting", "running", "stopping"):
            return
        try:
            if rt._psutil is None or rt._psutil.pid != rt.pid:
                rt._psutil = psutil.Process(rt.pid)
                rt._psutil.cpu_percent(None)
            procs = [rt._psutil, *rt._psutil.children(recursive=True)]
            cpu = rss = 0.0
            for p in procs:
                with contextlib.suppress(psutil.Error):
                    cpu += p.cpu_percent(None)
                    rss += p.memory_info().rss
            cores = psutil.cpu_count() or 1
            rt.cpu_percent = round(min(100.0, cpu / cores), 1)
            rt.rss_bytes = int(rss)
            rt.cpu_history.append(rt.cpu_percent)
            rt.mem_history.append(rt.rss_bytes)
        except psutil.Error:
            rt._psutil = None
