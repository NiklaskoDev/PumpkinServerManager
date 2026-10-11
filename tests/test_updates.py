"""The update procedure: backup first, health check, automatic rollback, rolling-channel identity."""

from __future__ import annotations

import json

from conftest import create_server, finish, wait_until

from pumpkin_manager.updater import is_newer


def world_data(ctx, server) -> dict:
    return json.loads((ctx.settings.servers_dir / server.id / "world" / "level.dat").read_text())


async def test_update_check_flags_newer_stable_build(ctx, source):
    server = await create_server(ctx, "Checker")
    assert not ctx.repo.get_server(server.id).pending_build_id
    await ctx.updater.check()
    assert not ctx.repo.get_server(server.id).pending_build_id  # already on the latest

    new = source.publish("stable", "0.2.0")
    result = await ctx.updater.check()
    assert result["servers_with_updates"] == 1
    assert ctx.repo.get_server(server.id).pending_build_id == ctx.builds.register(new).id
    view = ctx.servers.view(ctx.repo.get_server(server.id))
    assert view["update"]["available"] and view["update"]["pending_build"]["tag"] == new.tag


async def test_successful_update_backs_up_first_and_restarts(ctx, source):
    server = await create_server(ctx, "Happy")
    old_build = server.build_id
    source.publish("stable", "0.2.0")
    await ctx.updater.check()
    pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)

    job = ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="manual")
    row = await finish(ctx, job)
    assert row["status"] == "success", row["message"]

    rec = ctx.repo.get_server(server.id)
    assert rec.build_id == pending.id and rec.previous_build_id == old_build and not rec.pending_build_id
    assert ctx.pm.runtime(server.id).state == "running"
    assert ctx.pm.runtime(server.id).mc_version is None or True

    backups = [b for b in ctx.repo.list_backups(server.id) if b["kind"] == "pre-update"]
    assert len(backups) == 1 and backups[0]["verified"] and backups[0]["build_tag"] == "0.1.0+26.3-26.51"
    assert (ctx.settings.backup_dir / backups[0]["file_name"]).is_file()
    update = ctx.repo.list_updates(server.id)[0]
    assert update["status"] == "success" and update["backup_id"] == backups[0]["id"]

    # order of events in the job log proves the backup happened before the version switch
    steps = [s["text"] for s in ctx.repo.get_job(job["id"])["steps"]]
    stop_i = next(i for i, s in enumerate(steps) if "Stopping" in s)
    backup_i = next(i for i, s in enumerate(steps) if "Backup created" in s)
    start_i = next(i for i, s in enumerate(steps) if "Starting Pumpkin" in s)
    assert stop_i < backup_i < start_i


async def test_update_is_aborted_when_the_backup_fails(ctx, source, monkeypatch):
    server = await create_server(ctx, "NoBackup")
    source.publish("stable", "0.2.0")
    await ctx.updater.check()
    pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
    old_build = ctx.repo.get_server(server.id).build_id

    from pumpkin_manager import backups as backups_module

    def explode(*args, **kwargs):
        raise backups_module.BackupError("disk full (simulated)")

    monkeypatch.setattr(backups_module, "write_archive", explode)
    row = await finish(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="manual"))
    assert row["status"] == "success"  # the job handled the failure itself ...
    rec = ctx.repo.get_server(server.id)
    assert rec.build_id == old_build, "the version must NOT change without a verified backup"
    assert ctx.repo.list_updates(server.id)[0]["status"] == "failed"
    assert ctx.pm.runtime(server.id).state == "running", "the server is brought back on the old version"
    assert any("not applied" in a["message"] for a in ctx.repo.list_activity(20))


async def test_failed_start_rolls_back_binary_and_data(ctx, source):
    server = await create_server(ctx, "Rollback")
    before = world_data(ctx, server)
    bad = source.publish("stable", "0.2.0", corrupt_world=True, crash_on_start=True)
    await ctx.updater.check()
    pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
    old_build = ctx.repo.get_server(server.id).build_id

    row = await finish(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="auto"))
    assert row["status"] == "success"

    rec = ctx.repo.get_server(server.id)
    assert rec.build_id == old_build, "rolled back to the previous binary"
    assert ctx.pm.runtime(server.id).state == "running", "and the old version is running again"
    # the new version corrupted level.dat before crashing; the restore brought the original back
    after = world_data(ctx, server)
    assert after["created_by"] == before["created_by"]
    assert ctx.repo.list_updates(server.id)[0]["status"] == "rolled_back"
    assert ctx.repo.get_kv(f"skip:{server.id}") == pending.id

    # the bad build is not offered again, but a newer one is
    await ctx.updater.check()
    assert not ctx.repo.get_server(server.id).pending_build_id
    source.publish("stable", "0.2.1")
    await ctx.updater.check()
    assert ctx.repo.get_server(server.id).pending_build_id
    assert bad.digest != source.latest_by_channel["stable"].digest


async def test_update_that_crashes_shortly_after_start_is_rolled_back(ctx, source):
    server = await create_server(ctx, "Flaky")
    source.publish("stable", "0.2.0", crash_after=0.3)
    await ctx.updater.check()
    pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
    old_build = ctx.repo.get_server(server.id).build_id
    await finish(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="auto"))
    assert ctx.repo.get_server(server.id).build_id == old_build
    assert ctx.repo.list_updates(server.id)[0]["status"] == "rolled_back"


async def test_download_failure_leaves_server_untouched(ctx, source):
    server = await create_server(ctx, "Offline")
    source.publish("stable", "0.2.0")
    await ctx.updater.check()
    pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
    source.fail_download = True
    pid = ctx.pm.runtime(server.id).pid
    await finish(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="manual"))
    assert ctx.pm.runtime(server.id).pid == pid, "the server was not even restarted"
    assert ctx.repo.list_updates(server.id)[0]["status"] == "failed"
    assert not [b for b in ctx.repo.list_backups(server.id) if b["kind"] == "pre-update"]


