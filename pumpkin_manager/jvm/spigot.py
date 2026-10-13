"""Spigot and BungeeCord, the two projects of SpigotMC.

BungeeCord is an ordinary download: SpigotMC's build server publishes every successful build of the proxy.

Spigot is not. SpigotMC does not distribute the server (it is made from Mojang's code), it distributes a tool, BuildTools, that makes it on
the machine that runs it: from SpigotMC's sources and the Minecraft server Mojang publishes. That takes five to fifteen minutes and needs a
JDK (a compiler: the runtimes the servers use are JREs), git, and several gigabytes of disk while it runs. ``SpigotBuilder`` does it: it
downloads BuildTools, runs it in a folder of its own with its own home (so nothing is left in the user's), keeps the one jar it makes, and
deletes the rest. ``SpigotHub`` reads what SpigotMC publishes about the Minecraft versions it builds and about BungeeCord's builds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from ..errors import UserError
from ..util import free_bytes, human_bytes, iso, plain_environment
from .net import DownloadError, ProgressCallback, download, get_json, get_text
from .runtime import JavaRuntimes

log = logging.getLogger(__name__)

HUB = "https://hub.spigotmc.org"
HOSTS = frozenset({"hub.spigotmc.org"})
VERSIONS_URL = f"{HUB}/versions/"
BUILDTOOLS_URL = f"{HUB}/jenkins/job/BuildTools/lastSuccessfulBuild/artifact/target/BuildTools.jar"
BUNGEE_JOB = f"{HUB}/jenkins/job/BungeeCord"
BUNGEE_JAR = "artifact/bootstrap/target/BungeeCord.jar"
SPIGOT_JAR = "spigot.jar"  # what the manager calls the server it built
BUNGEE_JAR_NAME = "BungeeCord.jar"
MAX_BUNGEE_BUILDS = 60  # how many of the newest builds are asked for (there are thousands)
MAX_BUILDTOOLS_BYTES = 64 * 1024 * 1024  # it is 4 MB; the ceiling is only there so that a wrong address cannot fill the disk
MAX_BUNGEE_BYTES = 256 * 1024 * 1024  # it is about 26 MB
MIN_FREE_BYTES = 2 * 1024**3  # a build peaks at about 1 GB (the sources, Minecraft decompiled, Maven's downloads); the rest is a margin
BUILD_TIMEOUT = 3 * 3600.0  # a slow machine needs a quarter of an hour; three hours is a build that does not end
IDLE_TIMEOUT = 20 * 60.0  # BuildTools prints something every few seconds; twenty quiet minutes is a build that hangs (a network stall)

_INDEX_LINK = re.compile(r'<a href="(?P<id>[0-9][0-9A-Za-z.+_-]*)\.json">[^<]*</a>\s+(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})')
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


class BuildError(UserError):
    """Spigot could not be built (or the tools for it are missing); the message says what to do about it."""

    status = 502


def _index_date(text: str) -> str | None:
    """'04-Oct-2026 02:43' (how the version list writes when a file changed) as the manager writes times."""
    try:
        day, month, rest = text.split("-", 2)
        year, clock = rest.split(" ", 1)
        hour, minute = clock.split(":")
        return iso(datetime(int(year), _MONTHS[month], int(day), int(hour), int(minute), tzinfo=UTC))
    except (ValueError, KeyError):
        return None


def java_floor(info: dict[str, Any]) -> int | None:
    """The oldest Java that can build a Minecraft version, from the class-file versions SpigotMC lists (``[65, 68]`` is Java 21 to 24)."""
    versions = info.get("javaVersions")
    if isinstance(versions, list) and versions and isinstance(versions[0], int) and not isinstance(versions[0], bool) and versions[0] > 44:
        return versions[0] - 44
    return None


# ------------------------------------------------------------------------------------------------------------ what SpigotMC publishes
class SpigotHub:
    """SpigotMC's list of the Minecraft versions it builds (a file per version with the build that is current), and the builds of
    BungeeCord on its build server."""

    def __init__(self, client: httpx.AsyncClient, retry_delay: float = 1.5):
        self._client = client
        self._delay = retry_delay

    async def versions(self) -> list[tuple[str, str | None]]:
        """Every version SpigotMC has a file for (snapshots included) with the time its file last changed, which is when its newest build
        was published. The caller picks the releases."""
        text = await get_text(self._client, VERSIONS_URL, what="SpigotMC's list of versions", allowed_hosts=HOSTS, delay=self._delay)
        found = [(m["id"], _index_date(m["date"])) for m in _INDEX_LINK.finditer(text) if m["id"] != "latest"]
        if not found:
            raise DownloadError("SpigotMC's list of versions is empty or has changed its layout.")
        return found

    async def version_info(self, minecraft: str) -> dict[str, Any]:
        """What SpigotMC says about the newest build for one Minecraft version: its name (the Jenkins build number, sometimes with a
        letter), the git commits it is made from, the Java it can be built with. Raises a 404 ``DownloadError`` for an unknown version."""
        data = await get_json(self._client, f"{VERSIONS_URL}{minecraft}.json", what="SpigotMC's list of versions", delay=self._delay)
        if not isinstance(data, dict) or not str(data.get("name") or "").strip():
            raise DownloadError(f"SpigotMC's file for Minecraft {minecraft} is not what was expected.")
        return data

    async def bungee_builds(self, limit: int = MAX_BUNGEE_BUILDS) -> list[dict[str, Any]]:
        """The newest successful builds of BungeeCord, newest first: ``{"number", "published"}``."""
        data = await get_json(
            self._client, f"{BUNGEE_JOB}/api/json", params={"tree": f"builds[number,result,timestamp]{{0,{limit}}}"},
            what="SpigotMC's build server", delay=self._delay,
        )
        found = []
        for entry in (data.get("builds") if isinstance(data, dict) else None) or []:
            if not isinstance(entry, dict) or entry.get("result") != "SUCCESS" or not isinstance(entry.get("number"), int):
                continue
            stamp = entry.get("timestamp")
            published = iso(datetime.fromtimestamp(stamp / 1000, UTC)) if isinstance(stamp, int | float) and stamp > 0 else None
            found.append({"number": entry["number"], "published": published})
        if not found:
            raise DownloadError("SpigotMC's build server lists no successful BungeeCord build.")
        return sorted(found, key=lambda b: b["number"], reverse=True)

    @staticmethod
    def bungee_url(number: int | str) -> str:
        return f"{BUNGEE_JOB}/{int(number)}/{BUNGEE_JAR}"


# --------------------------------------------------------------------------------------------------------------------- running it
# A runner starts BuildTools: (argv, folder, environment, line callback) -> (exit code, the last lines). Tests put a fake in its place.
BuildRunner = Callable[[list[str], Path, dict[str, str], Callable[[str], None]], Awaitable[tuple[int, list[str]]]]

# What BuildTools prints as it goes, and how far along that is (it is a long job, so the person is told where it is).
_STAGES: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"^Starting clone of"), 0.04, "Downloading Spigot's source code"),
    (re.compile(r"^Maven does not exist"), 0.09, "Downloading Maven, the tool that compiles it"),
    (re.compile(r"^Attempting to build Minecraft"), 0.12, "Getting Minecraft's server from Mojang"),
    (re.compile(r"^Final mapped jar"), 0.15, "Giving Minecraft's classes readable names"),
    (re.compile(r"^(INFO:\s+)?Decompiling class"), 0.24, "Decompiling Minecraft (the slowest part)"),
    (re.compile(r"^Applying CraftBukkit Patches"), 0.62, "Applying CraftBukkit's changes"),
    (re.compile(r"^\s*Rebuilding Forked projects"), 0.70, "Applying Spigot's changes"),
    (re.compile(r"^\[INFO\] Building (Spigot-API|Spigot) "), 0.80, "Compiling Spigot"),
    (re.compile(r"^Success! Everything completed"), 0.97, "Spigot is built"),
)
# Lines that say nothing (thousands of them): not shown, not kept for an error message
_NOISE = re.compile(r"^(Extracted: |Progress \(|Downloading from |Downloaded from |Picked up JAVA_TOOL_OPTIONS|INFO:\s+Decompiling |Patching |\s*$)")


class BuildProgress:
    """Follows BuildTools' output and says where the build is: each stage once, in order, never going back."""

    def __init__(self, report: Callable[[float, str], None]):
        self._report = report
        self._at = 0.0

    def feed(self, line: str) -> None:
        for pattern, fraction, message in _STAGES:
            if pattern.search(line):
                if fraction > self._at:
                    self._at = fraction
                    self._report(fraction, message)
                return


