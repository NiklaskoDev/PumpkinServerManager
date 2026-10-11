"""Accounts for friends. Administrator only."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..access import Principal
from .deps import get_ctx, require_owner

if TYPE_CHECKING:
    from ..app import AppContext

router = APIRouter(tags=["users"])

Role = Literal["player", "moderator", "admin"]


class CreateUserBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    servers: dict[str, Role] = Field(default_factory=dict)  # server id -> role


class AccessBody(BaseModel):
    servers: dict[str, Role]  # everything the account may access; servers left out lose access


# --------------------------------------------------------------------------- where friends sign in
def bare_host(value: str | None) -> str | None:
    """The host of what was typed as "public address", which may by mistake carry a scheme, a port or a path.

    IPv6 addresses come back in brackets, ready to be followed by ``:port``.
    """
    host = (value or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0]
    if host.startswith("["):
        end = host.find("]")
        host = host[: end + 1] if end != -1 else host
    elif host.count(":") == 1:  # name:port or ipv4:port
        host = host.split(":", 1)[0]
    elif host.count(":") > 1:  # a bare IPv6 address
        host = f"[{host}]"
    return host or None


def is_private_host(host: str) -> bool:
    """True for names and addresses that only exist on a home network: localhost, 192.168.x.x, 'tower', 'nas.local', ..."""
    name = host.strip("[]").lower()
    if name == "localhost" or name.endswith((".local", ".lan", ".home.arpa", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return "." not in name  # a one-word name such as the Unraid default 'Tower' is a name on the local network
    return ip.is_private or ip.is_loopback or ip.is_link_local


def sign_in_target(request: Request, ctx: "AppContext") -> dict[str, Any]:
    """Where a friend signs in: the address the administrator is using now, with the host replaced by the configured public
    address (the one shown to players), so a friend gets a name that works from outside rather than a LAN address."""
    forwarded_https = request.headers.get("x-forwarded-proto", "").lower() == "https"
    scheme = "https" if request.url.scheme == "https" or forwarded_https else "http"
    public = bare_host(ctx.app_settings().public_address or ctx.settings.public_address)
    netloc = request.url.netloc if public is None else public + (f":{request.url.port}" if request.url.port else "")
    hostname = urlsplit(f"//{netloc}").hostname or ""
    return {
        "sign_in_url": f"{scheme}://{netloc}",
        # Passwords and sessions of a plain http address cross the network unencrypted: fine at home, not over the internet.
        "sign_in_unencrypted": scheme == "http" and not is_private_host(hostname),
    }


# ------------------------------------------------------------------------------------------------ routes
@router.get("/users")
async def list_users(request: Request, owner: Principal = Depends(require_owner)) -> list[dict[str, Any]]:
    return get_ctx(request).accounts.list_all()


@router.post("/users", status_code=201)
async def create_user(body: CreateUserBody, request: Request, owner: Principal = Depends(require_owner)) -> JSONResponse:
    """Returns the new account and its temporary password. The password is shown this once: only a hash is kept."""
    ctx = get_ctx(request)
    user, temporary = await ctx.accounts.create(body.username, dict(body.servers), owner.username)
    return JSONResponse({"user": user, "temporary_password": temporary, **sign_in_target(request, ctx)}, status_code=201)


@router.put("/users/{user_id}/servers")
async def set_access(user_id: int, body: AccessBody, request: Request, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    return get_ctx(request).accounts.set_access(user_id, dict(body.servers), owner.username)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, request: Request, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    """A new temporary password; the account is signed out everywhere and has to choose its own at the next sign-in."""
    ctx = get_ctx(request)
    temporary = await ctx.accounts.reset_password(user_id, owner.username)
    return {"temporary_password": temporary, **sign_in_target(request, ctx)}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    get_ctx(request).accounts.delete(user_id, owner.username)
    return {"ok": True}
