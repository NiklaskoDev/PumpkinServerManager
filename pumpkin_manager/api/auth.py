"""Login, logout and password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..security import SESSION_COOKIE, AuthError, Session, Throttled
from .deps import client_ip, get_ctx, require_session

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1000)


class PasswordBody(BaseModel):
    current: str = Field(max_length=1000)
    new: str = Field(max_length=1000)


def _secure_cookie(request: Request) -> bool:
    mode = get_ctx(request).settings.cookie_secure
    if mode == "always":
        return True
    if mode == "never":
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    ctx = get_ctx(request)
    try:
        token, session = await ctx.auth.login(body.username, body.password, client_ip(request), request.headers.get("user-agent"))
    except Throttled as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        SESSION_COOKIE, token, max_age=ctx.settings.session_hours * 3600, httponly=True, samesite="strict",
        secure=_secure_cookie(request), path="/",
    )
    ctx.reporter.record("info", "login", f"{session.username} signed in from {client_ip(request)}")
    return {"username": session.username, "csrf": session.csrf, "version": __version__}


@router.get("/auth/me")
async def me(session: Session = Depends(require_session)) -> dict:
    return {"username": session.username, "csrf": session.csrf, "version": __version__}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, session: Session = Depends(require_session)) -> dict:
    get_ctx(request).auth.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password")
async def change_password(body: PasswordBody, request: Request, session: Session = Depends(require_session)) -> dict:
    try:
        await get_ctx(request).auth.change_password(session, body.current, body.new)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_ctx(request).reporter.record("warning", "login", f"{session.username} changed their password")
    return {"ok": True}
