"""Modpack servers: finding modpacks, uploading one, and the Java runtimes they run on. Only the administrator creates servers."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..access import VERSIONS, Principal
from ..jobs import JobContext
from ..jvm.mojang import AVAILABLE_JAVA
from ..models import ServerRecord
from ..servers import ServerError
from .deps import get_ctx, require_owner, require_principal, require_session, server_perm

router = APIRouter(tags=["modpacks"], dependencies=[Depends(require_session)])


class ModpackUpdateBody(BaseModel):
    version_id: str | None = Field(None, max_length=64)  # a specific version of the modpack; without it the pending update


def _java(rec: ServerRecord) -> ServerRecord:
    if not rec.is_java or rec.spec.get("software"):
        raise ServerError("This server does not run a modpack.", 404)
    return rec


@router.get("/modpacks/sources")
async def sources(request: Request, owner: Principal = Depends(require_owner)) -> dict:
    """Where modpacks can come from, and whether each source is ready (CurseForge needs an API key)."""
    return get_ctx(request).servers.jvm.sources()


@router.get("/modpacks/search")
async def search(
    request: Request, q: str = Query("", max_length=100), offset: int = Query(0, ge=0, le=10_000),
    source: str = Query("modrinth", pattern="^(modrinth|curseforge)$"), owner: Principal = Depends(require_owner),
) -> dict:
    """Modpacks that can run on a server, most downloaded first when no search text is given. For CurseForge a key that may not
    search gets the packs CurseForge lists itself instead, with ``search: false`` and a note."""
    return await get_ctx(request).servers.jvm.search_packs(q, offset, source)


@router.get("/modpacks/project")
async def project(
    request: Request, ref: str = Query(min_length=2, max_length=300), source: str = Query("modrinth", pattern="^(modrinth|curseforge|gtnh)$"),
    owner: Principal = Depends(require_owner),
) -> dict:
    """One modpack and its versions. ``ref`` is a Modrinth link, id or short name, or a CurseForge project number or link; for
    ``source=gtnh`` it is ignored (GT New Horizons is one pack, and its versions come from its own website)."""
    return await get_ctx(request).servers.jvm.pack_details(ref, source)


@router.get("/modpacks/icon")
async def pack_icon(request: Request, url: str = Query(min_length=10, max_length=500), principal: Principal = Depends(require_principal)) -> Response:
    """A modpack's icon, fetched from Modrinth's or CurseForge's image host for the dashboard (whose content policy allows no other site)."""
    data, kind = await get_ctx(request).servers.jvm.pack_icon(url)
    return Response(content=data, media_type=kind, headers={"Cache-Control": "private, max-age=86400"})


@router.put("/modpacks/upload")
async def upload(request: Request, owner: Principal = Depends(require_owner)) -> dict:
    """Upload a .mrpack (Modrinth) or CurseForge modpack zip (the request body). Returns an upload id for creating a server from it."""
    ctx = get_ctx(request)
    declared = int(request.headers.get("content-length") or 0)
    if declared > 1024 * 1024 * 1024:
        raise ServerError("That file is too large for a modpack (limit 1 GiB).")
    return await ctx.servers.jvm.accept_upload(request.stream())


