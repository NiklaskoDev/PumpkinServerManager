"""Installing a mod loader (Fabric, Quilt, Forge, NeoForge) into a server folder and finding out how to start it.

What the loaders leave behind differs: Fabric and Quilt give a launcher jar, old Forge versions a ``forge-*.jar``, and
Forge and NeoForge since Minecraft 1.17 an *argument file* that is handed to ``java`` as ``@file``. The result is
described as ``{"jar": "<file>"}`` or ``{"argfile": "<file>"}`` (paths relative to the server folder).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from ..errors import UserError
from ..util import plain_environment
from .net import BadDownloadError, DownloadError, download, get_json

log = logging.getLogger(__name__)

FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged"
QUILT_DOWNLOAD_HOSTS = frozenset({"maven.quiltmc.org"})  # where Quilt's metadata sends installers; nothing else is followed

LOADERS = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_ZIP_MAGIC = b"PK\x03\x04"


class LoaderError(UserError):
    """The loader could not be installed or found; the message is safe to show to users."""


@dataclass(frozen=True)
class LoaderSpec:
    kind: str  # fabric | quilt | forge | neoforge
    version: str
    minecraft: str

    def __post_init__(self) -> None:
        if self.kind not in LOADERS:
            raise LoaderError(f"Unknown mod loader '{self.kind}'. Supported: {', '.join(LOADERS.values())}.")
        for value in (self.version, self.minecraft):
            if not _VERSION_RE.match(value) or ".." in value:  # these end up in download addresses
                raise LoaderError(f"'{value}' is not a valid version number.")

    @property
    def label(self) -> str:
        return f"{LOADERS[self.kind]} {self.version}"


# A runner starts ``argv`` in ``cwd``, hands every output line to ``on_line`` and returns (exit code, last output lines).
Runner = Callable[[list[str], Path, Callable[[str], None]], Awaitable[tuple[int, list[str]]]]


async def run_process(argv: list[str], cwd: Path, on_line: Callable[[str], None], timeout: float = 1800.0) -> tuple[int, list[str]]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=plain_environment(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, limit=1 << 20,
        )
    except OSError as exc:
        raise LoaderError(f"Could not start Java: {exc}") from exc
    tail: deque[str] = deque(maxlen=40)

    async def pump() -> None:
        assert proc.stdout is not None
        while raw := await proc.stdout.readline():
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail.append(line)
                on_line(line)

    try:
        await asyncio.wait_for(pump(), timeout)
        code = await proc.wait()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise LoaderError(f"The installer did not finish within {int(timeout // 60)} minutes.") from None
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return code, list(tail)


def launch_arguments(launch: dict[str, str]) -> list[str]:
    """The ``java`` arguments that start the installed loader (after the memory options)."""
    if launch.get("argfile"):
        return [f"@{launch['argfile']}"]
    if launch.get("jar"):
        return ["-jar", launch["jar"]]
    raise LoaderError("This server has no launch information; reinstall the modpack.")


def find_launch(server_dir: Path, kind: str) -> dict[str, str] | None:
    """Work out how to start the loader from what is on disk (also used to repair a server after a restore)."""

    def newest(paths: list[Path]) -> Path | None:
        return max(paths, key=lambda p: p.stat().st_mtime) if paths else None

    if kind in ("forge", "neoforge"):
        args_name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
        groups = ("minecraftforge/forge",) if kind == "forge" else ("neoforged/neoforge", "neoforged/forge")
        for group in groups:
            found = newest(list((server_dir / "libraries" / "net" / group).glob(f"*/{args_name}")))
            if found:
                return {"argfile": found.relative_to(server_dir).as_posix()}
        if kind == "forge":  # Minecraft 1.16 and older: a plain jar
            jar = newest([p for p in server_dir.glob("forge-*.jar") if "installer" not in p.name])
            if jar:
                return {"jar": jar.name}
        return None
    # Fabric and Quilt leave a launcher; the servers the manager sets up without a modpack keep their jar under a fixed name.
    name = {
        "fabric": "fabric-server-launch.jar", "quilt": "quilt-server-launch.jar", "vanilla": "server.jar", "paper": "paper.jar",
    }.get(kind)
    return {"jar": name} if name and (server_dir / name).is_file() else None


class LoaderInstaller:
    def __init__(self, client: httpx.AsyncClient, run: Runner = run_process, retry_delay: float = 2.0):
        self._client = client
        self._run = run
        self._delay = retry_delay

    async def install(self, spec: LoaderSpec, server_dir: Path, java: Path, say: Callable[[str], None]) -> dict[str, str]:
        """Install ``spec`` into ``server_dir`` and return how to start it. ``say`` receives short progress messages."""
        say(f"Installing {spec.label} for Minecraft {spec.minecraft}")
        if spec.kind == "fabric":
            launch = await self._fabric(spec, server_dir)
        elif spec.kind == "quilt":
            launch = await self._quilt(spec, server_dir, java, say)
        else:
            launch = await self._forge_like(spec, server_dir, java, say)
        found = find_launch(server_dir, spec.kind) or launch
        if found is None:
            raise LoaderError(f"{spec.label} was installed, but the files needed to start it are missing.")
        return found

    # ------------------------------------------------------------------- Fabric
    async def _fabric(self, spec: LoaderSpec, server_dir: Path) -> dict[str, str]:
        installers = await get_json(self._client, f"{FABRIC_META}/versions/installer", what="Fabric's download service", delay=self._delay)
        installer = next((i["version"] for i in installers if i.get("stable")), installers[0]["version"] if installers else None)
        if installer is None:
            raise LoaderError("Fabric's download service did not name an installer version.")
        url = f"{FABRIC_META}/versions/loader/{quote(spec.minecraft)}/{quote(spec.version)}/{quote(installer)}/server/jar"
        target = server_dir / "fabric-server-launch.jar"
        await download(self._client, url, target, what=f"the Fabric launcher {spec.version}", delay=self._delay)
        if target.read_bytes()[:4] != _ZIP_MAGIC:
            target.unlink(missing_ok=True)
            raise BadDownloadError(f"Fabric did not send a launcher for Minecraft {spec.minecraft} with loader {spec.version}.")
        return {"jar": target.name}

    # -------------------------------------------------------------------- Quilt
    async def _quilt(self, spec: LoaderSpec, server_dir: Path, java: Path, say: Callable[[str], None]) -> dict[str, str]:
        installers = await get_json(self._client, f"{QUILT_META}/versions/installer", what="Quilt's download service", delay=self._delay)
        if not installers:
            raise LoaderError("Quilt's download service did not name an installer version.")
        newest = installers[0]
        jar = server_dir / f"quilt-installer-{newest['version']}.jar"
        # Quilt's metadata lists a size and hashes for the installer, but the real service's SHA-256 for 0.15.1 does not match the
        # file (nor its Maven repository), so the checksum is the one the repository publishes next to the file, like for Forge.
        await download(
            self._client, newest["url"], jar, sha256=await self._checksum(newest["url"]), allowed_hosts=QUILT_DOWNLOAD_HOSTS,
            what="the Quilt installer", delay=self._delay,
        )
        argv = [
            str(java), "-jar", jar.name, "install", "server", spec.minecraft, spec.version, "--download-server",
            f"--install-dir={server_dir}",
        ]
        await self._run_installer(argv, server_dir, say, "Quilt")
        jar.unlink(missing_ok=True)
        return {"jar": "quilt-server-launch.jar"}

    # --------------------------------------------------------------- Forge / NeoForge
    def _installer_addresses(self, spec: LoaderSpec) -> list[str]:
        mc, version = spec.minecraft, spec.version
        if spec.kind == "forge":
            names = [f"{mc}-{version}", f"{mc}-{version}-{mc}"]  # the second form is what Minecraft 1.7.10 builds are called
            return [f"{FORGE_MAVEN}/{n}/forge-{n}-installer.jar" for n in names]
        if mc == "1.20.1":  # NeoForge for 1.20.1 still used the old name
            return [f"{NEOFORGE_MAVEN}/forge/{mc}-{version}/forge-{mc}-{version}-installer.jar"]
        return [f"{NEOFORGE_MAVEN}/neoforge/{version}/neoforge-{version}-installer.jar"]

    async def _checksum(self, url: str) -> str | None:
        """The SHA-256 the Maven repository publishes next to a file, if it does."""
        try:
            resp = await self._client.get(f"{url}.sha256")
        except httpx.HTTPError:
            return None
        text = resp.text.split()[0].lower() if resp.status_code == 200 and resp.text.split() else ""
        return text if re.fullmatch(r"[0-9a-f]{64}", text) else None

    async def _forge_like(self, spec: LoaderSpec, server_dir: Path, java: Path, say: Callable[[str], None]) -> dict[str, str] | None:
        installer = server_dir / f"{spec.kind}-installer.jar"
        last: DownloadError | None = None
        for url in self._installer_addresses(spec):
            try:
                await download(
                    self._client, url, installer, sha256=await self._checksum(url), what=f"the {LOADERS[spec.kind]} installer",
                    attempts=2, delay=self._delay,
                )
                break
            except BadDownloadError:
                raise
            except DownloadError as exc:
                last = exc
        else:
            raise LoaderError(
                f"{spec.label} for Minecraft {spec.minecraft} could not be downloaded: {last}. Is that version number right?"
            ) from last
        try:
            await self._run_installer([str(java), "-jar", installer.name, "--installServer"], server_dir, say, LOADERS[spec.kind])
        finally:
            installer.unlink(missing_ok=True)
            (server_dir / f"{installer.name}.log").unlink(missing_ok=True)
        return find_launch(server_dir, spec.kind)

    # ----------------------------------------------------------------- installers
    async def _run_installer(self, argv: list[str], cwd: Path, say: Callable[[str], None], name: str) -> None:
        say(f"Running the {name} installer (this needs the internet and takes a minute or two)")
        code, tail = await self._run(argv, cwd, lambda line: say(line[:160]))
        if code != 0:
            detail = " | ".join(tail[-6:]) or "no output"
            raise LoaderError(f"The {name} installer failed (exit code {code}): {detail}")