def remove_tree(path: Path) -> None:
    """Delete a folder with everything in it, including what git made read-only (its objects are: Windows refuses to delete such files
    until the flag is cleared, and the folder of a build would stay behind with half a gigabyte of them)."""

    def retry(function: Callable[[str], object], name: str, error: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
            function(name)

    shutil.rmtree(path, onexc=retry)


def kill_tree(pid: int) -> None:
    """End a process and everything it started (BuildTools starts git, Maven and more JVMs)."""
    try:
        parent = psutil.Process(pid)
        everything = [*parent.children(recursive=True), parent]
    except psutil.Error:
        return
    for proc in everything:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(everything, timeout=3)


async def run_build(
    argv: list[str], cwd: Path, env: dict[str, str], on_line: Callable[[str], None], *, timeout: float = BUILD_TIMEOUT, idle: float = IDLE_TIMEOUT,
) -> tuple[int, list[str]]:
    """Run BuildTools and hand every line it prints to ``on_line``. It is stopped (with everything it started) when it runs for longer
    than ``timeout`` seconds or says nothing for ``idle`` seconds, and when the job that started it is cancelled."""
    if os.name != "nt" and shutil.which("git", path=env.get("PATH")) is None:
        raise BuildError(
            "Spigot is built with git, and git is not installed here. The container image has it since the version that added Spigot: "
            "update the container. Outside the container, install git."
        )
    creation: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            limit=1 << 20, **creation,
        )
    except OSError as exc:
        raise BuildError(f"Could not start Java to build Spigot: {exc}") from exc
    tail: deque[str] = deque(maxlen=60)
    last = [time.monotonic()]

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # a line over 1 MiB: dropped
            if not raw:
                return
            last[0] = time.monotonic()
            line = raw.decode("utf-8", "replace").rstrip()
            on_line(line)
            if not _NOISE.match(line):
                tail.append(line)  # what an error message quotes: not the thousands of lines that say nothing

    async def watch() -> str:
        started = time.monotonic()
        while True:
            await asyncio.sleep(min(10.0, max(0.05, idle / 4)))
            now = time.monotonic()
            if now - started > timeout:
                return f"it did not finish within {int(timeout // 3600)} hours"
            if now - last[0] > idle:
                return f"it printed nothing for {int(idle // 60)} minutes (the internet connection may have stalled)"

    pumping, watching = asyncio.ensure_future(pump()), asyncio.ensure_future(watch())
    try:
        await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
        if pumping.done():
            pumping.result()  # raises what went wrong while reading, if anything
            await proc.wait()
        else:
            reason = watching.result()
            kill_tree(proc.pid)
            raise BuildError(f"BuildTools was stopped: {reason}.")
    except asyncio.CancelledError:
        kill_tree(proc.pid)
        raise
    finally:
        for task in (pumping, watching):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(pumping, watching, return_exceptions=True)
        if proc.returncode is None:
            kill_tree(proc.pid)
    return int(proc.returncode or 0), list(tail)


