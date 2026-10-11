"""Backup listing, creation, restore, download and upload (server admins and the administrator)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..access import BACKUPS, Principal
from ..backups import BackupError, verify_archive
from ..models import ServerRecord
from ..servers import ServerError
from ..util import free_bytes, human_bytes
from .deps import NOT_ALLOWED, backup_perm, get_ctx, require_principal, require_session, server_perm

router = APIRouter(tags=["backups"], dependencies=[Depends(require_session)])


class CreateBackupBody(BaseModel):
    note: str | None = Field(None, max_length=200)


class RestoreBody(BaseModel):
    safety_backup: bool = True


class PatchBackupBody(BaseModel):
    protected: bool | None = None
    note: str | None = Field(None, max_length=200)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "server_id": row["server_id"], "server_name": row["server_name"], "kind": row["kind"],
        "file_name": row["file_name"].split("/")[-1], "size": row["size"], "created_at": row["created_at"],
        "build_tag": row["build_tag"], "consistent": bool(row["consistent"]), "verified": bool(row["verified"]),
        "protected": bool(row["protected"]), "note": row["note"],
    }


@router.get("/backups")
async def list_backups(request: Request, server_id: str | None = None, principal: Principal = Depends(require_principal)) -> dict:
    """Backups of the servers this account may manage backups for (everything, for the administrator)."""
    ctx = get_ctx(request)
    allowed = principal.server_ids_with(BACKUPS)  # None: every server
    if server_id is not None and allowed is not None and server_id not in allowed:
        if not principal.can_see(server_id):
            raise HTTPException(status_code=404, detail="That server does not exist.")
        raise HTTPException(status_code=403, detail=NOT_ALLOWED)
    rows = [r for r in ctx.repo.list_backups(server_id) if allowed is None or r["server_id"] in allowed]
    existing = {r.id for r in ctx.repo.list_servers()}
    return {
        "backups": [{**_public(r), "server_exists": r["server_id"] in existing} for r in rows],
        "total_size": sum(int(r["size"] or 0) for r in rows),
        "free": free_bytes(ctx.settings.backup_dir),
    }


@router.post("/servers/{server_id}/backups", status_code=202)
async def create_backup(request: Request, body: CreateBackupBody, rec: ServerRecord = Depends(server_perm(BACKUPS))) -> JSONResponse:
    return JSONResponse({"job": get_ctx(request).backups.submit_backup(rec, "manual", body.note)}, status_code=202)


@router.post("/backups/{backup_id}/restore", status_code=202)
async def restore_backup(
    request: Request, body: RestoreBody, row: dict[str, Any] = Depends(backup_perm(BACKUPS))
) -> JSONResponse:
    ctx = get_ctx(request)
    rec = ctx.repo.get_server(row["server_id"])
    if rec is None:
        raise ServerError("The server this backup belongs to no longer exists.", 404)
    job = ctx.backups.submit_restore(rec, row["id"], body.safety_backup, lambda r: ctx.servers.ensure_config(r))
    return JSONResponse({"job": job}, status_code=202)


@router.patch("/backups/{backup_id}")
async def patch_backup(request: Request, body: PatchBackupBody, row: dict[str, Any] = Depends(backup_perm(BACKUPS))) -> dict:
    ctx = get_ctx(request)
    changes = body.model_dump(exclude_unset=True)
    if "protected" in changes:
        changes["protected"] = int(bool(changes["protected"]))
    ctx.repo.update_backup(row["id"], **changes)
    return _public(ctx.repo.get_backup(row["id"]))  # type: ignore[arg-type]


@router.delete("/backups/{backup_id}")
async def delete_backup(request: Request, row: dict[str, Any] = Depends(backup_perm(BACKUPS))) -> dict:
    if row["protected"]:
        raise BackupError("This backup is protected. Remove the protection first.")
    await asyncio.to_thread(get_ctx(request).backups.delete, row["id"])
    return {"ok": True}


@router.get("/backups/{backup_id}/download")
async def download_backup(request: Request, row: dict[str, Any] = Depends(backup_perm(BACKUPS))) -> FileResponse:
    path = get_ctx(request).backups.archive_path(row)
    if not path.is_file():
        raise BackupError("The backup file is missing from disk.")
    return FileResponse(path, filename=f"{row['server_id']}-{path.name}", media_type="application/gzip")


@router.put("/servers/{server_id}/backups/upload")
async def upload_backup(
    request: Request, name: str = Query("upload.tar.gz", max_length=200), rec: ServerRecord = Depends(server_perm(BACKUPS))
) -> dict:
    """Accept a ``.tar.gz`` (for example from another machine) so it can be restored."""
    ctx = get_ctx(request)
    limit = ctx.settings.max_upload_bytes
    declared = int(request.headers.get("content-length") or 0)
    if declared > limit:
        raise BackupError(f"Uploads are limited to {limit // (1024 * 1024)} MiB (MAX_UPLOAD_MB).", 413)
    if declared and free_bytes(ctx.settings.backup_dir) < declared + 50 * 1024 * 1024:
        raise BackupError("Not enough free space in the backup folder.", 507)
    file_name = ctx.backups._new_file_name(rec.id, "uploaded")
    final = ctx.settings.backup_dir / file_name
    partial = final.with_name(final.name + ".partial")
    written = 0
    try:
        with open(partial, "wb") as fh:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:  # a client can lie about (or omit) Content-Length
                    raise BackupError(f"Uploads are limited to {limit // (1024 * 1024)} MiB (MAX_UPLOAD_MB).", 413)
                await asyncio.to_thread(fh.write, chunk)
        result = await asyncio.to_thread(verify_archive, partial, None)
        os.replace(partial, final)
    finally:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
    row = ctx.repo.insert_backup(
        server_id=rec.id, server_name=rec.name, kind="uploaded", file_name=file_name, size=written,
        sha256=result["sha256"], build_tag=(result.get("manifest") or {}).get("build_tag"), consistent=1, verified=1,
        note=f"Uploaded as {name}"[:200],
    )
    ctx.reporter.record("info", "backup", f"Backup uploaded for '{rec.name}' ({human_bytes(written)})", rec.id)
    return _public(row)
