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
from ..models import AppSettings
from ..security import SESSION_COOKIE
from .deps import get_ctx, require_session

log = logging.getLogger(__name__)
router = APIRouter(tags=["system"])
protected = [Depends(require_session)]


@router.get("/health")
async def health(request: Request) -> dict:
    get_ctx(request).db.scalar("SELECT 1")
    return {"status": "ok", "version": __version__}


@router.get("/system", dependencies=protected)
async def system(request: Request) -> dict:
    ctx = get_ctx(request)
    info = ctx.system_info()
    info["summary"] = {
        "servers": len(ctx.repo.list_servers()),
        "running": len(ctx.pm.running_ids()),
        "players": sum(ctx.pm.runtime(sid).players_online for sid in ctx.pm.running_ids()),
        "updates": sum(1 for r in ctx.repo.list_servers() if r.pending_build_id),
        "active_jobs": ctx.jobs.active_count(),
    }
    return info


# -------------------------------------------------------------------- settings
class SettingsPatch(BaseModel):
    update_check_enabled: bool | None = None
    update_check_minutes: int | None = None
    default_channel: str | None = None
    github_token: str | None = None
    webhook_url: str | None = None
    webhook_events: list[str] | None = None
    telemetry_default: bool | None = None
    public_address: str | None = None
    keep_old_builds: int | None = None


def _settings_view(s: AppSettings) -> dict[str, Any]:
    data = s.model_dump()
    data["has_github_token"] = bool(data.pop("github_token"))
    return data


@router.get("/settings", dependencies=protected)
async def get_settings(request: Request) -> dict:
    return _settings_view(get_ctx(request).app_settings())


@router.put("/settings", dependencies=protected)
async def put_settings(body: SettingsPatch, request: Request) -> dict:
    ctx = get_ctx(request)
    changes = body.model_dump(exclude_unset=True)
    current = ctx.app_settings().model_dump()
    if "github_token" in changes:
        token = (changes["github_token"] or "").strip()
        changes["github_token"] = token or None
    merged = {**current, **changes}
    try:
        new = AppSettings.model_validate(merged)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HTTPException(status_code=422, detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from exc
    ctx.save_app_settings(new)
    return _settings_view(new)


class WebhookTest(BaseModel):
    url: str | None = None


@router.post("/settings/webhook-test", dependencies=protected)
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
@router.get("/activity", dependencies=protected)
async def activity(request: Request, limit: int = Query(100, ge=1, le=500), server_id: str | None = None) -> list:
    return get_ctx(request).repo.list_activity(limit, server_id)


@router.get("/jobs", dependencies=protected)
async def jobs(request: Request, limit: int = Query(30, ge=1, le=200), server_id: str | None = None) -> list:
    return get_ctx(request).repo.list_jobs(limit, server_id)


@router.get("/jobs/{job_id}", dependencies=protected)
async def job(request: Request, job_id: str) -> dict:
    row = get_ctx(request).repo.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Unknown job.")
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
    if ctx.auth.resolve(token) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    sub = ctx.events.subscribe({"servers", "jobs", "activity"})

    async def sender() -> None:
        while True:
            message = await sub.queue.get()
            await websocket.send_text(json.dumps(message, separators=(",", ":")))

    async def session_watch() -> None:
        while True:
            await asyncio.sleep(60)
            if ctx.auth.resolve(token) is None:
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
                if ctx.repo.get_server(server_id) is None:
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
