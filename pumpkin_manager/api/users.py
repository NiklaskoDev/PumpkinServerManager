"""Accounts for friends. Administrator only."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..access import Principal
from .deps import get_ctx, require_owner

router = APIRouter(tags=["users"])

Role = Literal["player", "moderator", "admin"]


class CreateUserBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    servers: dict[str, Role] = Field(default_factory=dict)  # server id -> role


class AccessBody(BaseModel):
    servers: dict[str, Role]  # everything the account may access; servers left out lose access


@router.get("/users")
async def list_users(request: Request, owner: Principal = Depends(require_owner)) -> list[dict[str, Any]]:
    return get_ctx(request).accounts.list_all()


@router.post("/users", status_code=201)
async def create_user(body: CreateUserBody, request: Request, owner: Principal = Depends(require_owner)) -> JSONResponse:
    """Returns the new account and its temporary password. The password is shown this once: only a hash is kept."""
    user, temporary = await get_ctx(request).accounts.create(body.username, dict(body.servers), owner.username)
    return JSONResponse({"user": user, "temporary_password": temporary}, status_code=201)


@router.put("/users/{user_id}/servers")
async def set_access(user_id: int, body: AccessBody, request: Request, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    return get_ctx(request).accounts.set_access(user_id, dict(body.servers), owner.username)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, request: Request, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    """A new temporary password; the account is signed out everywhere and has to choose its own at the next sign-in."""
    temporary = await get_ctx(request).accounts.reset_password(user_id, owner.username)
    return {"temporary_password": temporary}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request, owner: Principal = Depends(require_owner)) -> dict[str, Any]:
    get_ctx(request).accounts.delete(user_id, owner.username)
    return {"ok": True}
