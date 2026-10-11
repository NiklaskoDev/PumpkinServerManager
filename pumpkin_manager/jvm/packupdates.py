"""Updating a modpack server to another version of its modpack.

The order is the safety guarantee, the same as for Pumpkin updates: fetch everything while the server keeps running,
stop it, make a verified backup, switch the files, start it and watch it, and if it does not come up restore the backup.
Mods and configuration live in the server folder, so restoring the backup returns the whole server to how it was.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ..backups import BackupError
from ..jobs import JobContext
from ..mcnet import PingError, server_list_ping
from ..models import ServerRecord
from ..servers import ServerError
from ..util import iso
from .loaders import LoaderError
from .modrinth import summarize_version
from .mojang import available_java
from .mrpack import PackDownloadError, PackError, read_pack
from .net import BadDownloadError, DownloadError
from .service import JavaServerService, client_only_info

log = logging.getLogger(__name__)

PREVIOUS_FIELDS = ("minecraft", "loader", "java", "launch", "pack")  # what a rollback puts back besides the files


def pending_of(rec: ServerRecord) -> dict[str, Any] | None:
    return (rec.spec.get("pack") or {}).get("pending")


class PackUpdater:
    def __init__(self, jvm: JavaServerService, stability_seconds: float = 15.0):
        self.jvm = jvm
        self.svc = jvm.svc
        self.stability_seconds = stability_seconds

    # ----------------------------------------------------------------- checking
    @staticmethod
    def updatable(rec: ServerRecord) -> bool:
        return rec.is_java and (rec.spec.get("pack") or {}).get("source") == "modrinth"

    async def check(self, only: ServerRecord | None = None) -> dict[str, Any]:
        """Ask Modrinth whether the modpacks of the servers have a newer version and flag the servers that can be updated."""
        svc = self.svc
        servers = [only] if only is not None else [r for r in svc.repo.list_servers() if self.updatable(r)]
        versions: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        flagged = 0
        for rec in servers:
            project = (rec.spec.get("pack") or {}).get("project")
            if not project:
                continue
            if rec.policy.update_mode == "off" and only is None:
                self._store(rec, None)
                continue
            if project not in versions and project not in errors:
                try:
                    versions[project] = await self.jvm.modrinth.versions(project)
                except DownloadError as exc:
                    errors[project] = str(exc)
            if project in errors:
                continue
            if self._evaluate(rec, versions[project]):
                flagged += 1
        return {"servers_with_updates": flagged, "errors": errors}

    def _evaluate(self, rec: ServerRecord, versions: list[dict[str, Any]]) -> bool:
        """Set or clear the pending update of one server. Returns True if one is pending."""
        pack = rec.spec.get("pack") or {}
        kinds = ("release",) if pack.get("version_type", "release") == "release" else ("release", "beta", "alpha")
        newest = next((v for v in versions if v.get("version_type") in kinds), None)  # newest first
        newer = (
            newest is not None and newest["id"] != pack.get("version_id") and newest.get("date_published", "") > (pack.get("published") or "")
            and self.svc.repo.get_kv(f"skip:{rec.id}") != newest["id"]
        )
        if not newer:
            self._store(rec, None)
            return False
        summary = summarize_version(newest)
        found = {k: summary[k] for k in ("id", "number", "type", "published", "minecraft", "loaders")}
        if pending_of(rec) != found:
            if (pending_of(rec) or {}).get("id") != found["id"]:
                self.svc.reporter.record(
                    "info", "update", f"Update available for '{rec.name}': {pack.get('title') or 'the modpack'} {found['number']}", rec.id,
                    notify="update_available" if rec.policy.update_mode == "notify" else None, title=f"Update available for {rec.name}",
                )
            self._store(rec, found)
        else:
            self.svc.repo.update_server(rec.id, last_update_check_at=iso())
        return True

    def _store(self, rec: ServerRecord, pending: dict[str, Any] | None) -> None:
        rec = self.svc.get(rec.id)
        spec = dict(rec.spec)
        pack = dict(spec.get("pack") or {})
        before = pack.get("pending")
        if pending is None:
            pack.pop("pending", None)
        else:
            pack["pending"] = pending
        spec["pack"] = pack
        self.svc.repo.update_server(
            rec.id, spec=spec, last_update_check_at=iso(), pending_since=(rec.pending_since or iso()) if pending else None,
        )
        if before != pending:
            self.svc.publish_state(rec.id)

    async def available(self, rec: ServerRecord) -> dict[str, Any]:
        pack = rec.spec.get("pack") or {}
        if pack.get("source") != "modrinth":
            raise ServerError("Only modpacks from Modrinth have versions to choose from.", 409)
        try:
            versions = await self.jvm.modrinth.versions(pack["project"])
        except DownloadError as exc:
            raise ServerError(str(exc)) from exc
        return {"current": pack.get("version_id"), "pending": pack.get("pending"), "versions": [summarize_version(v) for v in versions]}

    # ----------------------------------------------------------------- applying
    def submit_update(self, rec: ServerRecord, version_id: str | None = None, *, trigger: str = "manual") -> dict[str, Any]:
        pack = rec.spec.get("pack") or {}
        if pack.get("source") != "modrinth":
            raise ServerError("Only modpacks from Modrinth can be updated from here.", 409)
        target = version_id or (pack.get("pending") or {}).get("id")
        if not target:
            raise ServerError("There is no newer version to install. Check for updates first.", 409)
        label = f"{pack.get('title') or 'modpack'} {(pack.get('pending') or {}).get('number') or ''}".strip()
        return self.svc.jobs.submit("update", f"Update {rec.name} to {label}", rec.id, lambda ctx: self.apply(rec, target, ctx, trigger))

    async def apply(self, rec: ServerRecord, version_id: str, ctx: JobContext, trigger: str = "manual") -> None:
        svc, jvm = self.svc, self.jvm
        rec = svc.get(rec.id)
        old_spec = rec.spec
        old_pack = dict(old_spec.get("pack") or {})
        if old_pack.get("version_id") == version_id:
            raise ServerError("The server already runs that version.")
        directory = svc.server_dir(rec.id)
        old_lock = jvm.lock_of(rec)
        if old_lock is None:
            raise ServerError(
                "This server has no record of the files its modpack installed (.manager/pack.json), so an update could not tell "
                "what to replace. Restore a backup that has it, or create the server again from the modpack."
            )
        rt = svc.pm.runtime(rec.id)
        was_running = rt.state in ("starting", "running")
        title = old_pack.get("title") or "the modpack"
        update_id = svc.repo.insert_update(
            server_id=rec.id, from_build_id=None, to_build_id=None, from_tag=old_pack.get("version_number"), to_tag=None, trigger=trigger,
        )
        pack_path = jvm.uploads_dir / f"{uuid.uuid4().hex}.mrpack"
        backup_row: dict[str, Any] | None = None
        switched = False
        to_tag = version_id
        try:
            # 1. Fetch everything while the server keeps running: a failure here costs no downtime.
            ctx.step("Looking up the new version")
            version = await jvm.modrinth.version(version_id)
            if version.get("project_id") != old_pack.get("project"):
                raise ServerError("That version belongs to another modpack.")
            to_tag = str(version.get("version_number") or version_id)
            svc.repo.update_update(update_id, to_tag=to_tag)
            ctx.step(f"Updating '{rec.name}': {title} {old_pack.get('version_number')} -> {to_tag}")
            ctx.step(f"Downloading {title} {to_tag}")
            await jvm.modrinth.download_pack(version, pack_path)
            index = read_pack(pack_path)
            if old_spec.get("leave_out_client_only", True):
                await jvm.leave_out_client_only(index, ctx)
            major = available_java(await jvm.mojang.java_major(index.minecraft))
            java = await jvm.runtimes.ensure(major, jvm._java_progress(ctx, major))
            ctx.step("Downloading the new mods (the server keeps running meanwhile)")
            await jvm.packs.prefetch(index, lambda fraction, message: ctx.progress(0.1 + 0.4 * fraction, message))
            loader_changed = (index.loader.kind, index.loader.version, index.minecraft) != (
                (old_spec.get("loader") or {}).get("kind"), (old_spec.get("loader") or {}).get("version"), old_spec.get("minecraft"),
            )
            if index.minecraft != old_spec.get("minecraft"):
                ctx.warn(
                    f"This update changes Minecraft from {old_spec.get('minecraft')} to {index.minecraft}. The world is converted when "
                    "the server starts and cannot be opened by the old version again; the backup made now is the way back."
                )

            # 2. Tell players what is about to happen, then stop gracefully so the world is saved.
            if rt.state == "running" and trigger == "manual" and rt.players_online > 0:
                ctx.step(f"Warning {rt.players_online} player(s): restarting in 30s for an update")
                await svc.pm.send_command(rec.id, "say The modpack is being updated. The server restarts in 30 seconds.", echo=False)
                await asyncio.sleep(30)
            if rt.is_active:
                await svc._do_stop(rec, ctx)
            ctx.progress(0.55)

            # 3. Mandatory backup. Without a verified backup nothing is changed.
            ctx.step("Creating the backup that protects this update")
            backup_row = await svc.backups.create(
                svc.get(rec.id), "pre-update", ctx, mode="cold", note=f"Before updating to {title} {to_tag}",
                build_tag=f"{title} {old_pack.get('version_number') or ''}".strip(),
            )
            svc.repo.update_update(update_id, backup_id=backup_row["id"])
            ctx.progress(0.7)

            # 4. Switch the files, and the loader when it changed.
            switched = True
            ctx.step("Switching the mods and configuration")
            lock, report = await jvm.packs.update(old_lock, index, pack_path, directory, lambda fraction, message: ctx.progress(0.7 + 0.1 * fraction, message))
            ctx.step(
                f"{len(report['added'])} mods added, {len(report['updated'])} updated, {len(report['removed'])} removed"
                + (f"; {len(report['kept_mods'])} you changed by hand were left alone" if report["kept_mods"] else "")
            )
            if report["kept_configs"]:
                ctx.warn(
                    f"{len(report['kept_configs'])} configuration file(s) you changed were kept; the modpack's new version of each is "
                    "saved next to it with the ending .pack-new: " + ", ".join(report["kept_configs"][:8])
                )
            launch = old_spec.get("launch")
            if loader_changed:
                ctx.step(f"Installing {index.loader.label}")
                launch = await jvm.loaders.install(index.loader, directory, java.java, lambda message: ctx.progress(None, message))
            fresh = {k: v for k, v in old_pack.items() if k != "pending"}
            new_pack = {**fresh, "version_id": version["id"], "version_number": to_tag, "published": version.get("date_published"),
                        "version_type": version.get("version_type", "release"), **client_only_info(index)}
            previous = {k: old_spec.get(k) for k in PREVIOUS_FIELDS}
            svc.repo.update_server(
                rec.id, pending_since=None, spec={
                    **old_spec, "minecraft": index.minecraft, "loader": {"kind": index.loader.kind, "version": index.loader.version},
                    "java": major, "launch": launch, "pack": new_pack, "previous": previous,
                },
            )
            svc.publish_state(rec.id)

            # 5. Bring it back up and make sure it stays up.
            if was_running:
                ctx.step("Starting the updated server (a big modpack takes a few minutes)")
                await svc._do_start(svc.get(rec.id), ctx)
                await self._watch_stability(rec, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(rec, old_spec, old_lock, update_id, backup_row, was_running, switched, version_id, to_tag, ctx, exc)
            return
        finally:
            pack_path.unlink(missing_ok=True)

        svc.repo.update_update(update_id, status="success", message="Updated", finished_at=iso())
        svc.repo.set_kv(f"skip:{rec.id}", "")
        ctx.step(f"Update complete: now running {title} {to_tag}")
        svc.reporter.record(
            "success", "update", f"'{rec.name}' updated to {title} {to_tag}", rec.id, notify="update_applied", title=f"{rec.name} updated",
        )
        svc.publish_state(rec.id)

    async def _watch_stability(self, rec: ServerRecord, ctx: JobContext) -> None:
        if self.stability_seconds <= 0:
            return
        ctx.step(f"Watching the new version for {int(self.stability_seconds)} seconds")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.stability_seconds
        while loop.time() < deadline:
            rt = self.svc.pm.runtime(rec.id)
            if rt.state != "running":
                raise ServerError(f"The updated server stopped by itself ({rt.last_error or rt.state}).")
            await asyncio.sleep(1)
        try:
            await server_list_ping("127.0.0.1", self.svc.get(rec.id).java_port, timeout=3)
        except PingError as exc:
            raise ServerError(f"The updated server no longer answers on its port ({exc}).") from exc

    async def _handle_failure(
        self, rec: ServerRecord, old_spec: dict[str, Any], old_lock: dict[str, Any], update_id: str, backup_row: dict[str, Any] | None,
        was_running: bool, switched: bool, version_id: str, to_tag: str, ctx: JobContext, exc: Exception,
    ) -> None:
        svc = self.svc
        known = (ServerError, BackupError, PackError, LoaderError, DownloadError)
        reason = str(exc) if isinstance(exc, known) else f"Unexpected error: {exc}"
        if not isinstance(exc, known):
            log.exception("Update of %s failed", rec.id)
        ctx.step(f"Update failed: {reason}", "error")
        rt = svc.pm.runtime(rec.id)
        if switched or (isinstance(exc, (PackError, BadDownloadError)) and not isinstance(exc, PackDownloadError)):
            svc.repo.set_kv(f"skip:{rec.id}", version_id)  # the version itself is the problem: do not offer it again
        if not switched:
            # Nothing was changed. Put the server back the way it was.
            svc.repo.update_update(update_id, status="failed", message=reason, finished_at=iso())
            if was_running and not rt.is_active:
                ctx.step("Restarting the server on the previous version")
                try:
                    await svc._do_start(svc.get(rec.id), ctx)
                except Exception as restart_exc:
                    ctx.step(f"The previous version could not be restarted: {restart_exc}", "error")
            svc.reporter.record(
                "error", "update", f"Update of '{rec.name}' to {to_tag} was not applied: {reason}", rec.id,
                notify="update_failed", title=f"Update of {rec.name} failed",
            )
            svc.publish_state(rec.id)
            return

        # The new version was switched in but did not start properly: put everything back from the backup.
        ctx.step(f"Rolling back to {(old_spec.get('pack') or {}).get('version_number') or 'the previous version'}", "warning")
        rolled_back = False
        try:
            if rt.is_active:
                await svc.pm.stop(rec.id, 20)
            if backup_row is not None:
                await svc.backups.restore(
                    svc.get(rec.id), backup_row["id"], ctx, safety_backup=False, restart=False,
                    reapply_config=lambda r: svc.ensure_config(r, ctx),
                )
            svc.repo.update_server(rec.id, spec=old_spec, pending_since=None)
            if was_running:
                await svc._do_start(svc.get(rec.id), ctx)
            rolled_back = True
        except Exception as rollback_exc:
            ctx.step(f"The rollback failed: {rollback_exc}", "error")
            log.exception("Rollback of %s failed", rec.id)
        if rolled_back:
            svc.repo.update_update(update_id, status="rolled_back", message=reason, finished_at=iso())
            svc.reporter.record(
                "warning", "update", f"Update of '{rec.name}' to {to_tag} failed ({reason}); rolled back to the previous version",
                rec.id, notify="update_failed", title=f"Update of {rec.name} rolled back",
            )
        else:
            svc.repo.update_update(update_id, status="failed", message=f"{reason}; rollback failed", finished_at=iso())
            svc.reporter.record(
                "error", "update",
                f"Update of '{rec.name}' failed and the automatic rollback did not complete. The backup "
                f"{backup_row['file_name'] if backup_row else ''} is intact; restore it from the Backups tab.",
                rec.id, notify="update_failed", title=f"Update of {rec.name} failed - action needed",
            )
        svc.publish_state(rec.id)

    # ---------------------------------------------------------------- rollback
    def submit_rollback(self, rec: ServerRecord) -> dict[str, Any]:
        if not rec.spec.get("previous"):
            raise ServerError("This server has no earlier modpack version to go back to.", 409)
        return self.svc.jobs.submit("rollback", f"Roll back {rec.name}", rec.id, lambda ctx: self.rollback(rec, ctx))

    async def rollback(self, rec: ServerRecord, ctx: JobContext) -> None:
        """Go back to how the server was before its last update: the backup made then is restored, mods and world included."""
        svc = self.svc
        rec = svc.get(rec.id)
        previous = rec.spec.get("previous")
        last = svc.repo.latest_update(rec.id, ("success",))
        backup = svc.repo.get_backup(last["backup_id"]) if last and last.get("backup_id") else None
        if not previous or backup is None:
            raise ServerError("The backup that was made before the last update no longer exists, so there is nothing to go back to.")
        current_pack = rec.spec.get("pack") or {}
        old_pack = previous.get("pack") or {}
        update_id = svc.repo.insert_update(
            server_id=rec.id, from_build_id=None, to_build_id=None, from_tag=current_pack.get("version_number"),
            to_tag=old_pack.get("version_number"), trigger="rollback",
        )
        was_running = svc.pm.runtime(rec.id).is_active
        try:
            if was_running:
                await svc._do_stop(rec, ctx)
            ctx.step("Restoring the server from the backup made before the update (everything since then is lost)")
            await svc.backups.restore(rec, backup["id"], ctx, safety_backup=True, restart=False, reapply_config=lambda r: svc.ensure_config(r, ctx))
            fresh = svc.get(rec.id)
            restored = {k: v for k, v in fresh.spec.items() if k != "previous"}
            restored.update({k: previous[k] for k in PREVIOUS_FIELDS if previous.get(k) is not None})
            svc.repo.update_server(rec.id, spec=restored, pending_since=None)
            if was_running:
                await svc._do_start(svc.get(rec.id), ctx)
        except Exception as exc:
            svc.repo.update_update(update_id, status="failed", message=str(exc), finished_at=iso())
            raise
        svc.repo.update_update(update_id, status="success", message="Rolled back", finished_at=iso())
        svc.repo.set_kv(f"skip:{rec.id}", current_pack.get("version_id") or "")
        svc.reporter.record(
            "warning", "update", f"'{rec.name}' rolled back to {old_pack.get('title') or 'the modpack'} {old_pack.get('version_number')} "
            "(world and mods restored from the backup)", rec.id,
        )
        svc.publish_state(rec.id)
