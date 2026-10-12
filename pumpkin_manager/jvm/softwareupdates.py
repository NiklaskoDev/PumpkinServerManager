"""Updating a server that runs Vanilla, Paper or Fabric: finding newer builds, switching to another version, going back.

The order is the same as for modpacks and Pumpkin: fetch everything while the server keeps running, stop it, make a verified backup,
switch the files, start it and watch it, and if it does not come up restore the backup.

Two kinds of news are told apart. A newer *build* of the Minecraft version the server runs (a Paper build, a Fabric loader) is a small
step that plugins and mods are made for, so it can be installed automatically. A newer *Minecraft version* converts the world and
breaks plugins and mods until their makers catch up, so it is only ever offered, never installed by itself. Vanilla has no builds: a
newer release is its update, and it too waits for a person.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..jobs import JobContext
from ..models import ServerRecord, software_label, software_tag
from ..servers import ServerError
from ..updater import retry_delay_minutes
from ..util import iso, parse_iso, utcnow
from .mojang import available_java
from .net import DownloadError
from .packupdates import JavaUpdates
from .service import JavaServerService
from .software import Build, SoftwareCatalog, SoftwareError, SoftwareInstaller, is_release, version_key

log = logging.getLogger(__name__)

PREVIOUS_FIELDS = ("minecraft", "java", "launch", "software")  # what a rollback puts back besides the files
VOLATILE = ("pending", "upgrade")  # what a check found: never part of what a server "was"


class DowngradeNotConfirmed(ServerError):
    status = 409
    code = "downgrade"


def software_of(rec: ServerRecord) -> dict[str, Any] | None:
    software = rec.spec.get("software") if rec.is_java else None
    return software if isinstance(software, dict) and software.get("kind") else None


def stable_part(software: dict[str, Any]) -> dict[str, Any]:
    """The software record without the news a check attached to it."""
    return {k: v for k, v in software.items() if k not in VOLATILE}


def sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SoftwareUpdater(JavaUpdates):
    previous_fields = PREVIOUS_FIELDS
    counts_failures = True
    nothing_earlier = "This server has no earlier version to go back to."

    def __init__(self, jvm: JavaServerService, stability_seconds: float = 15.0):
        super().__init__(jvm, stability_seconds)
        self.catalog: SoftwareCatalog = jvm.software
        self.installer: SoftwareInstaller = jvm.installer

    # ------------------------------------------------------------------------------------------ naming
    def tag_of(self, spec: dict[str, Any]) -> str | None:
        return software_tag(spec.get("software") or {})

    def name_of(self, spec: dict[str, Any]) -> str:
        return software_label(spec.get("software") or {})

    def marker_of(self, spec: dict[str, Any]) -> str:
        software = spec.get("software") or {}
        return f"{software.get('kind')}:{software.get('minecraft')}:{software.get('build') or ''}"

    def version_is_bad(self, exc: Exception) -> bool:
        from .net import BadDownloadError

        return isinstance(exc, BadDownloadError)

    @staticmethod
    def managed(rec: ServerRecord) -> bool:
        return software_of(rec) is not None

    def _software(self, rec: ServerRecord) -> dict[str, Any]:
        software = software_of(rec)
        if software is None:
            raise ServerError("This server does not run Vanilla, Paper or Fabric that the manager installed.", 404)
        return software

    # ----------------------------------------------------------------------------------------------- checking
    async def check(self, only: ServerRecord | None = None) -> dict[str, Any]:
        """Ask Mojang, PaperMC and Fabric whether the servers have a newer build or Minecraft version and flag them."""
        svc = self.svc
        self.catalog.invalidate()  # a check looks at what is there now
        servers = [only] if only is not None else [r for r in svc.repo.list_servers() if self.managed(r)]
        errors: dict[str, str] = {}
        flagged = 0
        for rec in servers:
            if only is None and (rec.policy.update_mode == "off" or svc.jobs.is_busy(rec.id)):
                if rec.policy.update_mode == "off":
                    self._store(rec, None, None, None)
                continue
            try:
                pending, upgrade, support = await self._look_up(rec)
            except (SoftwareError, DownloadError) as exc:
                errors[rec.id] = str(exc)
                continue
            if self._evaluate(rec, pending, upgrade, support):
                flagged += 1
        return {"servers_with_updates": flagged, "errors": errors}

    async def _look_up(self, rec: ServerRecord) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """(pending build or Minecraft version for Vanilla, newer Minecraft version, what the maker says about the current one)."""
        software = self._software(rec)
        kind, mc = software["kind"], software["minecraft"]
        skipped = self.svc.repo.get_kv(f"skip:{rec.id}")
        snapshots = not is_release(mc)  # a server on a snapshot is shown newer snapshots as well
        pending: dict[str, Any] | None = None
        upgrade: dict[str, Any] | None = None
        support: dict[str, Any] | None = None
        if kind == "vanilla":
            newer = await self.catalog.newer_minecraft("vanilla", mc, snapshots=snapshots)
            if newer is not None:
                candidate = Build("vanilla", newer["id"], channel=newer["type"], published=newer.get("published"))
                if candidate.identity != skipped:
                    pending = self._described(candidate, "minecraft")
            return pending, None, None
        options = await self.catalog.options(kind, mc)
        support = options.support
        newest = options.stable_builds()[0] if options.stable_builds() else None
        current = str(software.get("build") or "")
        if newest is not None and self._build_is_newer(kind, newest.build, current) and newest.identity != skipped:
            pending = self._described(newest, "build")
        newer = await self.catalog.newer_minecraft(kind, mc, snapshots=snapshots)
        if newer is not None:
            upgrade = {"minecraft": newer["id"], "published": newer.get("published"), "label": f"Minecraft {newer['id']}"}
        return pending, upgrade, support

    @staticmethod
    def _build_is_newer(kind: str, candidate: str, current: str) -> bool:
        if not current:
            return True
        if kind == "paper":
            return candidate.isdigit() and current.isdigit() and int(candidate) > int(current)
        return version_key(candidate) > version_key(current)

    @staticmethod
    def _described(build: Build, kind: str) -> dict[str, Any]:
        return {
            "kind": kind, "minecraft": build.minecraft, "build": build.build, "label": build.label, "channel": build.channel,
            "published": build.published,
        }

    def _evaluate(
        self, rec: ServerRecord, pending: dict[str, Any] | None, upgrade: dict[str, Any] | None, support: dict[str, Any] | None
    ) -> bool:
        """Set or clear what is pending for one server. Returns True if an update is."""
        known = (software_of(rec) or {}).get("pending")
        if pending is not None and (known or {}).get("label") != pending["label"]:
            self.svc.reporter.record(
                "info", "update", f"Update available for '{rec.name}': {pending['label']}", rec.id,
                notify="update_available" if rec.policy.update_mode == "notify" else None, title=f"Update available for {rec.name}",
            )
        self._store(rec, pending, upgrade, support)
        return pending is not None

    def _store(
        self, rec: ServerRecord, pending: dict[str, Any] | None, upgrade: dict[str, Any] | None, support: dict[str, Any] | None
    ) -> None:
        rec = self.svc.get(rec.id)
        spec = dict(rec.spec)
        software = dict(spec.get("software") or {})
        before = (software.get("pending"), software.get("upgrade"), software.get("support"))
        for key, value in (("pending", pending), ("upgrade", upgrade)):
            if value is None:
                software.pop(key, None)
            else:
                software[key] = value
        if support is not None:
            software["support"] = support
        spec["software"] = software
        self.svc.repo.update_server(
            rec.id, spec=spec, last_update_check_at=iso(), pending_since=(rec.pending_since or iso()) if pending else None,
        )
        if before != (software.get("pending"), software.get("upgrade"), software.get("support")):
            self.svc.publish_state(rec.id)

    # ----------------------------------------------------------------------------- choosing what to install
    async def prepare(
        self, rec: ServerRecord, minecraft: str | None, build: str | None, *, confirm_downgrade: bool = False
    ) -> Build:
        """Turn a request for a version into the build to install, or say why that cannot be done. Nothing is changed."""
        software = self._software(rec)
        kind, current_mc = software["kind"], software["minecraft"]
        if not minecraft and not build:
            pending = software.get("pending")
            if not pending:
                raise ServerError("There is no newer version to install. Check for updates first.", 409)
            minecraft, build = pending["minecraft"], pending.get("build") or None
        resolved, _ = await self.catalog.resolve(kind, minecraft or current_mc, build)
        if resolved.minecraft == current_mc and resolved.build == str(software.get("build") or ""):
            raise ServerError("The server already runs that version.", 409)
        if resolved.minecraft != current_mc and not await self.catalog.is_newer(resolved.minecraft, current_mc) and not confirm_downgrade:
            raise DowngradeNotConfirmed(
                f"Minecraft {resolved.minecraft} is older than the {current_mc} this server runs. A world that was opened by a newer "
                "version usually cannot be opened by an older one. The backup that is made first is the way back; confirm to go ahead."
            )
        return resolved

    # ----------------------------------------------------------------------------------------------- applying
    def submit_update(self, rec: ServerRecord, build: Build, *, trigger: str = "manual", announce_seconds: int = 30) -> dict[str, Any]:
        return self.svc.jobs.submit(
            "update", f"Update {rec.name} to {build.label}", rec.id, lambda ctx: self.apply(rec, build, ctx, trigger, announce_seconds),
        )

    async def apply(self, rec: ServerRecord, build: Build, ctx: JobContext, trigger: str = "manual", announce_seconds: int = 30) -> None:
        svc, jvm = self.svc, self.jvm
        rec = svc.get(rec.id)
        old_spec = rec.spec
        old = stable_part(self._software(rec))
        directory = svc.server_dir(rec.id)
        rt = svc.pm.runtime(rec.id)
        was_running = rt.state in ("starting", "running")
        old_label = software_label(old)
        update_id = svc.repo.insert_update(
            server_id=rec.id, from_build_id=None, to_build_id=None, from_tag=self.tag_of(old_spec), to_tag=None, trigger=trigger,
        )
        staging = svc.settings.servers_dir / f".update-{rec.id}-{uuid.uuid4().hex[:8]}"
        backup_row: dict[str, Any] | None = None
        switched = False
        try:
            # 1. Fetch everything while the server keeps running: a failure here costs no downtime.
            ctx.step(f"Updating '{rec.name}': {old_label} -> {build.label}")
            svc.repo.update_update(update_id, to_tag=software_tag({"kind": build.kind, "minecraft": build.minecraft, "build": build.build}))
            if build.kind != old["kind"]:
                raise ServerError("The software of a server cannot be changed by an update. Create a new server instead.")
            options = await self.catalog.options(build.kind, build.minecraft)
            major = available_java(max(await jvm.mojang.java_major(build.minecraft), options.java_minimum or 0))
            java = await jvm.runtimes.ensure(major, jvm._java_progress(ctx, major))
            staging.mkdir(parents=True)
            ctx.step(f"Downloading {build.label} (the server keeps running meanwhile)")
            launch = await self.installer.fetch(
                build, staging, java.java, lambda text: ctx.progress(None, text),
                lambda done, total: ctx.progress(0.1 + 0.4 * (done / total if total else 0)),
            )
            minecraft_changed = build.minecraft != old["minecraft"]
            api_plan = await self._plan_fabric_api(old, build, directory, staging, minecraft_changed, ctx) if build.kind == "fabric" else None
            if minecraft_changed:
                ctx.warn(
                    f"This update changes Minecraft from {old['minecraft']} to {build.minecraft}. The world is converted when the server "
                    "starts and cannot be opened by the old version again; the backup made now is the way back."
                    + (" Plugins and mods have to support the new version as well." if build.kind != "vanilla" else "")
                )
            ctx.progress(0.5)

            # 2. Tell players what is about to happen, then stop gracefully so the world is saved.
            if rt.state == "running" and announce_seconds > 0 and rt.players_online > 0:
                ctx.step(f"Warning {rt.players_online} player(s): restarting in {announce_seconds}s for an update")
                await svc.pm.send_command(rec.id, f"say The server is being updated and restarts in {announce_seconds} seconds.", echo=False)
                await asyncio.sleep(announce_seconds)
            if rt.is_active:
                await svc._do_stop(rec, ctx)
            ctx.progress(0.55)

            # 3. Mandatory backup. Without a verified backup nothing is changed.
            ctx.step("Creating the backup that protects this update")
            backup_row = await svc.backups.create(
                svc.get(rec.id), "pre-update", ctx, mode="cold", note=f"Before updating to {build.label}", build_tag=old_label,
            )
            svc.repo.update_update(update_id, backup_id=backup_row["id"])
            ctx.progress(0.7)

            # 4. Switch the files.
            switched = True
            ctx.step(f"Installing {build.label}")
            await asyncio.to_thread(os.replace, staging / launch["jar"], directory / launch["jar"])
            fabric_api = await self._switch_fabric_api(old, api_plan, directory, staging, ctx) if build.kind == "fabric" else None
            new = {
                **old, "minecraft": build.minecraft, "build": build.build, "channel": build.channel, "installed_at": iso(),
                "support": options.support,
            }
            new.pop("fabric_api", None)
            if fabric_api:
                new["fabric_api"] = fabric_api
            previous = {k: old_spec.get(k) for k in PREVIOUS_FIELDS}
            previous["software"] = old
            svc.repo.update_server(
                rec.id, pending_since=None, spec={**old_spec, "minecraft": build.minecraft, "java": major, "launch": launch, "software": new, "previous": previous},
            )
            svc.publish_state(rec.id)

            # 5. Bring it back up and make sure it stays up.
            if was_running:
                ctx.step("Starting the updated server")
                await svc._do_start(svc.get(rec.id), ctx)
                await self._watch_stability(rec, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(rec, old_spec, update_id, backup_row, was_running, switched, build.identity, build.label, ctx, exc)
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        svc.repo.update_update(update_id, status="success", message="Updated", finished_at=iso())
        svc.repo.set_kv(f"skip:{rec.id}", "")
        svc.repo.set_kv(f"autofails:{rec.id}", "0")
        ctx.step(f"Update complete: now running {build.label}")
        svc.reporter.record(
            "success", "update", f"'{rec.name}' updated to {build.label}", rec.id, notify="update_applied", title=f"{rec.name} updated",
        )
        svc.publish_state(rec.id)

    # ---------------------------------------------------------------------------------------------- Fabric API
    async def _plan_fabric_api(
        self, old: dict[str, Any], build: Build, directory: Path, staging: Path, minecraft_changed: bool, ctx: JobContext
    ) -> dict[str, Any] | None:
        """Fabric API belongs to one Minecraft version. When the server has the copy the manager installed and the version changes,
        fetch the new one now (the server keeps running); a copy somebody replaced by hand is left alone."""
        kept = old.get("fabric_api")
        if not kept or not minecraft_changed:
            return {"keep": kept} if kept else None
        path = directory / str(kept.get("file", "-"))
        if not path.is_file() or (kept.get("sha1") and await asyncio.to_thread(sha1_of, path) != kept["sha1"]):
            ctx.warn(
                f"The Fabric API in the mods folder is not the file the manager installed (it was changed by hand), so it is left alone. "
                f"Make sure it supports Minecraft {build.minecraft}."
            )
            return {"drop": True}
        api = await self.installer.fabric_api(build.minecraft)
        if api is None:
            raise ServerError(
                f"Fabric API has no version for Minecraft {build.minecraft} yet, and the mods of this server need it. Wait until it is "
                "published, or choose another Minecraft version."
            )
        ctx.step(f"Downloading Fabric API {api.number}")
        record = await self.installer.install_fabric_api(api, staging, lambda done, total: ctx.progress(0.45 + 0.05 * (done / total if total else 0)))
        return {"replace": kept, "record": record}

    async def _switch_fabric_api(
        self, old: dict[str, Any], plan: dict[str, Any] | None, directory: Path, staging: Path, ctx: JobContext
    ) -> dict[str, Any] | None:
        if plan is None or plan.get("drop"):
            return None
        if "keep" in plan:
            return plan["keep"]  # the same Minecraft version: Fabric API stays as it is
        old_file = directory / str(plan["replace"]["file"])
        new_file = directory / plan["record"]["file"]
        old_file.unlink(missing_ok=True)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(os.replace, staging / plan["record"]["file"], new_file)
        ctx.step(f"Fabric API is now {plan['record']['number']}")
        return plan["record"]

    # ------------------------------------------------------------------------------------ automatic updates
    async def process_pending(self) -> int:
        """Start the automatic updates that are due: new builds for servers that are set to install them. Called every minute."""
        started = 0
        now = utcnow()
        svc = self.svc
        for rec in svc.repo.list_servers():
            software = software_of(rec)
            pending = (software or {}).get("pending")
            if not software or software["kind"] == "vanilla" or not pending or pending.get("kind") != "build":
                continue  # a new Minecraft version is never installed by itself
            if rec.policy.update_mode != "auto" or svc.jobs.is_busy(rec.id):
                continue
            attempt = svc.repo.get_kv(f"autoattempt:{rec.id}")
            failures = int(svc.repo.get_kv(f"autofails:{rec.id}") or 0)
            if attempt and (now - parse_iso(attempt)).total_seconds() < retry_delay_minutes(failures) * 60:
                continue  # a recent attempt failed: wait, with a delay that grows while the trouble persists
            rt = svc.pm.runtime(rec.id)
            players = rt.players_online if rt.state == "running" else 0
            waited_hours = (now - parse_iso(rec.pending_since)).total_seconds() / 3600 if rec.pending_since else 0
            if players > 0 and rec.policy.update_only_when_empty and waited_hours < rec.policy.update_max_defer_hours:
                continue
            svc.repo.set_kv(f"autoattempt:{rec.id}", iso())
            try:
                build = await self.prepare(rec, pending["minecraft"], pending.get("build") or None)
                self.submit_update(rec, build, trigger="auto", announce_seconds=60 if players > 0 else 0)
                started += 1
            except Exception as exc:
                log.warning("Could not start the automatic update of %s: %s", rec.id, exc)
        return started

    # ---------------------------------------------------------------------------------------------- picking
    async def versions(self, rec: ServerRecord, snapshots: bool = False) -> dict[str, Any]:
        """The Minecraft versions this server's software can run, with the one it runs marked."""
        software = self._software(rec)
        listed = await self.catalog.versions(software["kind"], snapshots=snapshots or not is_release(software["minecraft"]))
        return {"kind": software["kind"], "current": software["minecraft"], "versions": listed}

    async def builds(self, rec: ServerRecord, minecraft: str, experimental: bool = False) -> dict[str, Any]:
        software = self._software(rec)
        options = await self.catalog.options(software["kind"], minecraft)
        return {**options.public(experimental), "current_build": software.get("build") or "", "current": software["minecraft"]}


__all__ = ["SoftwareUpdater", "DowngradeNotConfirmed", "software_of"]
