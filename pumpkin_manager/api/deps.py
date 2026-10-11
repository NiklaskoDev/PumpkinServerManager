"""Shared FastAPI dependencies: the application context and authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from ..models import ServerRecord
from ..security import SESSION_COOKIE, Session, csrf_matches

if TYPE_CHECKING:
    from ..app import AppContext

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_ctx(request: Request) -> "AppContext":
    return request.app.state.ctx


def require_session(request: Request) -> Session:
    """Authenticate the cookie; unsafe methods must also carry the session's CSRF token.

    Browsers share cookies between ports of the same host, so another web app on the same machine
    could otherwise make authenticated cross-origin requests to this one.
    """
    ctx = get_ctx(request)
    session = ctx.auth.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="Please log in.")
    if request.method not in SAFE_METHODS and not csrf_matches(session, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token. Reload the page and try again.")
    request.state.session = session
    return session


def load_server(request: Request, server_id: str) -> ServerRecord:
    return get_ctx(request).servers.get(server_id)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
