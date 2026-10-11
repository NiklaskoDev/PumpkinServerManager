"""File browser API for a server's folder."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import files as fm
from ..util import free_bytes
from .deps import get_ctx, load_server, require_session

router = APIRouter(tags=["files"], dependencies=[Depends(require_session)])


class WriteBody(BaseModel):
    path: str = Field(max_length=1000)
    content: str


class PathBody(BaseModel):
    path: str = Field(max_length=1000)


class RenameBody(BaseModel):
    source: str = Field(max_length=1000)
    destination: str = Field(max_length=1000)


def _root(request: Request, server_id: str):
    ctx = get_ctx(request)
    load_server(request, server_id)
    return ctx, ctx.servers.server_dir(server_id)


@router.get("/servers/{server_id}/files")
async def list_files(request: Request, server_id: str, path: str = "") -> dict:
    _, root = _root(request, server_id)
    return await asyncio.to_thread(fm.list_dir, root, path)


@router.get("/servers/{server_id}/files/content")
async def read_file(request: Request, server_id: str, path: str) -> dict:
    _, root = _root(request, server_id)
    return await asyncio.to_thread(fm.read_text, root, path)


@router.put("/servers/{server_id}/files/content")
async def write_file(request: Request, server_id: str, body: WriteBody) -> dict[str, Any]:
    ctx, root = _root(request, server_id)
    rel = body.path.replace("\\", "/").strip("/")
    if rel == "pumpkin.toml":
        # The main config goes through validation so a typo cannot make Pumpkin silently ignore it.
        result = ctx.servers.write_raw_config(load_server(request, server_id), body.content)
        return {"saved": True, **result}
    await asyncio.to_thread(fm.write_text, root, body.path, body.content)
    return {"saved": True}


@router.get("/servers/{server_id}/files/download")
async def download(request: Request, server_id: str, path: str) -> FileResponse:
    _, root = _root(request, server_id)
    target = fm.resolve(root, path)
    if not target.is_file():
        raise fm.FileError("Only files can be downloaded.")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.put("/servers/{server_id}/files/upload")
async def upload(
    request: Request, server_id: str, name: str = Query(min_length=1, max_length=255), path: str = ""
) -> dict:
    ctx, root = _root(request, server_id)
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise fm.FileError("Invalid file name.")
    folder = fm.resolve(root, path)
    if not folder.is_dir():
        raise fm.FileError("The target folder does not exist.", 404)
    dest = fm.resolve(root, f"{path}/{name}", must_exist=False)
    if dest.exists() and dest.is_dir():
        raise fm.FileError("A folder with that name already exists.", 409)
    limit = ctx.settings.max_upload_bytes
    declared = int(request.headers.get("content-length") or 0)
    if declared and declared > limit:
        raise fm.FileError(f"Uploads are limited to {limit // (1024 * 1024)} MiB.", 413)
    if declared and free_bytes(root) < declared + 100 * 1024 * 1024:
        raise fm.FileError("Not enough free disk space for this upload.", 507)

    tmp = dest.with_name(f".{dest.name}.upload")
    written = 0
    try:
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    raise fm.FileError(f"Uploads are limited to {limit // (1024 * 1024)} MiB.", 413)
                await asyncio.to_thread(fh.write, chunk)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return {"saved": True, "size": written, "path": fm.rel_of(root, dest)}


@router.post("/servers/{server_id}/files/mkdir")
async def mkdir(request: Request, server_id: str, body: PathBody) -> dict:
    _, root = _root(request, server_id)
    await asyncio.to_thread(fm.make_dir, root, body.path)
    return {"ok": True}


@router.post("/servers/{server_id}/files/rename")
async def rename(request: Request, server_id: str, body: RenameBody) -> dict:
    _, root = _root(request, server_id)
    await asyncio.to_thread(fm.rename, root, body.source, body.destination)
    return {"ok": True}


@router.delete("/servers/{server_id}/files")
async def delete(request: Request, server_id: str, path: str) -> dict:
    _, root = _root(request, server_id)
    await asyncio.to_thread(fm.delete, root, path)
    return {"ok": True}
