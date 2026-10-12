"""System information, settings, activity feed, jobs and the live-events WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from .. import __version__
from ..access import ACTIVITY, CONSOLE_VIEW, Principal
from ..jvm.net import DownloadError
from ..models import AppSettings
from ..security import SESSION_COOKIE
from .deps import NOT_ALLOWED, get_ctx, require_owner, require_principal, visible_job

log = logging.getLogger(__name__)
router = APIRouter(tags=["system"])
owner_only = [Depends(require_owner)]


@router.get("/health")
async def health(request: Request) -> dict:
    get_ctx(request).db.scalar("SELECT 1")
    return {"status": "ok", "version": __version__}


@router.get("/system", dependencies=owner_only)
async def system(request: Request) -> dict:
    ctx = get_ctx(request)
    info = ctx.system_info()
    info["summary"] = {
        "servers": len(ctx.repo.list_servers()),
        "running": len(ctx.pm.running_ids()),
        "players": sum(ctx.pm.runtime(sid).players_online for sid in ctx.pm.running_ids()),
        "updates": sum(
            1 for r in ctx.repo.list_servers()
            if r.pending_build_id or (r.spec.get("pack") or {}).get("pending") or (r.spec.get("software") or {}).get("pending")
        ),
        "active_jobs": ctx.jobs.active_count(),
    }
    return info


# -------------------------------------------------------------------- settings
class SettingsPatch(BaseModel):
    update_check_enabled: bool | None = None
    update_check_minutes: int | None = None
    default_channel: str | None = None
    github_token: str | None = None
    curseforge_api_key: str | None = None
    webhook_url: str | None = None
    webhook_events: list[str] | None = None
    telemetry_default: bool | None = None
    public_address: str | None = None
    keep_old_builds: int | None = None


def _settings_view(s: AppSettings, *, curseforge_from_environment: bool = False) -> dict[str, Any]:
    data = s.model_dump()
    data["has_github_token"] = bool(data.pop("github_token"))
    data["has_curseforge_key"] = bool(data.pop("curseforge_api_key")) or curseforge_from_environment  # the key itself is never sent out
    data["curseforge_key_from_environment"] = curseforge_from_environment and not s.curseforge_api_key
    return data


@router.get("/settings", dependencies=owner_only)
async def get_settings(request: Request) -> dict:
    ctx = get_ctx(request)
    return _settings_view(ctx.app_settings(), curseforge_from_environment=bool(ctx.settings.curseforge_api_key))


@router.put("/settings", dependencies=owner_only)
async def put_settings(body: SettingsPatch, request: Request) -> dict:
    ctx = get_ctx(request)
    changes = body.model_dump(exclude_unset=True)
    current = ctx.app_settings().model_dump()
    if "github_token" in changes:
        token = (changes["github_token"] or "").strip()
        changes["github_token"] = token or None
    if "curseforge_api_key" in changes:
        key = (changes["curseforge_api_key"] or "").strip()
        changes["curseforge_api_key"] = key or None
        if key:  # a wrong key (an upload token, a typo) is the usual mistake: say so now, not when a pack fails to install
            try:
                await ctx.servers.jvm.curseforge.check_key(key)
            except DownloadError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    merged = {**current, **changes}
    try:
        new = AppSettings.model_validate(merged)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HTTPException(status_code=422, detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from exc
    ctx.save_app_settings(new)
    return _settings_view(new, curseforge_from_environment=bool(ctx.settings.curseforge_api_key))


class WebhookTest(BaseModel):
    url: str | None = None


@router.post("/settings/webhook-test", dependencies=owner_only)
async def webhook_test(body: WebhookTest, request: Request) -> dict:
    ctx = get_ctx(request)
    url = body.url or ctx.app_settings().webhook_url
    if not url:
        raise HTTPException(status_code=400, detail="Enter a webhook URL first.")
    try:
        AppSettings(webhook_url=url)
        await ctx.notifier.test(url)
    except (ValidationError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=f"The webhook did not accept the test message: {exc}") from exc
    return {"ok": True}


# ------------------------------------------------------------- activity / jobs
def _allowed_servers(principal: Principal, server_id: str | None) -> set[str] | None:
    """Which servers' entries an account may list (None: all of them). A server it may not follow answers like a missing one."""
    allowed = principal.server_ids_with(ACTIVITY)
    if allowed is None:
        return None
    if server_id is not None:
        if server_id not in allowed:
            if not principal.can_see(server_id):
                raise HTTPException(status_code=404, detail="That server does not exist.")
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        return {server_id}
    return allowed


