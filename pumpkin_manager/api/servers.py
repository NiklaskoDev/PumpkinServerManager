"""Server CRUD, lifecycle, console, configuration and player management."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import pumpkin_config as pc
from ..models import CHANNELS
from .deps import get_ctx, load_server, require_session

router = APIRouter(tags=["servers"], dependencies=[Depends(require_session)])

Channel = Literal["stable", "nightly", "canary"]


def accepted(job: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"job": job}, status_code=202)


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    channel: Channel | None = None
    pinned_tag: str | None = Field(None, max_length=100)
    java_port: int | None = Field(None, ge=1024, le=65535)
    bedrock: bool = False
    bedrock_port: int | None = Field(None, ge=1024, le=65535)
    settings: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    start: bool = True


class PatchBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    channel: Channel | None = None
    pinned_tag: str | None = Field(None, max_length=100)
    java_port: int | None = Field(None, ge=1024, le=65535)
    bedrock: bool | None = None
    bedrock_port: int | None = Field(None, ge=1024, le=65535)
    policy: dict[str, Any] | None = None


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=1000)


class ConfigBody(BaseModel):
    values: dict[str, Any]


class RawConfigBody(BaseModel):
    content: str = Field(max_length=600_000)


class PlayerBody(BaseModel):
    action: Literal["whitelist_add", "whitelist_remove", "op", "deop", "ban", "pardon", "kick", "ban_ip", "pardon_ip"]
    target: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(None, max_length=200)


@router.get("/servers")
async def list_servers(request: Request) -> list:
    return get_ctx(request).servers.list_views()


@router.post("/servers", status_code=202)
async def create_server(body: CreateBody, request: Request) -> JSONResponse:
    ctx = get_ctx(request)
    if body.channel is not None and body.channel not in CHANNELS:
        raise pc.ConfigError({"channel": "unknown channel"})
    return accepted(ctx.servers.submit_create(body.model_dump()))


@router.get("/servers/{server_id}")
async def get_server(request: Request, server_id: str) -> dict:
    ctx = get_ctx(request)
    rec = load_server(request, server_id)
    await ctx.servers.disk_usage(rec.id)
    return ctx.servers.view(rec, detail=True)


@router.patch("/servers/{server_id}")
async def patch_server(request: Request, server_id: str, body: PatchBody) -> dict:
    ctx = get_ctx(request)
    rec = load_server(request, server_id)
    rec, restart_required = ctx.servers.patch(rec, body.model_dump(exclude_unset=True))
    view = ctx.servers.view(rec, detail=True)
    view["restart_required"] = restart_required
    return view


@router.delete("/servers/{server_id}", status_code=202)
async def delete_server(
    request: Request, server_id: str, final_backup: bool = True, delete_backups: bool = False
) -> JSONResponse:
    ctx = get_ctx(request)
    return accepted(ctx.servers.submit_delete(load_server(request, server_id), final_backup, delete_backups))


@router.post("/servers/{server_id}/start", status_code=202)
async def start(request: Request, server_id: str) -> JSONResponse:
    ctx = get_ctx(request)
    return accepted(ctx.servers.submit_start(load_server(request, server_id)))


@router.post("/servers/{server_id}/stop", status_code=202)
async def stop(request: Request, server_id: str) -> JSONResponse:
    ctx = get_ctx(request)
    return accepted(ctx.servers.submit_stop(load_server(request, server_id)))


@router.post("/servers/{server_id}/restart", status_code=202)
async def restart(request: Request, server_id: str) -> JSONResponse:
    ctx = get_ctx(request)
    return accepted(ctx.servers.submit_restart(load_server(request, server_id)))


@router.post("/servers/{server_id}/kill")
async def kill(request: Request, server_id: str) -> dict:
    ctx = get_ctx(request)
    await ctx.servers.kill(load_server(request, server_id))
    return {"ok": True}


# --------------------------------------------------------------------- console
@router.post("/servers/{server_id}/command")
async def command(request: Request, server_id: str, body: CommandBody) -> dict:
    ctx = get_ctx(request)
    await ctx.servers.command(load_server(request, server_id), body.command)
    return {"ok": True}


@router.get("/servers/{server_id}/console")
async def console(request: Request, server_id: str, lines: int = Query(500, ge=1, le=3000)) -> dict:
    ctx = get_ctx(request)
    load_server(request, server_id)
    return {"lines": ctx.pm.backlog(server_id, lines)}


# ---------------------------------------------------------------------- config
@router.get("/servers/{server_id}/config")
async def read_config(request: Request, server_id: str) -> dict:
    return get_ctx(request).servers.read_config(load_server(request, server_id))


@router.put("/servers/{server_id}/config")
async def write_config(request: Request, server_id: str, body: ConfigBody) -> dict:
    return await get_ctx(request).servers.save_settings(load_server(request, server_id), body.values)


@router.get("/servers/{server_id}/config/raw")
async def read_raw_config(request: Request, server_id: str) -> dict:
    return get_ctx(request).servers.read_raw_config(load_server(request, server_id))


@router.put("/servers/{server_id}/config/raw")
async def write_raw_config(request: Request, server_id: str, body: RawConfigBody) -> dict:
    return get_ctx(request).servers.write_raw_config(load_server(request, server_id), body.content)


# --------------------------------------------------------------------- players
@router.get("/servers/{server_id}/players")
async def players(request: Request, server_id: str) -> dict:
    ctx = get_ctx(request)
    rec = load_server(request, server_id)
    rt = ctx.pm.runtime(rec.id)
    return {
        "online": {"count": rt.players_online, "max": rt.players_max, "names": rt.player_names},
        "lists": ctx.servers.read_player_lists(rec),
        "running": rt.state == "running",
    }


@router.post("/servers/{server_id}/players")
async def player_action(request: Request, server_id: str, body: PlayerBody) -> dict:
    ctx = get_ctx(request)
    await ctx.servers.player_action(load_server(request, server_id), body.action, body.target, body.reason)
    return {"ok": True}
