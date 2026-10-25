"""Command line entry point: ``python -m pumpkin_manager [serve|reset-password|java-install|version]``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import secrets
import sys

from . import __version__
from .config import Settings, trusts_every_proxy


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def serve() -> int:
    import uvicorn

    from .app import create_app

    settings = Settings.from_env()
    _configure_logging(settings.log_level)
    log = logging.getLogger("pumpkin_manager")
    log.info("Pumpkin Server Manager %s - data in %s, backups in %s", __version__, settings.data_dir, settings.backup_dir)
    if trusts_every_proxy(settings.forwarded_allow_ips):
        log.warning(
            "FORWARDED_ALLOW_IPS=* : every sender is believed when it says which address it comes from. Unless the dashboard port can "
            "only be reached through your reverse proxy, anyone can fake their address and get around the limit on failed sign-ins. "
            "Set FORWARDED_ALLOW_IPS to the proxy's own address instead."
        )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="warning",
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=8,
    )
    return 0


def reset_password(user: str | None, password: str | None) -> int:
    from .db import Database
    from .repo import Repo
    from .security import MIN_PASSWORD_LENGTH, AuthService

    settings = Settings.from_env()
    if not settings.db_path.exists():
        print(f"No database found at {settings.db_path}. Start the manager once first.", file=sys.stderr)
        return 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # `docker exec` runs as root by default. Become the account that owns the database first, otherwise the
        # WAL files SQLite creates would be root-owned and the manager (running as PUID) could no longer write them.
        owner = settings.db_path.stat()
        if owner.st_uid != 0:
            os.setgid(owner.st_gid)
            os.setuid(owner.st_uid)
    db = Database(settings.db_path)
    db.migrate()
    repo = Repo(db)
    username = user or settings.admin_user
    generated = False
    if password is None and sys.stdin.isatty():
        first = getpass.getpass("New password (leave empty to generate one): ")
        password = first or None
    if not password:
        password, generated = secrets.token_urlsafe(12), True
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"The password must be at least {MIN_PASSWORD_LENGTH} characters long.", file=sys.stderr)
        return 1
    target = repo.get_user_by_name(username)
    temporary = bool(target and target["kind"] == "friend")  # a friend's account gets a temporary password to replace at login
    ok = asyncio.run(AuthService(repo, settings.session_hours).reset_password(username, password, temporary=temporary))
    db.close()
    if not ok:
        print(f"There is no user named '{username}'.", file=sys.stderr)
        return 1
    print(f"Password for '{username}' has been reset. All sessions were signed out.")
    if temporary:
        print("It is a temporary password: the account has to choose its own at the next sign-in.")
    if generated:
        print(f"New password: {password}")
    return 0


def java_install(major: int) -> int:
    """Download a Java runtime the way modpack servers do and start it once: proves that Java runs in this container."""
    from .jvm.net import DownloadError, new_client
    from .jvm.runtime import JavaDownloads, JavaRuntimes, run_java_version

    settings = Settings.from_env()
    _configure_logging(settings.log_level)

    async def run() -> int:
        client = new_client()
        try:
            source = JavaDownloads(client)
            runtimes = JavaRuntimes(settings.java_dir, lambda: source)
            shown = {"tenth": -1}

            def progress(received: int, total: int) -> None:
                tenth = int(10 * received / total) if total else 0
                if tenth != shown["tenth"]:
                    shown["tenth"] = tenth
                    print(f"  downloading Java {major}: {received // 1048576} of {total // 1048576} MiB", flush=True)

            installed = await runtimes.ensure(major, progress)
            print(f"Java {major} ({installed.release}) is installed in {installed.path}")
            print(await run_java_version(installed.java))
            return 0
        except DownloadError as exc:
            print(f"Could not install Java {major}: {exc}", file=sys.stderr)
            return 1
        finally:
            await client.aclose()

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pumpkin_manager", description="Pumpkin Server Manager")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web dashboard and server supervisor (default)")
    reset = sub.add_parser("reset-password", help="set a new password for the administrator account")
    reset.add_argument("--user", help="account name (default: ADMIN_USER or 'admin')")
    reset.add_argument("--password", help="new password (prompted for when omitted)")
    sub.add_parser("version", help="print the version")
    java = sub.add_parser("java-install", help="download a Java runtime for modpack servers and check that it starts")
    java.add_argument("major", type=int, help="Java version, for example 21")
    args = parser.parse_args(argv)

    if args.command == "reset-password":
        return reset_password(args.user, args.password)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "java-install":
        return java_install(args.major)
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
