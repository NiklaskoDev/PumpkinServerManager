"""File browser API for a server's folder (server admins and the administrator)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import files as fm
from ..access import FILES
from ..models import ServerRecord
from ..util import free_bytes
from .deps import get_ctx, require_session, server_perm

router = APIRouter(tags=["files"], dependencies=[Depends(require_session)])


class WriteBody(BaseModel):
    path: str = Field(max_length=1000)
    content: str


class PathBody(BaseModel):
    path: str = Field(max_length=1000)


class RenameBody(BaseModel):
    source: str = Field(max_length=1000)
    destination: str = Field(max_length=1000)


def _root(request: Request, rec: ServerRecord) -> Path:
    return get_ctx(request).servers.server_dir(rec.id)


@router.get("/servers/{server_id}/files")
async def list_files(request: Request, path: str = "", rec: ServerRecord = Depends(server_perm(FILES))) -> dict:
    return await asyncio.to_thread(fm.list_dir, _root(request, rec), path)


@router.get("/servers/{server_id}/files/content")
async def read_file(request: Request, path: str, rec: ServerRecord = Depends(server_perm(FILES))) -> dict:
    return await asyncio.to_thread(fm.read_text, _root(request, rec), path)


@router.put("/servers/{server_id}/files/content")
async def write_file(request: Request, body: WriteBody, rec: ServerRecord = Depends(server_perm(FILES))) -> dict[str, Any]:
    ctx = get_ctx(request)
    rel = body.path.replace("\\", "/").strip("/")
    if rel == "pumpkin.toml":
        # The main config goes through validation so a typo cannot make Pumpkin silently ignore it.
        result = ctx.servers.write_raw_config(rec, body.content)
        return {"saved": True, **result}
    await asyncio.to_thread(fm.write_text, _root(request, rec), body.path, body.content)
    return {"saved": True}


@router.get("/servers/{server_id}/files/download")
async def download(request: Request, path: str, rec: ServerRecord = Depends(server_perm(FILES))) -> FileResponse:
    target = fm.resolve(_root(request, rec), path)
    if not target.is_file():
        raise fm.FileError("Only files can be downloaded.")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.put("/servers/{server_id}/files/upload")
async def upload(
    request: Request, name: str = Query(min_length=1, max_length=255), path: str = "",
    rec: ServerRecord = Depends(server_perm(FILES)),
) -> dict:
    ctx, root = get_ctx(request), _root(request, rec)
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
async def mkdir(request: Request, body: PathBody, rec: ServerRecord = Depends(server_perm(FILES))) -> dict:
    await asyncio.to_thread(fm.make_dir, _root(request, rec), body.path)
    return {"ok": True}


@router.post("/servers/{server_id}/files/rename")
async def rename(request: Request, body: RenameBody, rec: ServerRecord = Depends(server_perm(FILES))) -> dict:
    await asyncio.to_thread(fm.rename, _root(request, rec), body.source, body.destination)
    return {"ok": True}


@router.delete("/servers/{server_id}/files")
async def delete(request: Request, path: str, rec: ServerRecord = Depends(server_perm(FILES))) -> dict:
    await asyncio.to_thread(fm.delete, _root(request, rec), path)
    return {"ok": True}
