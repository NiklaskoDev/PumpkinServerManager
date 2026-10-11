"""Server creation, start/stop, console, monitoring and crash handling against the fake Pumpkin."""

from __future__ import annotations

import tomllib

from conftest import create_server, finish, wait_until

from pumpkin_manager.mcnet import MsmpClient, server_list_ping


async def test_create_start_monitor_stop(ctx):
    server = await create_server(ctx, "Survival", settings={"motd": "Hello there", "max_players": 12})

    rt = ctx.pm.runtime(server.id)
    assert rt.state == "running"
    assert server.build_id and ctx.builds.is_available(ctx.repo.get_build(server.build_id))

    config = tomllib.loads((ctx.settings.servers_dir / server.id / "pumpkin.toml").read_text())
    assert config["networking"]["java"]["address"] == f"0.0.0.0:{server.java_port}"
    assert config["networking"]["management"]["enabled"] is True
    assert config["networking"]["bedrock"]["enabled"] is False  # would collide on UDP 19132 otherwise
    assert config["telemetry"]["enabled"] is False
    assert config["commands"]["use_tty"] is False

    info = await server_list_ping("127.0.0.1", server.java_port)
    assert info["max"] == 12 and info["motd"] == "Hello there" and info["version"] == "0.1.0"

    await ctx.pm.send_command(server.id, "fakejoin Alice")
    async with MsmpClient(server.mgmt_port, server.mgmt_secret) as client:
        assert [p["name"] for p in await client.players()] == ["Alice"]
    await ctx.servers.monitor_tick()
    await ctx.servers._poll_players(server)
    assert rt.players_online == 1 and rt.player_names == ["Alice"] and rt.msmp_ok

    lines = [line["m"] for line in ctx.pm.backlog(server.id)]
    assert any("Server is now running" in m for m in lines)

    job = ctx.servers.submit_stop(server)
    assert (await finish(ctx, job))["status"] == "success"
    assert rt.state == "stopped" and rt.exit_code == 0
    assert ctx.repo.get_server(server.id).desired_running is False
    assert (ctx.settings.servers_dir / server.id / "world" / "level.dat").exists()


async def test_wrong_management_secret_is_rejected(ctx):
    server = await create_server(ctx, "Secrets")
    try:
        async with MsmpClient(server.mgmt_port, "x" * 40) as client:
            await client.players()
    except Exception as exc:
        assert "management" in str(exc).lower() or "401" in str(exc) or "HTTP" in str(exc)
    else:
        raise AssertionError("connection with a wrong secret must fail")


async def test_each_server_gets_its_own_ports(ctx):
    a = await create_server(ctx, "One", bedrock=True)
    b = await create_server(ctx, "Two")
    assert a.java_port != b.java_port and a.mgmt_port != b.mgmt_port
    assert a.bedrock_port is not None and b.bedrock_port is None
    cfg_b = tomllib.loads((ctx.settings.servers_dir / b.id / "pumpkin.toml").read_text())
    assert cfg_b["networking"]["bedrock"]["enabled"] is False


async def test_crash_is_detected_and_restarted(ctx):
    server = await create_server(ctx, "Crashy")
    rt = ctx.pm.runtime(server.id)
    pid = rt.pid
    rt.proc.kill()
    await wait_until(lambda: rt.state == "crashed", what="crash detection")
    assert rt.exit_code != 0
    crashes = [a for a in ctx.repo.list_activity(20) if a["kind"] == "crash"]
    assert any("unexpectedly" in a["message"] for a in crashes)
    # restart_on_crash: the manager brings it back after a short back-off
    await wait_until(lambda: rt.state == "running" and rt.pid != pid, timeout=30, what="automatic restart")


async def test_start_failure_is_reported_with_console_tail(ctx, source):
    source.publish("stable", "0.2.0", crash_on_start=True)
    server = await create_server(ctx, "Broken", start=False)
    job = ctx.servers.submit_start(server)
    row = await finish(ctx, job)
    assert row["status"] == "failed"
    assert "exited during startup" in row["message"] and "simulated crash" in row["message"]
    assert ctx.pm.runtime(server.id).state != "running"


async def test_kill_escalation_when_server_ignores_stop(ctx, source):
    source.publish("stable", "0.3.0", hang_on_stop=True, ignore_sigterm=True)
    server = await create_server(ctx, "Stubborn")
    ctx.repo.update_server(server.id, policy={**server.policy.model_dump(), "stop_timeout": 5})
    job = ctx.servers.submit_stop(ctx.repo.get_server(server.id))
    row = await finish(ctx, job, timeout=60)
    assert row["status"] == "success"
    assert ctx.pm.runtime(server.id).state == "stopped"


async def test_busy_server_rejects_second_operation(ctx):
    from pumpkin_manager.jobs import Busy

    server = await create_server(ctx, "Busy")
    first = ctx.servers.submit_restart(server)
    try:
        ctx.servers.submit_stop(server)
    except Busy as exc:
        assert exc.status == 409
    else:
        raise AssertionError("expected Busy")
    await finish(ctx, first)


async def test_delete_server_keeps_final_backup(ctx):
    server = await create_server(ctx, "Doomed")
    job = ctx.servers.submit_delete(server, final_backup=True, delete_backups=False)
    assert (await finish(ctx, job))["status"] == "success"
    assert ctx.repo.get_server(server.id) is None
    assert not (ctx.settings.servers_dir / server.id).exists()
    backups = ctx.repo.list_backups(server.id)
    assert [b["kind"] for b in backups] == ["pre-delete"]
    assert (ctx.settings.backup_dir / backups[0]["file_name"]).is_file()
