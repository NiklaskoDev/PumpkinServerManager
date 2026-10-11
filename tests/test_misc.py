"""Restart persistence, config safety at start, port pool, schedulers, crash back-off and the CLI."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import create_server, finish, make_settings, wait_until

from pumpkin_manager.__main__ import main
from pumpkin_manager.app import AppContext
from pumpkin_manager.config import Settings
from pumpkin_manager.security import AuthError
from pumpkin_manager.util import iso, utcnow


async def test_update_knowledge_survives_a_restart(tmp_path, source):
    settings = make_settings(tmp_path)
    first = AppContext(settings, release_source=source, verify_elf=False, background=False)
    await first.startup()
    source.publish("nightly", "n1")
    await first.updater.check()
    checked_at = first.updater.last_check
    await first.shutdown()

    second = AppContext(settings, release_source=source, verify_elf=False, background=False)
    await second.startup()
    try:
        assert second.updater.last_check == checked_at
        assert set(second.updater.latest) == {"stable", "nightly"}
        assert second.updater.latest["nightly"].tag == "nightly"
    finally:
        await second.shutdown()


async def test_servers_that_were_running_come_back_after_a_restart(tmp_path, source):
    import sys

    from fakes import FAKE

    settings = make_settings(tmp_path)

    def build(app_ctx):
        app_ctx.servers.command_builder = lambda rec, b: [sys.executable, "-u", str(FAKE), str(app_ctx.builds.binary_path(b.id))]

    first = AppContext(settings, release_source=source, verify_elf=False, background=False)
    build(first)
    await first.startup()
    running = await create_server(first, "Returns")
    stopped = await create_server(first, "Stays-down")
    await finish(first, first.servers.submit_stop(first.repo.get_server(stopped.id)))
    await first.shutdown()  # like a container stop: every server is stopped, but "desired state" is remembered
    assert first.pm.runtime(running.id).state == "stopped"

    second = AppContext(settings, release_source=source, verify_elf=False, background=False)
    build(second)
    await second.startup()
    try:
        assert [r.id for r in second.servers.autostart_targets()] == [running.id]
        second.servers.start_autostart()
        await wait_until(lambda: second.pm.runtime(running.id).state == "running", 30, what="autostart")
        assert second.pm.runtime(stopped.id).state == "stopped"
    finally:
        await second.shutdown()


async def test_a_broken_config_file_blocks_the_start_with_a_clear_message(ctx):
    server = await create_server(ctx, "Typo", start=False)
    (ctx.settings.servers_dir / server.id / "pumpkin.toml").write_text("max_players = = 5\n")
    row = await finish(ctx, ctx.servers.submit_start(server))
    assert row["status"] == "failed"
    assert "not valid TOML" in row["message"] and "default settings" in row["message"]
    assert ctx.pm.runtime(server.id).state != "running"


async def test_port_pool_exhaustion_is_explained(ctx):
    low, high = ctx.settings.port_range
    for i in range(high - low + 1):
        await create_server(ctx, f"S{i}", start=False)
    job = ctx.servers.submit_create({"name": "One too many", "start": False})
    row = await finish(ctx, job)
    assert row["status"] == "failed"
    assert "No free port" in row["message"] and "PORT_RANGE" in row["message"]
    assert not [s for s in ctx.repo.list_servers() if s.name == "One too many"]


async def test_manual_port_is_validated(ctx):
    a = await create_server(ctx, "First", start=False)
    row = await finish(ctx, ctx.servers.submit_create({"name": "Second", "java_port": a.java_port, "start": False}))
    assert row["status"] == "failed" and "already assigned" in row["message"]
    row = await finish(ctx, ctx.servers.submit_create({"name": "Third", "java_port": 80, "start": False}))
    assert row["status"] == "failed" and "1024" in row["message"]


async def test_scheduler_runs_due_backups_only(ctx):
    due = await create_server(ctx, "Due", start=False)
    fresh = await create_server(ctx, "Fresh", start=False)
    off = await create_server(ctx, "Off", start=False, policy={"backup_schedule": {"type": "off"}})
    old = iso(utcnow() - timedelta(days=3))
    ctx.repo.update_server(due.id, last_backup_at=old)
    ctx.repo.update_server(off.id, last_backup_at=old)

    await ctx.scheduler._scheduled_backups()
    await wait_until(lambda: not ctx.jobs.is_busy(due.id), 30, what="scheduled backup")
    assert [b["kind"] for b in ctx.repo.list_backups(due.id)] == ["scheduled"]
    assert not ctx.repo.list_backups(fresh.id) and not ctx.repo.list_backups(off.id)
    assert ctx.repo.get_server(due.id).last_backup_at > old


async def test_crash_restarts_back_off_and_eventually_pause(ctx, monkeypatch):
    server = await create_server(ctx, "Loop", start=False)
    ctx.repo.update_server(server.id, desired_running=1)
    delays: list[float] = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_later", lambda delay, fn, *args: delays.append(delay))
    for _ in range(5):
        await ctx.servers._on_exit(server.id, 101, True)
    assert delays == [5, 15, 45], "three restarts with growing delays, then the manager gives up"
    messages = [a["message"] for a in ctx.repo.list_activity(30)]
    assert any("automatic restarts are paused" in m for m in messages)


async def test_crash_policy_respects_user_stops_and_disabled_restart(ctx, monkeypatch):
    server = await create_server(ctx, "Quiet", start=False, policy={"restart_on_crash": False})
    ctx.repo.update_server(server.id, desired_running=1)
    delays: list[float] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda delay, fn, *args: delays.append(delay))
    await ctx.servers._on_exit(server.id, 1, True)
    await ctx.servers._on_exit(server.id, 0, False)  # a requested stop is not a crash
    assert delays == []


def test_cli_reset_password(tmp_path, monkeypatch, capsys):
    import asyncio as aio

    settings = make_settings(tmp_path)

    async def first_boot():
        app_ctx = AppContext(settings, verify_elf=False, background=False, release_source=None)
        await app_ctx.startup()
        await app_ctx.shutdown()

    aio.run(first_boot())
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("ADMIN_USER", "admin")
    assert main(["reset-password", "--password", "brand-new-pass-1"]) == 0
    assert "has been reset" in capsys.readouterr().out
    assert main(["reset-password", "--password", "short"]) == 1
    assert main(["reset-password", "--user", "ghost", "--password", "long-enough-pw"]) == 1

    async def check():
        app_ctx = AppContext(settings, verify_elf=False, background=False, release_source=None)
        try:
            await app_ctx.auth.login("admin", "brand-new-pass-1", "cli", None)
            with pytest.raises(AuthError):
                await app_ctx.auth.login("admin", "correct horse battery", "cli", None)
        finally:
            await app_ctx.shutdown()

    aio.run(check())


def test_settings_from_env_validation():
    env = {"DATA_DIR": "/x", "PORT_RANGE": "25570-25580", "BEDROCK_PORT_RANGE": "19132", "MAX_UPLOAD_MB": "10", "ENABLE_API_DOCS": "yes"}
    s = Settings.from_env(env)
    assert s.port_range == (25570, 25580) and s.bedrock_port_range == (19132, 19132)
    assert s.max_upload_bytes == 10 * 1024 * 1024 and s.api_docs and str(s.backup_dir).replace("\\", "/") == "/x/backups"
    for bad in ("25580-25570", "abc", "0-10", "1-70000"):
        with pytest.raises(ValueError):
            Settings.from_env({"PORT_RANGE": bad})
    assert Settings.from_env({"BACKUP_DIR": "/mnt/backups"}).backup_dir.as_posix() == "/mnt/backups"


def test_overlapping_port_pools_are_rejected():
    with pytest.raises(ValueError, match="overlap"):
        Settings.from_env({"PORT_RANGE": "25565-25575", "MGMT_PORT_RANGE": "25570-25600"})
    Settings.from_env({"PORT_RANGE": "25565-25575", "MGMT_PORT_RANGE": "25576-25600"})


async def test_java_and_management_ports_never_collide(ctx):
    """Even when a management port number lies inside the Java pool, the two kinds share one namespace."""
    from pumpkin_manager.servers import ServerError

    server = await create_server(ctx, "Taken", start=False)
    with pytest.raises(ServerError, match="already assigned"):
        ctx.servers.allocate_port("mgmt", server.java_port)
    with pytest.raises(ServerError, match="already assigned"):
        ctx.servers.allocate_port("java", server.mgmt_port)


async def test_bedrock_servers_get_two_ports_each(ctx):
    """Pumpkin's Bedrock side also binds port+1, so neighbours need a gap (verified against the real server)."""
    from pumpkin_manager.servers import ServerError

    low, high = ctx.settings.bedrock_port_range
    ports = []
    for i in range(3):
        ports.append((await create_server(ctx, f"B{i}", start=False, bedrock=True)).bedrock_port)
    assert ports == [low, low + 2, low + 4], "a stride of two"
    assert high - low + 1 == 6, "the test pool fits exactly three servers"
    row = await finish(ctx, ctx.servers.submit_create({"name": "B3", "bedrock": True, "start": False}))
    assert row["status"] == "failed" and "two UDP ports" in row["message"]
    with pytest.raises(ServerError, match="already assigned"):
        ctx.servers.allocate_port("bedrock", low + 1)  # would collide with the IPv6 status port of the first server
