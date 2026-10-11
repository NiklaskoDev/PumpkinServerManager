"""Pumpkin versions: what is available, what is cached, and updating/rolling back servers."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..access import VERSIONS, Principal
from ..jobs import JobContext
from ..models import BuildRecord, ServerRecord
from ..releases import ROLLING_TAGS, ReleaseError
from ..servers import ServerError
from .deps import any_server_perm, get_ctx, require_owner, require_principal, require_session, server_perm

router = APIRouter(tags=["versions"], dependencies=[Depends(require_session)])


class TargetBody(BaseModel):
    build_id: str | None = Field(None, max_length=200)
    tag: str | None = Field(None, max_length=100)
    channel: Literal["stable", "nightly", "canary"] | None = None
    pin: bool = False


class RollbackBody(BaseModel):
    restore_data: bool = False


class DownloadBody(BaseModel):
    tag: str | None = Field(None, max_length=100)
    channel: Literal["stable", "nightly", "canary"] | None = None


async def _resolve_target(request: Request, body: TargetBody, rec_channel: str) -> BuildRecord:
    """Turn the many ways the UI can name a version into a registered build."""
    ctx = get_ctx(request)
    if body.build_id:
        build = ctx.repo.get_build(body.build_id)
        if build is None:
            raise ServerError("That version is not known.", 404)
        return build
    try:
        if body.tag:
            remote = await ctx.release_source.by_tag(body.tag)
        else:
            remote = await ctx.release_source.latest(body.channel or rec_channel)
    except ReleaseError as exc:
        raise ServerError(str(exc), 502) from exc
    return ctx.builds.register(remote)


@router.get("/versions")
async def overview(request: Request, principal: Principal = Depends(any_server_perm(VERSIONS))) -> dict:
    data = get_ctx(request).updater.overview()
    if not principal.is_owner:
        # What a server admin needs to pick a version; the list of builds says which servers use them, so it stays private.
        data = {"channels": data["channels"], "last_check": data["last_check"], "errors": data["errors"], "builds": [], "disk_bytes": 0}
    return data


@router.post("/versions/check")
async def check_now(request: Request, owner: Principal = Depends(require_owner)) -> dict:
    ctx = get_ctx(request)
    result = await asyncio.wait_for(ctx.updater.check(), timeout=60)
    return {**result, **ctx.updater.overview()}


@router.get("/versions/releases")
async def stable_releases(request: Request, limit: int = 15, principal: Principal = Depends(any_server_perm(VERSIONS))) -> dict:
    ctx = get_ctx(request)
    try:
        builds = await ctx.release_source.list_stable(max(1, min(limit, 30)))
    except ReleaseError as exc:
        raise ServerError(str(exc), 502) from exc
    return {
        "releases": [
            {
                "tag": b.tag, "version": b.version, "mc_version": b.mc_version, "published_at": b.published_at,
                "size": b.size, "release_url": b.release_url, "notes": (b.body or "")[:1500],
            }
            for b in builds
        ],
        "rolling": list(ROLLING_TAGS),
    }


@router.post("/versions/download", status_code=202)
async def download(request: Request, body: DownloadBody, owner: Principal = Depends(require_owner)) -> JSONResponse:
    ctx = get_ctx(request)

    async def run(job: JobContext) -> None:
        job.step("Looking up the build")
        try:
            remote = await (ctx.release_source.by_tag(body.tag) if body.tag else ctx.release_source.latest(body.channel or "stable"))
        except ReleaseError as exc:
            raise ServerError(str(exc), 502) from exc
        build = ctx.builds.register(remote)
        job.step(f"Downloading {build.label}")
        await ctx.builds.ensure(build, ctx.servers._download_progress(job, span=1.0))
        job.step("Download complete and verified")

    return JSONResponse({"job": ctx.jobs.submit("download", "Download Pumpkin", None, run)}, status_code=202)


@router.delete("/versions/{build_id}")
async def delete_build(request: Request, build_id: str, owner: Principal = Depends(require_owner)) -> dict:
    ctx = get_ctx(request)
    build = ctx.repo.get_build(build_id)
    if build is None:
        raise ServerError("That version is not known.", 404)
    for rec in ctx.repo.list_servers():
        if build_id in (rec.build_id, rec.previous_build_id, rec.pending_build_id):
            raise ServerError(f"'{rec.name}' still uses this version, so it cannot be deleted.", 409)
    await asyncio.to_thread(ctx.builds.delete_files, build_id)
    return {"ok": True}


@router.post("/versions/prune")
async def prune(request: Request, owner: Principal = Depends(require_owner)) -> dict:
    removed = await asyncio.to_thread(get_ctx(request).updater.prune_builds)
    return {"removed": removed}


# ----------------------------------------------------------- per-server updates
@router.get("/servers/{server_id}/updates")
async def update_history(request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> dict:
    return {"updates": get_ctx(request).repo.list_updates(rec.id, 25)}


@router.post("/servers/{server_id}/update", status_code=202)
async def update_server(request: Request, body: TargetBody, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> JSONResponse:
    ctx = get_ctx(request)
    if body.build_id is None and body.tag is None and body.channel is None and rec.pending_build_id:
        target = ctx.repo.get_build(rec.pending_build_id)
        if target is None:
            raise ServerError("The pending update is no longer available; check for updates again.", 409)
    else:
        target = await _resolve_target(request, body, rec.channel)
    job = ctx.updater.submit_update(rec, target, trigger="manual", announce_seconds=30, pin=body.pin)
    return JSONResponse({"job": job}, status_code=202)


@router.post("/servers/{server_id}/update/check")
async def check_server(
    request: Request, rec: ServerRecord = Depends(server_perm(VERSIONS)), principal: Principal = Depends(require_principal)
) -> dict[str, Any]:
    ctx = get_ctx(request)
    result = await asyncio.wait_for(ctx.updater.check(), timeout=60)
    rec = ctx.servers.get(rec.id)
    return {**result, "server": ctx.access.shape_server_view(principal, ctx.servers.view(rec, detail=True))}


@router.post("/servers/{server_id}/rollback", status_code=202)
async def rollback(request: Request, body: RollbackBody, rec: ServerRecord = Depends(server_perm(VERSIONS))) -> JSONResponse:
    return JSONResponse({"job": get_ctx(request).updater.submit_rollback(rec, body.restore_data)}, status_code=202)
