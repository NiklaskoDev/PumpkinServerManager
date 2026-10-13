"""Updating a modpack server to another version of its modpack.

The order is the safety guarantee, the same as for Pumpkin updates: fetch everything while the server keeps running,
stop it, make a verified backup, switch the files, start it and watch it, and if it does not come up restore the backup.
Mods and configuration live in the server folder, so restoring the backup returns the whole server to how it was.

``JavaUpdates`` holds what updating a modpack and updating a server's software (``softwareupdates``) have in common.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..backups import BackupError
from ..jobs import JobContext
from ..mcnet import PingError, StillStartingError, server_list_ping
from ..models import ServerRecord
from ..servers import ServerError
from ..updater import retry_delay_minutes
from ..util import iso
from . import gtnh
from .loaders import LoaderError
from .mrpack import PackDownloadError, PackError
from .net import BadDownloadError, DownloadError
from .service import JavaServerService, OpenedPack
from .software import SoftwareError

log = logging.getLogger(__name__)

PREVIOUS_FIELDS = ("minecraft", "loader", "java", "launch", "pack")  # what a rollback puts back besides the files
PACK_SOURCES = ("modrinth", "curseforge", gtnh.PROJECT)  # the places a server's modpack can be looked up again
# A modded server can close a status request for a while after it says "Done" (hundreds of mods still finishing their start, Forge filling
# in its status in pieces): the new version only counts as broken when it stays silent for this many tries.
PING_ATTEMPTS = 6
PING_PAUSE = 5.0
# Forge 1.7.10 answers "still starting" instead of its status until the last mod has finished (GT New Horizons: about half a minute after
# "Done", longer on a slow disk, and its quest book is loaded again after every update). That is a server that works, so it gets longer.
STARTING_PATIENCE = 600.0


def pending_of(rec: ServerRecord) -> dict[str, Any] | None:
    return (rec.spec.get("pack") or {}).get("pending")


class JavaUpdates:
    """The parts of updating a Java server that do not depend on what is updated: watching the new version run, putting everything
    back when it does not, and going back on request. A subclass says how its kind of version is named and what a rollback restores."""

    previous_fields: tuple[str, ...] = PREVIOUS_FIELDS
    counts_failures = False  # automatic updates wait longer after each failed attempt (software; a modpack is never updated by itself)
    nothing_earlier = "This server has no earlier version to go back to."

    def __init__(self, jvm: JavaServerService, stability_seconds: float = 15.0):
        self.jvm = jvm
        self.svc = jvm.svc
        self.stability_seconds = stability_seconds

    # ------------------------------------------------------------------------------------------ what differs
    def tag_of(self, spec: dict[str, Any]) -> str | None:
        """The short version a spec describes, as it is written in the update history."""
        raise NotImplementedError

    def name_of(self, spec: dict[str, Any]) -> str:
        """What a spec runs, for messages: 'Cool Pack 1.0.0', 'Paper 1.21.8 (build 60)'."""
        raise NotImplementedError

    def marker_of(self, spec: dict[str, Any]) -> str:
        """What names the version a spec runs in the 'do not offer it again' marker."""
        raise NotImplementedError

    def version_is_bad(self, exc: Exception) -> bool:
        """Does this failure mean the version itself is unusable (so offering it again is pointless)?"""
        return False

    # ------------------------------------------------------------------------------------------ watching
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
        last: PingError | None = None
        silent, starting_since = 0, None
        while True:
            try:
                await server_list_ping("127.0.0.1", self.svc.get(rec.id).java_port, timeout=5)
                return
            except StillStartingError as exc:  # it answers, and says that it is not done: patience, but not forever
                last, silent = exc, 0
                starting_since = loop.time() if starting_since is None else starting_since
                if loop.time() - starting_since > STARTING_PATIENCE:
                    raise ServerError(f"The updated server still says that it is starting, {int(STARTING_PATIENCE // 60)} minutes after it was ready.") from None
            except PingError as exc:
                last = exc
                silent += 1
                if silent >= PING_ATTEMPTS:
                    raise ServerError(f"The updated server no longer answers on its port ({last}).") from None
            rt = self.svc.pm.runtime(rec.id)
            if rt.state != "running":
                raise ServerError(f"The updated server stopped by itself ({rt.last_error or rt.state}).")
            await asyncio.sleep(PING_PAUSE)

    async def _handle_failure(
        self, rec: ServerRecord, old_spec: dict[str, Any], update_id: str, backup_row: dict[str, Any] | None,
        was_running: bool, switched: bool, marker: str, to_label: str, ctx: JobContext, exc: Exception,
    ) -> None:
        svc = self.svc
        known = (ServerError, BackupError, PackError, LoaderError, DownloadError, SoftwareError)
        reason = str(exc) if isinstance(exc, known) else f"Unexpected error: {exc}"
        if not isinstance(exc, known):
            log.exception("Update of %s failed", rec.id)
        ctx.step(f"Update failed: {reason}", "error")
        rt = svc.pm.runtime(rec.id)
        bad = self.version_is_bad(exc)
        if switched or bad:
            svc.repo.set_kv(f"skip:{rec.id}", marker)  # the version itself is the problem: do not offer it again
        if not switched:
            # Nothing was changed. Put the server back the way it was.
            svc.repo.update_update(update_id, status="failed", message=reason, finished_at=iso())
            if was_running and not rt.is_active:
                ctx.step("Restarting the server on the previous version")
                try:
                    await svc._do_start(svc.get(rec.id), ctx)
                except Exception as restart_exc:
                    ctx.step(f"The previous version could not be restarted: {restart_exc}", "error")
            retry_note = ""
            if self.counts_failures:
                if bad:
                    retry_note = " This version will not be tried again automatically; a newer one will be."
                else:
                    failures = int(svc.repo.get_kv(f"autofails:{rec.id}") or 0) + 1
                    svc.repo.set_kv(f"autofails:{rec.id}", str(failures))
                    if rec.policy.update_mode == "auto":
                        retry_note = f" The next automatic attempt is in about {retry_delay_minutes(failures)} minutes."
            svc.reporter.record(
                "error", "update", f"Update of '{rec.name}' to {to_label} was not applied: {reason}{retry_note}", rec.id,
                notify="update_failed", title=f"Update of {rec.name} failed",
            )
            svc.publish_state(rec.id)
            return

        # The new version was switched in but did not start properly: put everything back from the backup.
        ctx.step(f"Rolling back to {self.tag_of(old_spec) or 'the previous version'}", "warning")
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
                "warning", "update", f"Update of '{rec.name}' to {to_label} failed ({reason}); rolled back to the previous version",
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
            raise ServerError(self.nothing_earlier, 409)
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
        update_id = svc.repo.insert_update(
            server_id=rec.id, from_build_id=None, to_build_id=None, from_tag=self.tag_of(rec.spec), to_tag=self.tag_of(previous),
            trigger="rollback",
        )
        was_running = svc.pm.runtime(rec.id).is_active
        try:
            if was_running:
                await svc._do_stop(rec, ctx)
            ctx.step("Restoring the server from the backup made before the update (everything since then is lost)")
            await svc.backups.restore(rec, backup["id"], ctx, safety_backup=True, restart=False, reapply_config=lambda r: svc.ensure_config(r, ctx))
            fresh = svc.get(rec.id)
            restored = {k: v for k, v in fresh.spec.items() if k != "previous"}
            restored.update({k: previous[k] for k in self.previous_fields if previous.get(k) is not None})
            svc.repo.update_server(rec.id, spec=restored, pending_since=None)
            if was_running:
                await svc._do_start(svc.get(rec.id), ctx)
        except Exception as exc:
            svc.repo.update_update(update_id, status="failed", message=str(exc), finished_at=iso())
            raise
        svc.repo.update_update(update_id, status="success", message="Rolled back", finished_at=iso())
        svc.repo.set_kv(f"skip:{rec.id}", self.marker_of(rec.spec))
        svc.reporter.record(
            "warning", "update", f"'{rec.name}' rolled back to {self.name_of(previous)} (world and mods restored from the backup)", rec.id,
        )
        svc.publish_state(rec.id)


class PackUpdater(JavaUpdates):
    nothing_earlier = "This server has no earlier modpack version to go back to."

    # ------------------------------------------------------------------------------------------ naming
    def tag_of(self, spec: dict[str, Any]) -> str | None:
        return (spec.get("pack") or {}).get("version_number")

    def name_of(self, spec: dict[str, Any]) -> str:
        pack = spec.get("pack") or {}
        return f"{pack.get('title') or 'the modpack'} {pack.get('version_number')}"

    def marker_of(self, spec: dict[str, Any]) -> str:
        return (spec.get("pack") or {}).get("version_id") or ""

    def version_is_bad(self, exc: Exception) -> bool:
        return isinstance(exc, (PackError, BadDownloadError)) and not isinstance(exc, PackDownloadError)

    # ----------------------------------------------------------------- checking
    @staticmethod
    def updatable(rec: ServerRecord) -> bool:
        return rec.is_java and (rec.spec.get("pack") or {}).get("source") in PACK_SOURCES

    async def check(self, only: ServerRecord | None = None) -> dict[str, Any]:
        """Ask Modrinth, CurseForge and GT New Horizons' website whether the modpacks of the servers have a newer version and flag
        the servers to update."""
        svc = self.svc
        servers = [only] if only is not None else [r for r in svc.repo.list_servers() if self.updatable(r)]
        if only is not None and (only.spec.get("pack") or {}).get("source") == gtnh.PROJECT:
            self.jvm.gtnh.invalidate()  # somebody asked: do not answer from the list read an hour ago
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
                    pack = rec.spec.get("pack") or {}
                    versions[project] = await self.jvm.pack_versions(pack.get("source", ""), project, str(pack.get("title") or ""))
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
        newest = next((v for v in versions if v.get("type") in kinds), None)  # newest first
        position = {v["id"]: i for i, v in enumerate(versions)}
        if newest is not None and pack.get("version_id") in position:
            later = position[newest["id"]] < position[pack["version_id"]]  # listed above the installed one (two versions can share a day)
        else:
            later = newest is not None and (newest.get("published") or "") > (pack.get("published") or "")
        newer = newest is not None and newest["id"] != pack.get("version_id") and later and self.svc.repo.get_kv(f"skip:{rec.id}") != newest["id"]
        if not newer:
            self._store(rec, None)
            return False
        found = {k: newest[k] for k in ("id", "number", "type", "published", "minecraft", "loaders")}
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
        if pack.get("source") not in PACK_SOURCES:
            raise ServerError("Only modpacks from Modrinth, CurseForge or GT New Horizons have versions to choose from.", 409)
        try:
            versions = await self.jvm.pack_versions(pack["source"], pack["project"], str(pack.get("title") or ""))
        except DownloadError as exc:
            raise ServerError(str(exc)) from exc
        return {"current": pack.get("version_id"), "pending": pack.get("pending"), "versions": versions}

    # ----------------------------------------------------------------- applying
    def submit_update(self, rec: ServerRecord, version_id: str | None = None, *, trigger: str = "manual") -> dict[str, Any]:
        pack = rec.spec.get("pack") or {}
        if pack.get("source") not in PACK_SOURCES:
            raise ServerError("Only modpacks from Modrinth, CurseForge or GT New Horizons can be updated from here.", 409)
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
        backup_row: dict[str, Any] | None = None
        opened: OpenedPack | None = None
        switched = False
        to_tag = version_id
        try:
            # 1. Fetch everything while the server keeps running: a failure here costs no downtime.
            ctx.step("Looking up the new version")

            def announce(number: str) -> None:
                nonlocal to_tag
                to_tag = number
                svc.repo.update_update(update_id, to_tag=to_tag)
                ctx.step(f"Updating '{rec.name}': {title} {old_pack.get('version_number')} -> {to_tag}")

            opened = await jvm.open_version(rec, version_id, ctx, announce)
            index, pack_path = opened.index, opened.path
            missing = [f for f in index.manual if not (directory / f.path).is_file()]
            if missing:
                raise ServerError(
                    f"This version needs {len(missing)} mod(s) that have to be added by hand first, because their authors do not allow "
                    f"automatic downloads: {', '.join(f.name or f.path for f in missing[:6])}. Add them to the server's mods folder (the "
                    "Modpack tab links to each), then update again."
                )
            bundled = opened.info.get("source") == gtnh.PROJECT  # the server files bring their own loader
            new_launch = gtnh.launch_of(index.archive, opened.info["variant"]) if bundled else None  # fails here, before anything changes
            major = await jvm.java_for_pack(index, opened.info)
            java = await jvm.runtimes.ensure(major, jvm._java_progress(ctx, major))
            ctx.step("Downloading the new mods (the server keeps running meanwhile)")
            await jvm.packs.prefetch(index, lambda fraction, message: ctx.progress(0.1 + 0.4 * fraction, message))
            loader_changed = not bundled and (index.loader.kind, index.loader.version, index.minecraft) != (
                (old_spec.get("loader") or {}).get("kind"), (old_spec.get("loader") or {}).get("version"), old_spec.get("minecraft"),
            )
            if bundled and (warning := gtnh.jump_warning(str(old_pack.get("version_number") or ""), index.version_id)):
                ctx.warn(warning)
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
            used = {f.path for f in index.files}
            gone = [m["path"] for m in old_lock.get("manual") or [] if m.get("path") not in used and (directory / str(m.get("path"))).is_file()]
            if gone:  # mods that were added by hand for the old version: the manager never installed them, so it does not remove them either
                ctx.warn(
                    "These mods were added by hand for the old version, and the new version does not use them under that name. If they are "
                    "out of date, delete them from the mods folder: " + ", ".join(gone[:8])
                )
            if bundled and gtnh.SERVERUTILITIES_CONFIG in {*report["added"], *report["updated"], *report.get("replaced", [])}:
                if gtnh.switch_off_own_backups(directory):  # the new version of the file is the pack's, with its own backups on again
                    ctx.step("Switched off the backups of ServerUtilities again: the manager looks after the backups")
            if report.get("replaced"):
                ctx.warn(
                    f"{len(report['replaced'])} configuration and library file(s) that differed from what the old version shipped were "
                    "replaced by the new version's, as the pack's own update guide says (the mods rewrite many of their configuration files "
                    "themselves). The backup made before the update has the old files: " + ", ".join(report["replaced"][:6])
                    + ("…" if len(report["replaced"]) > 6 else "")
                )
            if report["kept_configs"]:
                ctx.warn(
                    f"{len(report['kept_configs'])} configuration file(s) you changed were kept; the modpack's new version of each is "
                    "saved next to it with the ending .pack-new: " + ", ".join(report["kept_configs"][:8])
                )
            launch = old_spec.get("launch")
            if bundled:
                launch = new_launch
            elif loader_changed:
                ctx.step(f"Installing {index.loader.label}")
                launch = await jvm.loaders.install(index.loader, directory, java.java, lambda message: ctx.progress(None, message))
            fresh = {k: v for k, v in old_pack.items() if k != "pending"}
            new_pack = {**fresh, **opened.info}
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
                if index.archive is not None:  # the author's own server files: what the new version removed from the world is meant to go
                    jvm.confirm_missing.add(rec.id)
                    jvm.prompts.pop(rec.id, None)
                try:
                    await svc._do_start(svc.get(rec.id), ctx)
                finally:
                    jvm.confirm_missing.discard(rec.id)
                if asked := jvm.prompts.pop(rec.id, None):
                    if asked["answered"]:
                        shown = ", ".join(asked["names"][:5]) + ("…" if asked["count"] and asked["count"] > 5 else "")
                        ctx.warn(
                            f"The new version removed {asked['count']} block(s) or item(s) that the world still had ({shown}): the server asked "
                            "for a confirmation at startup and the manager gave it, as the pack's own update guide does. Forge made a backup of the "
                            "world first, and the backup made before the update is the way back."
                        )
                await self._watch_stability(rec, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(rec, old_spec, update_id, backup_row, was_running, switched, version_id, to_tag, ctx, exc)
            return
        finally:
            if opened is not None:
                opened.cleanup()
                jvm.packs.cache.forget_staged(opened.index.stage)

        svc.repo.update_update(update_id, status="success", message="Updated", finished_at=iso())
        svc.repo.set_kv(f"skip:{rec.id}", "")
        ctx.step(f"Update complete: now running {title} {to_tag}")
        svc.reporter.record(
            "success", "update", f"'{rec.name}' updated to {title} {to_tag}", rec.id, notify="update_applied", title=f"{rec.name} updated",
        )
        svc.publish_state(rec.id)
