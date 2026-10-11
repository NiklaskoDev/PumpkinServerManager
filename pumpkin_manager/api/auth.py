"""Login, logout and password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..security import SESSION_COOKIE, AuthError, Session, Throttled
from .deps import client_ip, get_ctx, require_session_even_if_password_pending

router = APIRouter(tags=["auth"])


def _identity(request: Request, session: Session) -> dict:
    """Who is signed in, for the dashboard: the account type, whether a new password must be chosen, and (friends) the roles."""
    principal = get_ctx(request).access.principal(session.user_id)
    return {
        "id": session.user_id, "username": session.username, "csrf": session.csrf, "version": __version__,
        "kind": session.kind, "must_change_password": session.must_change,
        "servers": dict(principal.roles) if principal else {},
    }


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
    return _identity(request, session)


# The three routes below are the only ones an account with a temporary password may use before it has chosen its own.
@router.get("/auth/me")
async def me(request: Request, session: Session = Depends(require_session_even_if_password_pending)) -> dict:
    return _identity(request, session)


@router.post("/auth/logout")
async def logout(request: Request, response: Response, session: Session = Depends(require_session_even_if_password_pending)) -> dict:
    get_ctx(request).auth.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password")
async def change_password(
    body: PasswordBody, request: Request, session: Session = Depends(require_session_even_if_password_pending)
) -> dict:
    try:
        await get_ctx(request).auth.change_password(session, body.current, body.new)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    note = "chose a new password after signing in with a temporary one" if session.must_change else "changed their password"
    get_ctx(request).reporter.record("warning" if not session.must_change else "info", "login", f"{session.username} {note}")
    return {"ok": True}
