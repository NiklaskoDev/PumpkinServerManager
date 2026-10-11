"""Server CRUD, lifecycle, console, configuration and player management."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import pumpkin_config as pc
from ..access import (
    CONSOLE_COMMAND,
    CONSOLE_VIEW,
    CONTROL,
    DELETE,
    MODERATE,
    OPERATORS,
    SETTINGS,
    START,
    VIEW,
    Principal,
)
from ..models import CHANNELS, ServerRecord
from .deps import NOT_ALLOWED, get_ctx, require_owner, require_principal, require_session, server_perm

router = APIRouter(tags=["servers"], dependencies=[Depends(require_session)])

Channel = Literal["stable", "nightly", "canary"]
OPERATOR_ACTIONS = {"op", "deop"}  # the rest of the player actions are for moderators


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


class OperatorLevelBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    level: int = Field(ge=1, le=4)


def _view(request: Request, principal: Principal, rec: ServerRecord, detail: bool = False) -> dict[str, Any]:
    ctx = get_ctx(request)
    return ctx.access.shape_server_view(principal, ctx.servers.view(rec, detail=detail))  # type: ignore[return-value]


@router.get("/servers")
async def list_servers(request: Request, principal: Principal = Depends(require_principal)) -> list:
    """The servers this account may see, each shaped for its role."""
    ctx = get_ctx(request)
    shaped = (ctx.access.shape_server_view(principal, view) for view in ctx.servers.list_views())
    return [view for view in shaped if view is not None]


@router.post("/servers", status_code=202)
async def create_server(body: CreateBody, request: Request, owner: Principal = Depends(require_owner)) -> JSONResponse:
    ctx = get_ctx(request)
    if body.channel is not None and body.channel not in CHANNELS:
        raise pc.ConfigError({"channel": "unknown channel"})
    return accepted(ctx.servers.submit_create(body.model_dump()))


@router.get("/servers/{server_id}")
async def get_server(
    request: Request, rec: ServerRecord = Depends(server_perm(VIEW)), principal: Principal = Depends(require_principal)
) -> dict:
    await get_ctx(request).servers.disk_usage(rec.id)
    return _view(request, principal, rec, detail=True)


@router.patch("/servers/{server_id}")
async def patch_server(
    request: Request, body: PatchBody, rec: ServerRecord = Depends(server_perm(SETTINGS)),
    principal: Principal = Depends(require_principal),
) -> dict:
    ctx = get_ctx(request)
    rec, restart_required = ctx.servers.patch(rec, body.model_dump(exclude_unset=True))
    view = _view(request, principal, rec, detail=True)
    view["restart_required"] = restart_required
    return view


@router.delete("/servers/{server_id}", status_code=202)
async def delete_server(
    request: Request, final_backup: bool = True, delete_backups: bool = False,
    rec: ServerRecord = Depends(server_perm(DELETE)),
) -> JSONResponse:
    return accepted(get_ctx(request).servers.submit_delete(rec, final_backup, delete_backups))


@router.post("/servers/{server_id}/start", status_code=202)
async def start(request: Request, rec: ServerRecord = Depends(server_perm(START))) -> JSONResponse:
    return accepted(get_ctx(request).servers.submit_start(rec))


@router.post("/servers/{server_id}/stop", status_code=202)
async def stop(request: Request, rec: ServerRecord = Depends(server_perm(CONTROL))) -> JSONResponse:
    return accepted(get_ctx(request).servers.submit_stop(rec))


@router.post("/servers/{server_id}/restart", status_code=202)
async def restart(request: Request, rec: ServerRecord = Depends(server_perm(CONTROL))) -> JSONResponse:
    return accepted(get_ctx(request).servers.submit_restart(rec))


@router.post("/servers/{server_id}/kill")
async def kill(request: Request, rec: ServerRecord = Depends(server_perm(CONTROL))) -> dict:
    await get_ctx(request).servers.kill(rec)
    return {"ok": True}


# --------------------------------------------------------------------- console
@router.post("/servers/{server_id}/command")
async def command(request: Request, body: CommandBody, rec: ServerRecord = Depends(server_perm(CONSOLE_COMMAND))) -> dict:
    await get_ctx(request).servers.command(rec, body.command)
    return {"ok": True}


@router.get("/servers/{server_id}/console")
async def console(
    request: Request, lines: int = Query(500, ge=1, le=3000), rec: ServerRecord = Depends(server_perm(CONSOLE_VIEW))
) -> dict:
    return {"lines": get_ctx(request).pm.backlog(rec.id, lines)}


# ---------------------------------------------------------------------- config
@router.get("/servers/{server_id}/config")
async def read_config(request: Request, rec: ServerRecord = Depends(server_perm(SETTINGS))) -> dict:
    return get_ctx(request).servers.read_config(rec)


@router.put("/servers/{server_id}/config")
async def write_config(request: Request, body: ConfigBody, rec: ServerRecord = Depends(server_perm(SETTINGS))) -> dict:
    return await get_ctx(request).servers.save_settings(rec, body.values)


@router.get("/servers/{server_id}/config/raw")
async def read_raw_config(request: Request, rec: ServerRecord = Depends(server_perm(SETTINGS))) -> dict:
    return get_ctx(request).servers.read_raw_config(rec)


@router.put("/servers/{server_id}/config/raw")
async def write_raw_config(request: Request, body: RawConfigBody, rec: ServerRecord = Depends(server_perm(SETTINGS))) -> dict:
    return get_ctx(request).servers.write_raw_config(rec, body.content)


# --------------------------------------------------------------------- players
@router.get("/servers/{server_id}/players")
async def players(request: Request, rec: ServerRecord = Depends(server_perm(MODERATE))) -> dict:
    ctx = get_ctx(request)
    rt = ctx.pm.runtime(rec.id)
    return {
        "online": {"count": rt.players_online, "max": rt.players_max, "names": rt.player_names},
        "lists": ctx.servers.read_player_lists(rec),
        "running": rt.state == "running",
        "op_level": ctx.servers.op_default_level(rec),
    }


@router.post("/servers/{server_id}/players")
async def player_action(
    request: Request, body: PlayerBody, rec: ServerRecord = Depends(server_perm(MODERATE)),
    principal: Principal = Depends(require_principal),
) -> dict:
    # Moderators may kick, ban and whitelist; making someone an operator takes more than that.
    if body.action in OPERATOR_ACTIONS and not principal.can(rec.id, OPERATORS):
        raise HTTPException(status_code=403, detail=NOT_ALLOWED)
    await get_ctx(request).servers.player_action(rec, body.action, body.target, body.reason)
    return {"ok": True}


@router.post("/servers/{server_id}/operators/level", status_code=202)
async def set_operator_level(
    request: Request, body: OperatorLevelBody, rec: ServerRecord = Depends(server_perm(OPERATORS))
) -> JSONResponse:
    """Give one operator a specific level. Restarts the server when it is running (see ``ServerService``)."""
    return accepted(get_ctx(request).servers.submit_operator_level(rec, body.name.strip(), body.level))