@router.get("/servers/{server_id}/modpack")
async def server_modpack(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict:
    """What a modpack server has installed: the pack, its mods and how Java is set up for it."""
    jvm = get_ctx(request).servers.jvm
    _java(rec)
    modpack, java = jvm.describe(rec)
    lock = jvm.lock_of(rec) or {}
    mods = sorted(lock.get("files", {}))
    return {"modpack": modpack, "java": java, "mods": mods[:3000], "mod_count": len(mods), "config_files": len(lock.get("overrides", {}))}


@router.get("/servers/{server_id}/modpack/versions")
async def modpack_versions(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict:
    """Every version of the server's modpack that can be installed, newest first."""
    return await get_ctx(request).pack_updates.available(_java(rec))


@router.post("/servers/{server_id}/modpack/check")
async def check_modpack(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict:
    """Ask Modrinth or CurseForge right now whether the modpack has a newer version."""
    ctx = get_ctx(request)
    result = await asyncio.wait_for(ctx.pack_updates.check(_java(rec)), timeout=60)
    if result["errors"]:
        raise ServerError(next(iter(result["errors"].values())), 502)
    return {**result, "update": ctx.servers.view(ctx.servers.get(rec.id))["update"]}


@router.post("/servers/{server_id}/modpack/update", status_code=202)
async def update_modpack(request: Request, body: ModpackUpdateBody, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> JSONResponse:
    """Update (or switch) the modpack: a verified backup is made first, and the server is restored if the new version does not start."""
    job = get_ctx(request).pack_updates.submit_update(_java(rec), body.version_id)
    return JSONResponse({"job": job}, status_code=202)


@router.post("/servers/{server_id}/modpack/rollback", status_code=202)
async def rollback_modpack(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> JSONResponse:
    """Go back to how the server was before its last update (mods and world come from the backup made then)."""
    return JSONResponse({"job": get_ctx(request).pack_updates.submit_rollback(_java(rec))}, status_code=202)


# ------------------------------------------------------------------ Java runtimes
@router.get("/java/runtimes")
async def runtimes(request: Request, owner: Principal = Depends(require_owner)) -> dict:
    ctx = get_ctx(request)
    return {
        "installed": ctx.servers.jvm.runtime_overview(), "available": list(AVAILABLE_JAVA),
        "disk_bytes": await asyncio.to_thread(ctx.runtimes.disk_usage), "mods_cache_bytes": await asyncio.to_thread(ctx.mod_cache.disk_usage),
        # the development kits only Spigot's builds use (a compiler is what a JRE lacks)
        "jdks": [j.public() for j in ctx.jdks.installed()], "jdk_disk_bytes": await asyncio.to_thread(ctx.jdks.disk_usage),
    }


@router.post("/java/runtimes/{major}", status_code=202)
async def download_runtime(request: Request, major: int, owner: Principal = Depends(require_owner)) -> JSONResponse:
    ctx = get_ctx(request)
    if major not in AVAILABLE_JAVA:
        raise ServerError(f"Java {major} is not one of the versions this manager can install ({', '.join(map(str, AVAILABLE_JAVA))}).")

    async def run(job: JobContext) -> None:
        job.step(f"Downloading Java {major}")
        await ctx.runtimes.ensure(
            major, lambda done, total: job.progress(done / total if total else None, f"Downloading Java {major} ({done // 1048576} of {total // 1048576} MiB)")
        )
        job.step(f"Java {major} is installed")

    return JSONResponse({"job": ctx.jobs.submit("download", f"Download Java {major}", None, run)}, status_code=202)


@router.delete("/java/runtimes/{major}")
async def remove_runtime(request: Request, major: int, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    ctx = get_ctx(request)
    if ctx.runtimes.get(major) is None:
        raise ServerError("That Java version is not installed.", 404)
    for rec in ctx.repo.list_servers():
        if rec.is_java and rec.spec.get("java") == major:
            raise ServerError(f"'{rec.name}' runs on Java {major}, so it cannot be removed.", 409)
    ctx.updater.ensure_builds_not_in_flight()  # a server that is being created may be about to use it
    await asyncio.to_thread(ctx.runtimes.remove, major)
    return {"ok": True}


@router.delete("/java/jdks/{major}")
async def remove_jdk(request: Request, major: int, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    """Remove a development kit: it is downloaded again when the next Spigot build needs it."""
    ctx = get_ctx(request)
    if ctx.jdks.get(major) is None:
        raise ServerError("That development kit is not installed.", 404)
    if ctx.spigot_builder.busy:
        raise ServerError("A Spigot build is running with it right now. Try again when it has finished.", 409)
    ctx.updater.ensure_builds_not_in_flight()  # a server that is being created or updated may be about to build with it
    await asyncio.to_thread(ctx.jdks.remove, major)
    return {"ok": True}
