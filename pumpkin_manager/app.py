"""Application factory: wires the services together and exposes them over HTTP."""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .access import AccessService
from .accounts import AccountService
from .backups import BackupService
from .binaries import BuildStore
from .config import Settings
from .db import Database
from .errors import UserError
from .events import EventBus
from .jobs import JobService
from .jvm.curseforge import CurseForgeClient
from .jvm.gtnh import GtnhCatalog
from .jvm.loaders import LoaderInstaller, Runner, run_process
from .jvm.modrinth import ModrinthClient
from .jvm.mojang import MojangMeta
from .jvm.mrpack import ModCache, PackInstaller
from .jvm.net import new_client
from .jvm.packupdates import PackUpdater
from .jvm.runtime import JavaDownloads, JavaRuntimes, JavaSource, run_java_version
from .jvm.service import JavaServerService
from .jvm.software import SoftwareCatalog, SoftwareInstaller
from .jvm.softwareupdates import SoftwareUpdater
from .jvm.spigot import BuildRunner, SpigotBuilder, run_build
from .models import AppSettings
from .notify import Notifier
from .process import ProcessManager
from .pumpkin_config import ConfigError
from .releases import GitHubReleaseSource, ReleaseSource
from .repo import Repo
from .reporter import Reporter
from .scheduler import Scheduler
from .security import AuthService
from .servers import CommandBuilder, ServerService
from .updater import UpdateService
from .util import memory_info

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class AppContext:
    """Owns every service for the lifetime of the process."""

    def __init__(
        self,
        settings: Settings,
        *,
        release_source: ReleaseSource | None = None,
        command_builder: CommandBuilder | None = None,
        verify_elf: bool = True,
        background: bool = True,
        stability_seconds: float | None = None,
        http_client: httpx.AsyncClient | None = None,
        java_source: JavaSource | None = None,
        java_runner: Runner | None = None,
        verify_java: bool = True,
        build_runner: BuildRunner | None = None,
    ):
        self.settings = settings
        self.background = background
        self.started_at = time.time()
        for directory in (settings.data_dir, settings.servers_dir, settings.binaries_dir, settings.logs_dir, settings.backup_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.db = Database(settings.db_path)
        self.db.migrate()
        self.repo = Repo(self.db)
        if self.repo.get_kv("initialised") is None:
            seed = AppSettings(update_check_minutes=settings.update_check_minutes, public_address=settings.public_address)
            self.repo.save_app_settings(seed)
            self.repo.set_kv("initialised", "1")
        self._app_settings = self.repo.get_app_settings()

        self.events = EventBus()
        self.pm = ProcessManager(settings.logs_dir, self.events)
        self.notifier = Notifier(self.app_settings)
        self.reporter = Reporter(self.repo, self.events, self.notifier)
        self.auth = AuthService(self.repo, settings.session_hours)
        self.access = AccessService(self.repo)
        self.accounts = AccountService(self.repo, self.access, self.reporter, self.events)
        self.release_source: ReleaseSource = release_source or GitHubReleaseSource(
            settings.github_repo, settings.github_api_url, token_getter=self._github_token
        )
        self.builds = BuildStore(settings, self.repo, lambda: self.release_source, verify_elf=verify_elf)
        self.jobs = JobService(self.repo, self.events)
        self.servers = ServerService(
            settings, self.repo, self.events, self.pm, self.builds, self.jobs, self.reporter,
            lambda: self.release_source, self.app_settings, command_builder,
        )
        self.backups = BackupService(
            settings, self.repo, self.pm, self.reporter, self.jobs,
            start_server=lambda rec, ctx: self.servers._do_start(rec, ctx),
            stop_server=lambda rec, ctx: self.servers._do_stop(rec, ctx),
        )
        self.servers.backups = self.backups
        self.servers.access = self.access
        self._setup_java(http_client, java_source, java_runner, verify_java, build_runner)
        extra = {} if stability_seconds is None else {"stability_seconds": stability_seconds}
        self.updater = UpdateService(
            self.repo, self.builds, self.pm, self.servers, self.backups, self.jobs, self.reporter,
            lambda: self.release_source, self.app_settings, **extra,
        )
        self.pack_updates = PackUpdater(self.servers.jvm, **extra)
        self.software_updates = SoftwareUpdater(self.servers.jvm, **extra)
        self.scheduler = Scheduler(self)
        self.jobs.on_finish = self._on_job_finished
        self.generated_admin_password: str | None = None

    def _setup_java(
        self, http_client: httpx.AsyncClient | None, java_source: JavaSource | None, java_runner: Runner | None, verify_java: bool,
        build_runner: BuildRunner | None = None,
    ) -> None:
        """The Java engine: one HTTP client for Mojang, Modrinth, Adoptium, Azul, SpigotMC and the loaders' download sites."""
        self.java_http = http_client or new_client()
        self._own_java_http = http_client is None
        self._java_source: JavaSource | None = java_source
        self._jdk_source: JavaSource | None = None

        def source() -> JavaSource:
            if self._java_source is None:  # looked at only when Java is first needed, so an unusual CPU only matters then
                self._java_source = JavaDownloads(self.java_http)
            return self._java_source

        def jdk_source() -> JavaSource:
            if self._jdk_source is None:
                self._jdk_source = JavaDownloads(self.java_http, image_type="jdk")
            return self._jdk_source

        def java_in_use(major: int) -> bool:
            running = set(self.pm.running_ids())
            return any(r.is_java and r.spec.get("java") == major and r.id in running for r in self.repo.list_servers())

        self.mojang = MojangMeta(self.java_http, self.repo)
        self.runtimes = JavaRuntimes(
            self.settings.java_dir, source, verifier=run_java_version if verify_java else None, in_use=java_in_use
        )
        self.modrinth = ModrinthClient(self.java_http)
        self.curseforge = CurseForgeClient(self.java_http, self._curseforge_key)
        self.loader_installer = LoaderInstaller(self.java_http, java_runner or run_process)
        self.mod_cache = ModCache(self.settings.cache_dir / "mods")
        self.packs = PackInstaller(self.java_http, self.mod_cache, headers_for=self.curseforge.download_headers)
        # Development kits: Spigot is compiled, which a runtime that only runs servers cannot do. Only the builder uses them.
        self.jdks = JavaRuntimes(self.settings.jdk_dir, jdk_source, verifier=run_java_version if verify_java else None, tools=("javac",))
        self.spigot_builder = SpigotBuilder(self.java_http, self.jdks, self.settings.cache_dir / "spigot-build", build_runner or run_build)
        self.software = SoftwareCatalog(self.java_http, self.mojang)
        self.software_installer = SoftwareInstaller(self.java_http, self.loader_installer, self.modrinth, builder=self.spigot_builder)
        self.gtnh = GtnhCatalog(self.java_http)
        self.servers.jvm = JavaServerService(
            self.servers, self.runtimes, self.mojang, self.modrinth, self.loader_installer, self.packs, self.settings.cache_dir / "uploads",
            self.curseforge, self.software, self.software_installer, self.gtnh,
        )

    # ----------------------------------------------------------------- settings
    def app_settings(self) -> AppSettings:
        return self._app_settings

    def save_app_settings(self, new: AppSettings) -> None:
        self.repo.save_app_settings(new)
        self._app_settings = new
        self.scheduler.reset_update_timer()

    def _github_token(self) -> str | None:
        return self._app_settings.github_token or self.settings.github_token

    def _curseforge_key(self) -> str | None:
        return self._app_settings.curseforge_api_key or self.settings.curseforge_api_key

    def _on_job_finished(self, job: dict[str, Any]) -> None:
        if job.get("server_id"):
            self.servers.publish_state(job["server_id"])

    # ---------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        interrupted_jobs = self.repo.fail_interrupted_jobs()
        for row in self.db.all("SELECT * FROM updates WHERE status='running'"):
            self.repo.add_activity(
                "warning", "update",
                f"An update of server {row['server_id']} to {row['to_tag']} was interrupted by a restart. "
                "Check the server and its pre-update backup.", row["server_id"],
            )
        self.repo.interrupt_running_updates()
        if interrupted_jobs:
            log.warning("%d job(s) were interrupted by the previous shutdown", interrupted_jobs)

        self.generated_admin_password = await self.auth.ensure_admin(self.settings.admin_user, self.settings.admin_password)
        for name in await self.auth.flag_example_passwords():
            warning = (
                f"The account '{name}' still used the example password that earlier versions of docker-compose.yml shipped, which "
                "everyone can read. It has been signed out and has to choose its own password at the next sign-in."
            )
            log.warning(warning)
            self.repo.add_activity("warning", "login", warning)
        if self.generated_admin_password:
            bar = "=" * 64
            log.warning(
                "\n%s\n  First start: created the administrator account\n    username: %s\n    password: %s\n"
                "  Change it after logging in (or set ADMIN_PASSWORD before the first start).\n%s",
                bar, self.settings.admin_user, self.generated_admin_password, bar,
            )
        self.servers.reconcile()
        self.servers.jvm.purge_old_uploads()
        await asyncio.to_thread(self.spigot_builder.purge)  # what a build that the last shutdown interrupted left behind
        self.updater.restore_state()
        result = await asyncio.to_thread(self.backups.reconcile)
        if result["added"] or result["removed"]:
            log.info("Backup index reconciled: %d added, %d removed", result["added"], result["removed"])
        if self.background:
            self.scheduler.start()
            count = self.servers.start_autostart()
            if count:
                log.info("Starting %d server(s) automatically", count)

    async def shutdown(self) -> None:
        log.info("Shutting down...")
        await self.scheduler.stop()
        # Jobs first: if an update is mid-flight, stopping the server underneath it must not be mistaken for a failed
        # update (which would trigger a rollback while the container is shutting down).
        cancelled = await self.jobs.cancel_all()
        if cancelled:
            log.warning("Cancelled %d running operation(s) because the manager is shutting down", cancelled)
        await self.servers.shutdown()
        await self.reporter.drain()
        if self._own_java_http:
            await self.java_http.aclose()
        for closable in (self.release_source, self.notifier):
            close = getattr(closable, "aclose", None)
            if close:
                await close()
        self.db.close()

    # -------------------------------------------------------------------- info
    def system_info(self) -> dict[str, Any]:
        s = self.settings
        data_usage = shutil.disk_usage(s.data_dir)
        backup_usage = shutil.disk_usage(s.backup_dir)
        used = self.repo.used_ports()
        return {
            "version": __version__,
            "python": platform.python_version(),
            "arch": platform.machine(),
            "timezone": s.timezone,
            "uptime": time.time() - self.started_at,
            "data_dir": str(s.data_dir),
            "backup_dir": str(s.backup_dir),
            "disk": {
                "data_free": data_usage.free, "data_total": data_usage.total,
                "backup_free": backup_usage.free, "backup_total": backup_usage.total,
                "same_volume": s.data_dir.resolve().drive == s.backup_dir.resolve().drive and data_usage.total == backup_usage.total,
                "binaries": self.builds.disk_usage(),
                "backups": self.backups.total_size(),
            },
            "ports": {
                "java_range": list(s.port_range), "bedrock_range": list(s.bedrock_port_range),
                "java_used": sorted(used["java"]), "bedrock_used": sorted(used["bedrock"]),
            },
            "memory": memory_info(),
            "java_installed": [j.major for j in self.runtimes.installed()],
            "update_check": {
                "enabled": self.app_settings().update_check_enabled,
                "last": self.updater.last_check,
                "errors": self.updater.last_errors,
            },
            "github_token": bool(self._github_token()),
            "curseforge_key": bool(self._curseforge_key()),
        }


# The live updates run over a WebSocket to the host the page came from. Browsers differ in whether 'self' covers ws:// and wss://, so
# the host is named as well, but never "ws: wss:" for every host: a page must not be able to send data to a socket somewhere else.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src {connect}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?$")  # a name or IPv4 address, with a port maybe

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def content_security_policy(host: str | None) -> str:
    """The Content-Security-Policy for a page requested from ``host`` (the Host header: only text that looks like a host is used)."""
    connect = "'self'"
    if host and _HOST_RE.match(host):
        connect += f" ws://{host} wss://{host}"
    return _CSP.format(connect=connect)


def forwarded_headers_ignored(peer: str | None, forwarded_for: str | None) -> bool:
    """Whether a request names the addresses it was passed through (``X-Forwarded-For``) while the address the manager acts on is
    still the sender's own, which is what happens when the sender is not listed in FORWARDED_ALLOW_IPS."""
    if not peer or not forwarded_for:
        return False
    return peer not in [part.strip() for part in forwarded_for.split(",")]


def create_app(
    settings: Settings | None = None,
    *,
    release_source: ReleaseSource | None = None,
    command_builder: CommandBuilder | None = None,
    verify_elf: bool = True,
    background: bool = True,
    stability_seconds: float | None = None,
    http_client: httpx.AsyncClient | None = None,
    java_source: JavaSource | None = None,
    java_runner: Runner | None = None,
    verify_java: bool = True,
    build_runner: BuildRunner | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    ctx = AppContext(
        settings, release_source=release_source, command_builder=command_builder, verify_elf=verify_elf,
        background=background, stability_seconds=stability_seconds, http_client=http_client, java_source=java_source,
        java_runner=java_runner, verify_java=verify_java, build_runner=build_runner,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await ctx.startup()
        try:
            yield
        finally:
            await ctx.shutdown()

    app = FastAPI(
        title="Pumpkin Server Manager", version=__version__, lifespan=lifespan,
        docs_url="/api/docs" if settings.api_docs else None,
        redoc_url=None, openapi_url="/api/openapi.json" if settings.api_docs else None,
    )
    app.state.ctx = ctx
    app.state.proxy_hint_given = False

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        peer = request.client.host if request.client else None
        if not app.state.proxy_hint_given and forwarded_headers_ignored(peer, request.headers.get("x-forwarded-for")):
            app.state.proxy_hint_given = True  # said once: it would be the same line on every request
            log.warning(
                "Requests from %s carry an X-Forwarded-For header, but %s is not in FORWARDED_ALLOW_IPS, so the header is ignored and "
                "every visitor looks like %s (the limit on failed sign-ins then counts them all together). If %s is your reverse proxy, "
                "set FORWARDED_ALLOW_IPS=%s. If you have no proxy, somebody is sending forged headers; they have no effect.",
                peer, peer, peer, peer, peer,
            )
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        response.headers.setdefault("Content-Security-Policy", content_security_policy(request.headers.get("host")))
        if settings.hsts_max_age and request.url.scheme == "https":  # browsers ignore it over http; behind a trusted proxy the scheme is its
            response.headers.setdefault("Strict-Transport-Security", f"max-age={settings.hsts_max_age}")
        path = request.url.path
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif path.startswith("/static/"):
            # Revalidate on every load (cheap 304s thanks to the ETag) so an upgraded container never serves
            # old scripts to a browser that cached them against the new API.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(ConfigError)
    async def config_error(_: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "errors": exc.errors}, status_code=422)

    @app.exception_handler(UserError)
    async def user_error(_: Request, exc: UserError) -> JSONResponse:
        body = {"detail": str(exc)}
        if getattr(exc, "code", None):
            body["code"] = exc.code  # type: ignore[attr-defined]
        return JSONResponse(body, status_code=exc.status)

    from .api import auth, backups, files, modpacks, servers, software, system, users, versions

    for module in (auth, system, users, servers, files, backups, versions, modpacks, software):
        app.include_router(module.router, prefix="/api")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon() -> FileResponse:
            return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app
