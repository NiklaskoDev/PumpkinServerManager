"""Shared FastAPI dependencies: the application context, authentication and the permission checks.

Every API route must declare what it needs through one of the dependencies below (each carries a ``policy`` marker that
``tests/test_access.py`` reads, so a route added later without a check fails the test-suite):

* ``require_session``       - any signed-in account (building block; routes also need one of the following)
* ``require_principal``     - a signed-in account; the handler itself filters what that account may see
* ``require_owner``         - the administrator only
* ``server_perm(perm)``     - the account holds ``perm`` on the server named in the path
* ``any_server_perm(perm)`` - the administrator, or an account holding ``perm`` on at least one server
* ``backup_perm(perm)``     - ``perm`` on the server a backup belongs to
* ``visible_job``           - a job on a server whose activity the account may see
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from ..access import ACTIVITY, Principal
from ..errors import UserError
from ..models import ServerRecord
from ..security import SESSION_COOKIE, Session, csrf_matches

if TYPE_CHECKING:
    from ..app import AppContext

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
NOT_ALLOWED = "You do not have permission to do that."


class PasswordChangeRequired(UserError):
    """The account is signed in with a temporary password and may do nothing but choose its own."""

    status = 403
    code = "password_change_required"


def policy(kind: str, perm: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def mark(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.policy = (kind, perm)  # type: ignore[attr-defined]
        return fn

    return mark


def get_ctx(request: Request) -> "AppContext":
    return request.app.state.ctx


def _authenticate(request: Request) -> Session:
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


@policy("session-while-password-pending")
def require_session_even_if_password_pending(request: Request) -> Session:
    """For the few routes a freshly created account may use before it has chosen its own password."""
    return _authenticate(request)


@policy("session")
def require_session(request: Request) -> Session:
    session = _authenticate(request)
    if session.must_change:
        raise PasswordChangeRequired("You are signed in with a temporary password. Choose your own password first.")
    return session


@policy("principal")
def require_principal(request: Request, session: Session = Depends(require_session)) -> Principal:
    principal = get_ctx(request).access.principal(session.user_id)
    if principal is None:  # the account was deleted while the session was still around
        raise HTTPException(status_code=401, detail="Please log in.")
    request.state.principal = principal
    return principal


@policy("owner")
def require_owner(principal: Principal = Depends(require_principal)) -> Principal:
    if not principal.is_owner:
        raise HTTPException(status_code=403, detail="Only the administrator can do this.")
    return principal


def server_perm(perm: str) -> Callable[..., ServerRecord]:
    """Dependency for routes with ``{server_id}``: returns the server if the account holds ``perm`` on it."""

    @policy("server", perm)
    def dependency(request: Request, server_id: str, principal: Principal = Depends(require_principal)) -> ServerRecord:
        # A server the account has no role on answers exactly like one that does not exist.
        rec = get_ctx(request).repo.get_server(server_id) if principal.can_see(server_id) else None
        if rec is None:
            raise HTTPException(status_code=404, detail="That server does not exist.")
        if not principal.can(server_id, perm):
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        return rec

    return dependency


def any_server_perm(perm: str) -> Callable[..., Principal]:
    @policy("any-server", perm)
    def dependency(principal: Principal = Depends(require_principal)) -> Principal:
        if not principal.can_anywhere(perm):
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        return principal

    return dependency


def backup_perm(perm: str) -> Callable[..., dict[str, Any]]:
    """Dependency for routes with ``{backup_id}``: returns the backup row if the account holds ``perm`` on its server."""

    @policy("backup", perm)
    def dependency(request: Request, backup_id: str, principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        row = get_ctx(request).repo.get_backup(backup_id)
        if row is None or not principal.can_see(row["server_id"]):
            raise HTTPException(status_code=404, detail="That backup does not exist.")
        if not principal.can(row["server_id"], perm):
            raise HTTPException(status_code=403, detail=NOT_ALLOWED)
        return row

    return dependency


@policy("job")
def visible_job(request: Request, job_id: str, principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    row = get_ctx(request).repo.get_job(job_id)
    if row is not None and not principal.is_owner and not (row["server_id"] and principal.can(row["server_id"], ACTIVITY)):
        row = None
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return row


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
