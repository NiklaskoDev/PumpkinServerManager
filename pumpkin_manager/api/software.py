"""Servers that run Vanilla, Paper or Fabric: what can be installed, and updating, switching and rolling back the ones that do."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..access import VERSIONS, Principal
from ..models import ServerRecord
from ..servers import ServerError
from .deps import any_server_perm, get_ctx, require_session, server_perm

router = APIRouter(tags=["software"], dependencies=[Depends(require_session)])


class SoftwareUpdateBody(BaseModel):
    # Without a Minecraft version and a build the update that was found is installed. A Minecraft version alone means its newest
    # stable build. Going to an older Minecraft version has to be confirmed (a world that a newer version opened may not open again).
    minecraft: str | None = Field(None, max_length=64)
    build: str | None = Field(None, max_length=64)
    confirm_downgrade: bool = False


def _managed(rec: ServerRecord) -> ServerRecord:
    if not (rec.is_java and rec.spec.get("software")):
        raise ServerError("This server does not run Vanilla, Paper or Fabric that the manager installed.", 404)
    return rec


# ------------------------------------------------------------------------------------------------ what can be installed
@router.get("/software/{kind}/versions")
async def versions(
    request: Request, kind: str, snapshots: bool = False, principal: Principal = Depends(any_server_perm(VERSIONS)),
) -> dict[str, Any]:
    """The Minecraft versions one kind of software runs, newest first, and the one to start with (the newest that has a stable build)."""
    catalog = get_ctx(request).software
    listed = await catalog.versions(kind, snapshots=snapshots)
    return {"kind": kind, "versions": listed, "recommended": await catalog.recommended_minecraft(kind)}


@router.get("/software/{kind}/builds")
async def builds(
    request: Request, kind: str, minecraft: str = Query(min_length=1, max_length=64), experimental: bool = False,
    principal: Principal = Depends(any_server_perm(VERSIONS)),
) -> dict[str, Any]:
    """The builds of one Minecraft version (Paper builds, Fabric loader versions), newest first, with what the maker says about it."""
    return (await get_ctx(request).software.options(kind, minecraft)).public(experimental)


# ------------------------------------------------------------------------------------------------------- one server
@router.get("/servers/{server_id}/software")
async def server_software(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict[str, Any]:
    """What a Vanilla, Paper or Fabric server runs: the software, its version, what was found newer, and how Java is set up."""
    jvm = get_ctx(request).servers.jvm
    _, java = jvm.describe(_managed(rec))
    return {"software": jvm.describe_software(rec), "java": java}


@router.get("/servers/{server_id}/software/versions")
async def server_versions(request: Request, snapshots: bool = False, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict[str, Any]:
    """The Minecraft versions this server's software can be switched to."""
    return await get_ctx(request).software_updates.versions(_managed(rec), snapshots)


@router.get("/servers/{server_id}/software/builds")
async def server_builds(
    request: Request, minecraft: str = Query(min_length=1, max_length=64), experimental: bool = False,
    rec: ServerRecord = Depends(server_perm(VERSIONS)),
) -> dict[str, Any]:
    return await get_ctx(request).software_updates.builds(_managed(rec), minecraft, experimental)


@router.post("/servers/{server_id}/software/check")
async def check_software(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict[str, Any]:
    """Ask Mojang, PaperMC or Fabric right now whether there is a newer build or Minecraft version."""
    ctx = get_ctx(request)
    result = await asyncio.wait_for(ctx.software_updates.check(_managed(rec)), timeout=60)
    if result["errors"]:
        raise ServerError(next(iter(result["errors"].values())), 502)
    fresh = ctx.servers.get(rec.id)
    return {**result, "update": ctx.servers.view(fresh)["update"], "software": ctx.servers.jvm.describe_software(fresh)}


@router.post("/servers/{server_id}/software/update", status_code=202)
async def update_software(
    request: Request, body: SoftwareUpdateBody, rec: ServerRecord = Depends(server_perm(VERSIONS))
) -> JSONResponse:
    """Install a newer build, or switch to another Minecraft version: a verified backup is made first, and the server is restored if
    the new version does not start."""
    updater = get_ctx(request).software_updates
    build = await updater.prepare(_managed(rec), body.minecraft, body.build, confirm_downgrade=body.confirm_downgrade)
    return JSONResponse({"job": updater.submit_update(rec, build)}, status_code=202)


@router.post("/servers/{server_id}/software/rollback", status_code=202)
async def rollback_software(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> JSONResponse:
    """Go back to how the server was before its last update (the software, the world and the plugins or mods come from the backup)."""
    return JSONResponse({"job": get_ctx(request).software_updates.submit_rollback(_managed(rec))}, status_code=202)
