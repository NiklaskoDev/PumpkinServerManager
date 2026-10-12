"""Helpers for the CurseForge tests: a small CurseForge world, a key for the app, and creating servers from it."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from conftest import finish
from jvmfakes import FakeMod
from jvmworld import CREATE, LITHIUM

MINIMAP = FakeMod("mods/minimap.jar", b"minimap" * 40)  # a mod that CurseForge tags as meant for clients
JADE = FakeMod("mods/jade.jar", b"jade" * 80)
SECRET = FakeMod("mods/secret.jar", b"secret" * 60)  # a mod whose author does not allow automatic downloads
LITHIUM_V2 = FakeMod("mods/lithium.jar", b"lithium-second-edition" * 100)

PACK = 1000  # the project number of "Alpha Pack"
OLD, NEW, NEWEST = "2026-09-01T12:00:00.000Z", "2026-09-10T12:00:00.000Z", "2026-09-20T12:00:00.000Z"

# What a pack author's server pack typically holds: the content, plus scripts and an installer the manager has no use for.
SERVER_FILES: dict[str, bytes] = {
    "mods/lithium.jar": LITHIUM.content, "mods/create.jar": CREATE.content, "config/create.toml": b"speed = 5\n",
    "config/other.toml": b"other = true\n", "kubejs/server_scripts/main.js": b"console.log('hi')\n", "README.txt": b"read me\n",
    "server.properties": b"allow-flight=true\nview-distance=9\n",
    # not taken over:
    "start.sh": b"#!/bin/sh\njava -jar forge.jar\n", "start.bat": b"@echo off\n", "forge-installer.jar": b"installer", "user_jvm_args.txt": b"-Xmx9G\n",
    "eula.txt": b"eula=false\n", "libraries/foo/bar.jar": b"library", "logs/latest.log": b"an old log\n",
    "ops.json": b'[{"uuid": "00000000-0000-0000-0000-000000000001", "name": "Cheater", "level": 4}]',
}


def give_key(ctx: Any, key: str) -> None:
    """Set the CurseForge API key the way the dashboard's settings do."""
    ctx.save_app_settings(ctx.app_settings().model_copy(update={"curseforge_api_key": key}))


def standard_pack(world: Any, **options: Any) -> dict[str, Any]:
    """Alpha Pack 1.0.0: lithium, create and a minimap (a client-only mod), with one configuration file. Returns the pack's file."""
    cf = world.cf
    cf.add_mod(2001, 3001, LITHIUM)
    cf.add_mod(2002, 3002, CREATE)
    cf.add_mod(2003, 3003, MINIMAP, client_only=True)
    if PACK not in cf.mods:
        cf.add_project(PACK, "Alpha Pack", class_id=4471)
    options.setdefault("mods", ((2001, 3001), (2002, 3002), (2003, 3003)))
    options.setdefault("overrides", {"config/create.toml": b"speed = 1\n"})
    options.setdefault("date", OLD)
    return cf.add_pack_version(PACK, 5000, "1.0.0", **options)


def add_update(world: Any, number: str = "1.1.0", file_id: int = 5100, **options: Any) -> dict[str, Any]:
    """A newer version of Alpha Pack: lithium updated, create dropped, jade added, a changed and a new configuration file."""
    cf = world.cf
    cf.add_mod(2001, 3011, LITHIUM_V2)
    cf.add_mod(2005, 3005, JADE, name="Jade")
    options.setdefault("mods", ((2001, 3011), (2005, 3005)))
    options.setdefault("overrides", {"config/create.toml": b"speed = 2\n", "config/new.toml": b"fresh = true\n"})
    options.setdefault("date", NEW)
    return cf.add_pack_version(PACK, file_id, number, **options)


async def create_cf(ctx: Any, name: str = "Alpha", *, modpack: dict[str, Any] | None = None, **spec: Any) -> tuple[Any, dict[str, Any]]:
    """Create a server from the fake CurseForge's Alpha Pack and wait for the job. Returns (server record, job row)."""
    body = {
        "name": name, "engine": "java", "eula": True, "modpack": {"source": "curseforge", "project": str(PACK), **(modpack or {})},
        "start": spec.pop("start", False), **spec,
    }
    row = await finish(ctx, ctx.servers.submit_create(body))
    assert row["status"] == "success", row["message"]
    return next(s for s in ctx.repo.list_servers() if s.name == name), row


async def try_create_cf(ctx: Any, name: str = "Alpha", *, modpack: dict[str, Any] | None = None, **spec: Any) -> dict[str, Any]:
    """Like create_cf, for creations that are expected to fail: returns the job row whatever happened."""
    body = {
        "name": name, "engine": "java", "eula": True, "modpack": {"source": "curseforge", "project": str(PACK), **(modpack or {})},
        "start": spec.pop("start", False), **spec,
    }
    return await finish(ctx, ctx.servers.submit_create(body))


def steps_of(ctx: Any, row: dict[str, Any]) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


def zip_at(path: Path, entries: dict[str, bytes], *, symlinks: dict[str, str] | None = None) -> Path:
    """Write a zip file; ``symlinks`` adds entries that are links (name -> target), as a hostile zip might."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777) << 16  # S_IFLNK | 0777
            zf.writestr(info, target)
    return path


def files_under(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file()] if directory.exists() else []