_PROBLEM = re.compile(r"\[ERROR\]|Exception|\bFATAL\b|\bfailed\b|Could not|Unable to|Error:", re.IGNORECASE)


def failure_detail(tail: list[str], count: int = 5) -> str:
    """What to quote of a failed build: the lines that name the problem (Maven's ``[ERROR]`` lines, a Java exception) if there are any among
    the last ones, else simply the last ones."""
    problems = [line for line in tail if _PROBLEM.search(line)]
    picked = problems[-count:] if problems else tail[-count:]
    return " | ".join(line.strip()[:200] for line in picked) or "no output"


def build_environment(jdk_home: Path, home: Path) -> dict[str, str]:
    """What BuildTools runs with: the JDK first on the path, a home of its own (Maven's repository, git's settings and the like land
    there and go with it), and an identity for git (``git am`` makes commits, and a fresh container has none configured)."""
    env = plain_environment()
    env["PATH"] = os.pathsep.join(p for p in (str(jdk_home / "bin"), env.get("PATH", "")) if p)
    env["JAVA_HOME"] = str(jdk_home)
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    option = f"-Duser.home={home}"  # every JVM BuildTools starts reads this variable, so all of them agree on where "home" is
    env["JAVA_TOOL_OPTIONS"] = f'"{option}"' if " " in option else option
    env.update(
        GIT_AUTHOR_NAME="Pumpkin Server Manager", GIT_AUTHOR_EMAIL="manager@localhost", GIT_COMMITTER_NAME="Pumpkin Server Manager",
        GIT_COMMITTER_EMAIL="manager@localhost", GIT_TERMINAL_PROMPT="0",
    )
    return env


def _check_jar(path: Path, what: str) -> None:
    """A jar that arrived whole: a zip whose directory can be read (a cut-off download cannot)."""
    try:
        with zipfile.ZipFile(path) as zf:
            if not zf.namelist():
                raise BuildError(f"{what} is an empty archive.")
    except zipfile.BadZipFile as exc:
        raise BuildError(f"{what} is not a complete jar file ({exc}).") from exc


