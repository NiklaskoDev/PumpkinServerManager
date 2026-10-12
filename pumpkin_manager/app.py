"""Application factory: wires the services together and exposes them over HTTP."""

from __future__ import annotations

import asyncio
import logging
import platform
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
from .jvm.loaders import LoaderInstaller, Runner, run_process
from .jvm.modrinth import ModrinthClient
from .jvm.mojang import MojangMeta
from .jvm.mrpack import ModCache, PackInstaller
from .jvm.net import new_client
from .jvm.packupdates import PackUpdater
from .jvm.runtime import JavaDownloads, JavaRuntimes, JavaSource, run_java_version
from .jvm.service import JavaServerService
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
        self._setup_java(http_client, java_source, java_runner, verify_java)
        extra = {} if stability_seconds is None else {"stability_seconds": stability_seconds}
        self.updater = UpdateService(
            self.repo, self.builds, self.pm, self.servers, self.backups, self.jobs, self.reporter,
            lambda: self.release_source, self.app_settings, **extra,
        )
        self.pack_updates = PackUpdater(self.servers.jvm, **extra)
        self.scheduler = Scheduler(self)
        self.jobs.on_finish = self._on_job_finished
        self.generated_admin_password: str | None = None

    def _setup_java(
        self, http_client: httpx.AsyncClient | None, java_source: JavaSource | None, java_runner: Runner | None, verify_java: bool
    ) -> None:
        """The Java engine: one HTTP client for Mojang, Modrinth, Adoptium, Azul and the loaders' download sites."""
        self.java_http = http_client or new_client()
        self._own_java_http = http_client is None
        self._java_source: JavaSource | None = java_source

        def source() -> JavaSource:
            if self._java_source is None:  # looked at only when Java is first needed, so an unusual CPU only matters then
                self._java_source = JavaDownloads(self.java_http)
            return self._java_source

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
        self.servers.jvm = JavaServerService(
            self.servers, self.runtimes, self.mojang, self.modrinth, self.loader_installer, self.packs, self.settings.cache_dir / "uploads",
            self.curseforge,
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
        if self.generated_admin_password:
            bar = "=" * 64
            log.warning(
                "\n%s\n  First start: created the administrator account\n    username: %s\n    password: %s\n"
                "  Change it after logging in (or set ADMIN_PASSWORD before the first start).\n%s",
                bar, self.settings.admin_user, self.generated_admin_password, bar,
            )
        self.servers.reconcile()
        self.servers.jvm.purge_old_uploads()
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


_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


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
) -> FastAPI:
    settings = settings or Settings.from_env()
    ctx = AppContext(
        settings, release_source=release_source, command_builder=command_builder, verify_elf=verify_elf,
        background=background, stability_seconds=stability_seconds, http_client=http_client, java_source=java_source,
        java_runner=java_runner, verify_java=verify_java,
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
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

    from .api import auth, backups, files, modpacks, servers, system, users, versions

    for module in (auth, system, users, servers, files, backups, versions, modpacks):
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