@router.get("/activity")
async def activity(
    request: Request, limit: int = Query(100, ge=1, le=500), server_id: str | None = None,
    principal: Principal = Depends(require_principal),
) -> list:
    """The activity feed. Accounts other than the administrator only get entries about servers they moderate or administer."""
    return get_ctx(request).repo.list_activity(limit, server_id, _allowed_servers(principal, server_id))


@router.get("/jobs")
async def jobs(
    request: Request, limit: int = Query(30, ge=1, le=200), server_id: str | None = None,
    principal: Principal = Depends(require_principal),
) -> list:
    return get_ctx(request).repo.list_jobs(limit, server_id, _allowed_servers(principal, server_id))


@router.get("/jobs/{job_id}")
async def job(row: dict = Depends(visible_job)) -> dict:
    return row


# ------------------------------------------------------------------- websocket
@router.websocket("/ws")
async def events_socket(websocket: WebSocket) -> None:
    ctx = websocket.app.state.ctx
    # Cross-site WebSocket hijacking guard: browsers always send Origin; it must match this host.
    origin, host = websocket.headers.get("origin"), websocket.headers.get("host")
    if origin and urlparse(origin).netloc != host:
        await websocket.close(code=4403)
        return
    token = websocket.cookies.get(SESSION_COOKIE)
    session = ctx.auth.resolve(token)
    principal = ctx.access.principal(session.user_id) if session else None
    if session is None or principal is None:
        await websocket.close(code=4401)
        return
    if session.must_change:  # a temporary password has to be replaced before anything else works
        await websocket.close(code=4403)
        return
    await websocket.accept()
    topics = {"servers", "jobs", "activity"}
    # Everything a friend receives goes through the filter: only their servers, and only what their role may see. The
    # administrator's events pass it too (they only gain the ``access`` entry on server views) and are never held back.
    user_id = principal.user_id
    sub = ctx.events.subscribe(
        topics, user_id=user_id, view=lambda message: ctx.access.filter_event(user_id, message), unrestricted=principal.is_owner
    )

    def session_ended() -> bool:
        current_session = ctx.auth.resolve(token)
        return current_session is None or current_session.must_change

    async def sender() -> None:
        while True:
            message = await sub.queue.get()
            # "Your access changed" is also what an account gets when it is reset or deleted: make sure the session still stands.
            if message["type"] == "access" and session_ended():
                await websocket.close(code=4401)
                return
            await websocket.send_text(json.dumps(message, separators=(",", ":")))

    async def session_watch() -> None:
        while True:
            await asyncio.sleep(10)
            if session_ended():
                await websocket.close(code=4401)
                return

    tasks = [asyncio.create_task(sender()), asyncio.create_task(session_watch())]
    try:
        while True:
            try:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            op, topic = msg.get("op"), str(msg.get("topic", ""))
            if op == "ping":
                await websocket.send_text('{"type":"pong"}')
            elif op == "subscribe" and topic.startswith("console:"):
                server_id = topic.split(":", 1)[1]
                if session_ended():
                    await websocket.close(code=4401)
                    break
                current = ctx.access.principal(session.user_id)  # roles may have changed since the socket was opened
                if ctx.repo.get_server(server_id) is None or current is None or not current.can(server_id, CONSOLE_VIEW):
                    continue
                sub.topics.add(topic)  # subscribe first, then snapshot: the client de-duplicates by line number
                await websocket.send_text(json.dumps({
                    "topic": topic, "type": "backlog",
                    "data": {"server_id": server_id, "lines": ctx.pm.backlog(server_id, 500)},
                }, separators=(",", ":")))
            elif op == "unsubscribe" and topic.startswith("console:"):
                sub.topics.discard(topic)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("events socket closed", exc_info=True)
    finally:
        ctx.events.unsubscribe(sub)
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await websocket.close()