class SpigotBuilder:
    """Makes ``spigot.jar`` for one Minecraft version with SpigotMC's BuildTools. One build at a time: it uses all the processors."""

    def __init__(self, client: httpx.AsyncClient, jdks: JavaRuntimes, work_dir: Path, run: BuildRunner = run_build, retry_delay: float = 2.0):
        self._client = client
        self._jdks = jdks
        self._work = work_dir
        self._run = run
        self._delay = retry_delay
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        """Is a build running (and a development kit in use)?"""
        return self._lock.locked()

    def purge(self) -> int:
        """Remove what builds that never finished (the manager was stopped meanwhile) left behind. Nothing while a build is running."""
        if self._lock.locked() or not self._work.is_dir():
            return 0
        return self._clear()

    def _clear(self) -> int:
        removed = 0
        for folder in self._work.iterdir():  # only builds put anything here, one at a time: nothing in it is in use now
            remove_tree(folder)
            removed += 1
        return removed

    async def build(
        self, minecraft: str, directory: Path, java_major: int, say: Callable[[str], None], progress: ProgressCallback | None = None,
        jdk_progress: ProgressCallback | None = None,
    ) -> Path:
        """Build Spigot for ``minecraft`` with Java ``java_major`` and leave ``spigot.jar`` in ``directory``. ``progress`` is told
        how far along it is as ``(done, 1000)``; ``say`` hears what is happening."""
        if self._lock.locked():
            say("Another Spigot build is running; this one waits for it")
        async with self._lock:
            self._work.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._clear)  # what a build that died left behind can be half a gigabyte
            if free_bytes(self._work) < MIN_FREE_BYTES:
                raise BuildError(
                    f"Not enough free disk space to build Spigot: about {human_bytes(MIN_FREE_BYTES)} are needed while it is built "
                    f"({human_bytes(free_bytes(self._work))} are free in {self._work}). The space is given back when it is done."
                )
            say(f"Getting a Java {java_major} development kit (Spigot is compiled, which a runtime that only runs servers cannot do)")
            try:
                jdk = await self._jdks.ensure(java_major, jdk_progress)
            except DownloadError as exc:
                raise BuildError(f"The Java {java_major} development kit that builds Spigot could not be downloaded: {exc}") from exc
            folder = self._work / uuid.uuid4().hex[:8]  # short: Windows cannot open paths over 260 characters, and a build makes deep ones
            home, out = folder / "home", folder / "out"
            for sub in (home, out):
                sub.mkdir(parents=True)
            try:
                say("Downloading BuildTools from SpigotMC")
                tools = folder / "BuildTools.jar"
                try:
                    await download(
                        self._client, BUILDTOOLS_URL, tools, max_size=MAX_BUILDTOOLS_BYTES, allowed_hosts=HOSTS, what="SpigotMC's BuildTools",
                        delay=self._delay,
                    )
                except DownloadError as exc:
                    raise BuildError(str(exc)) from exc
                _check_jar(tools, "The BuildTools download")
                argv = [
                    str(jdk.java), "-jar", tools.name, "--rev", minecraft, "--nogui", "--compile", "SPIGOT", "--final-name", SPIGOT_JAR, "-o",
                    str(out),
                ]
                say(f"Building Spigot for Minecraft {minecraft}: this takes five to fifteen minutes and needs the internet")

                def report(fraction: float, message: str) -> None:
                    say(message)
                    if progress:
                        progress(int(fraction * 1000), 1000)

                stages = BuildProgress(report)
                code, tail = await self._run(argv, folder, build_environment(jdk.path, home), stages.feed)
                if code != 0:
                    raise BuildError(f"BuildTools failed (exit code {code}): {failure_detail(tail)}")
                built = out / SPIGOT_JAR
                if not built.is_file():
                    raise BuildError("BuildTools said it was done but left no jar file behind.")
                _check_jar(built, "The jar BuildTools made")
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / SPIGOT_JAR
                await asyncio.to_thread(shutil.move, str(built), str(target))
                return target
            finally:
                await asyncio.to_thread(remove_tree, folder)


__all__ = [
    "BUNGEE_JAR_NAME", "BuildError", "BuildProgress", "BuildRunner", "HOSTS", "SPIGOT_JAR", "SpigotBuilder", "SpigotHub", "build_environment",
    "java_floor", "run_build",
]