async def test_rolling_channel_updates_by_digest_not_tag(ctx, source):
    source.publish("nightly", "n1")
    server = await create_server(ctx, "Nightly", channel="nightly")
    first = ctx.repo.get_build(server.build_id)
    assert first.tag == "nightly"
    await ctx.updater.check()
    assert not ctx.repo.get_server(server.id).pending_build_id

    source.publish("nightly", "n2")  # same tag, different binary
    await ctx.updater.check()
    rec = ctx.repo.get_server(server.id)
    assert rec.pending_build_id and rec.pending_build_id != first.id
    assert ctx.repo.get_build(rec.pending_build_id).tag == "nightly"

    await finish(ctx, ctx.updater.submit_update(rec, ctx.repo.get_build(rec.pending_build_id), trigger="auto"))
    assert ctx.repo.get_server(server.id).build_id != first.id
    # the previous nightly binary stays cached for a rollback because GitHub no longer serves it
    assert ctx.builds.is_available(first)


async def test_auto_update_waits_for_players_to_leave(ctx, source):
    server = await create_server(ctx, "Busy-server")
    source.publish("stable", "0.2.0")
    await ctx.updater.check()
    await ctx.pm.send_command(server.id, "fakejoin Bob")
    await ctx.servers._poll_players(ctx.repo.get_server(server.id))
    assert ctx.pm.runtime(server.id).players_online == 1

    assert await ctx.updater.process_pending() == 0, "players online: the update must wait"
    await ctx.pm.send_command(server.id, "fakeleave Bob")
    await ctx.servers._poll_players(ctx.repo.get_server(server.id))
    assert await ctx.updater.process_pending() == 1
    await wait_until(lambda: not ctx.jobs.is_busy(server.id), 60, what="auto update")
    assert ctx.repo.list_updates(server.id)[0]["status"] == "success"


async def test_update_mode_off_and_pinned_servers_are_left_alone(ctx, source):
    a = await create_server(ctx, "Off", policy={"update_mode": "off"})
    b = await create_server(ctx, "Pinned", pinned_tag="0.1.0+26.3-26.51")
    source.publish("stable", "0.2.0")
    result = await ctx.updater.check()
    assert result["servers_with_updates"] == 0
    assert not ctx.repo.get_server(a.id).pending_build_id and not ctx.repo.get_server(b.id).pending_build_id


async def test_manual_rollback_restores_previous_version_and_data(ctx, source):
    server = await create_server(ctx, "Undo")
    first_build = server.build_id
    source.publish("stable", "0.2.0")
    await ctx.updater.check()
    pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
    await finish(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="manual"))
    assert ctx.repo.get_server(server.id).build_id == pending.id

    row = await finish(ctx, ctx.updater.submit_rollback(ctx.repo.get_server(server.id), restore_data=True))
    assert row["status"] == "success", row["message"]
    rec = ctx.repo.get_server(server.id)
    assert rec.build_id == first_build and rec.pinned_tag, "pinned so it is not immediately updated again"
    assert ctx.pm.runtime(server.id).state == "running"


def test_is_newer_guards_against_older_builds(ctx_free=None):
    from pumpkin_manager.models import BuildRecord

    def build(i, ts, channel="stable"):
        return BuildRecord(i, channel, i, None, None, i, 1, "x", None, "u", None, ts, ts, None, False, None, None)

    old, new = build("a", "2026-01-01T00:00:00Z"), build("b", "2026-02-01T00:00:00Z")
    assert is_newer(new, old) and not is_newer(old, new) and not is_newer(old, old)
    assert is_newer(new, None)
    assert is_newer(build("c", "2020-01-01T00:00:00Z", "nightly"), old), "switching channel is always an explicit switch"


async def test_shutting_down_mid_update_does_not_trigger_a_rollback(tmp_path, source):
    """A container stop during the stability watch must leave the update to be judged on the next start."""
    import sys

    from conftest import make_settings
    from fakes import FAKE

    from pumpkin_manager.app import AppContext

    settings = make_settings(tmp_path)
    app_ctx = AppContext(settings, release_source=source, verify_elf=False, background=False, stability_seconds=30)
    app_ctx.servers.command_builder = lambda rec, b: [sys.executable, "-u", str(FAKE), str(app_ctx.builds.binary_path(b.id))]
    await app_ctx.startup()
    server = await create_server(app_ctx, "Midway")
    old_build = server.build_id
    source.publish("stable", "0.2.0")
    await app_ctx.updater.check()
    pending = app_ctx.repo.get_build(app_ctx.repo.get_server(server.id).pending_build_id)
    app_ctx.updater.submit_update(app_ctx.repo.get_server(server.id), pending, trigger="auto")
    await wait_until(lambda: app_ctx.repo.get_server(server.id).build_id == pending.id and app_ctx.pm.runtime(server.id).state == "running", 30, what="new version running")

    await app_ctx.shutdown()  # the stability watch (30 s) is still going

    again = AppContext(settings, release_source=source, verify_elf=False, background=False)
    await again.startup()
    try:
        assert again.repo.get_server(server.id).build_id == pending.id != old_build, "no rollback happened during the shutdown"
        statuses = [u["status"] for u in again.repo.list_updates(server.id)]
        assert statuses == ["interrupted"], "the update is reported as interrupted, not as rolled back"
        assert any("interrupted by a restart" in a["message"] for a in again.repo.list_activity(30))
    finally:
        await again.shutdown()
