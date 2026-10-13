"""BungeeCord proxies end to end: made without a EULA or a world, configured through config.yml, stopped with "end", backed up without
saving a world, and updated like the other software (it has builds and nothing else)."""

from __future__ import annotations

import json
import tarfile

import pytest
from conftest import create_server, create_software_server, finish
from jvmfakes import FakeBungee

from pumpkin_manager import process
from pumpkin_manager.jvm.software import SoftwareError

yaml = pytest.importorskip("yaml")


def steps_of(ctx, row) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


def started_with(ctx, rec) -> dict:
    return json.loads((ctx.servers.server_dir(rec.id) / "fake-java.json").read_text(encoding="utf-8"))


def config_of(ctx, rec) -> dict:
    return yaml.safe_load((ctx.servers.server_dir(rec.id) / "config.yml").read_text(encoding="utf-8"))


def jar_of(ctx, rec) -> bytes:
    return (ctx.servers.server_dir(rec.id) / "BungeeCord.jar").read_bytes()


def console(ctx, rec) -> list[str]:
    return [line["m"] for line in ctx.pm.backlog(rec.id, 600)]


async def update(ctx, rec, build=None, *, announce_seconds=0, timeout=120):
    fresh = ctx.servers.get(rec.id)
    target = await ctx.software_updates.prepare(fresh, None, build)
    return await finish(ctx, ctx.software_updates.submit_update(fresh, target, announce_seconds=announce_seconds), timeout=timeout)


async def proxy(ctx, name="Network", **spec):
    return await create_software_server(ctx, name, "bungeecord", **spec)


# ------------------------------------------------------------------------------------------------------------------------------- catalog
async def test_a_proxy_has_no_minecraft_version_and_its_builds_are_the_successful_ones(jvm):
    world, ctx = jvm
    assert await ctx.software.versions("bungeecord") == [] and await ctx.software.recommended_minecraft("bungeecord") == ""
    options = await ctx.software.options("bungeecord", "whatever is asked: a proxy has no such thing")
    assert [b.build for b in options.builds] == ["2104", "2103", "2101", "2100"], "unstable and failed builds are not offered"
    assert options.java_minimum == 17 and options.default().build == "2104" and all(b.stable and b.minecraft == "" for b in options.builds)
    assert options.builds[0].url.endswith("/jenkins/job/BungeeCord/2104/artifact/bootstrap/target/BungeeCord.jar")
    assert options.builds[0].label == "BungeeCord (build 2104)" and options.public()["builds"][0]["label"] == "build 2104"
    assert await ctx.software.newer_minecraft("bungeecord", "") is None


async def test_a_build_that_is_not_listed_is_asked_for_by_its_number_and_a_wrong_number_is_refused(jvm):
    world, ctx = jvm
    build, _ = await ctx.software.resolve("bungeecord", None, "1999")
    assert build.build == "1999" and build.url.endswith("/1999/artifact/bootstrap/target/BungeeCord.jar"), "the address of any build is the same kind of address"
    for bad in ("abc", "-3", "0", "2104; rm -rf"):
        with pytest.raises(SoftwareError, match="BungeeCord has no build"):
            await ctx.software.resolve("bungeecord", None, bad)


# ------------------------------------------------------------------------------------------------------------------------------ creating
async def test_a_proxy_is_made_without_a_eula_or_a_world_and_started(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Network", "bungeecord", settings={"motd": "&aHello there", "max_players": 50}, eula=None)
    spec = rec.spec
    assert (spec["kind"], spec["minecraft"], spec["java"], spec["memory_mb"]) == ("software", "", 21, 512)
    assert spec["software"]["kind"] == "bungeecord" and spec["software"]["build"] == "2104" and spec["software"]["channel"] == "stable"
    assert spec["launch"] == {"jar": "BungeeCord.jar"} and "eula_accepted_at" not in spec and spec["jvm_args"] == ""
    assert rec.is_proxy and rec.is_java

    directory = ctx.servers.server_dir(rec.id)
    assert jar_of(ctx, rec) == FakeBungee.jar(2104)
    assert not (directory / "eula.txt").exists() and not (directory / "server.properties").exists(), "a proxy runs no Minecraft server"
    assert not list(ctx.settings.servers_dir.glob(".create-*"))
    config = config_of(ctx, rec)
    assert config["listeners"][0]["host"] == f"0.0.0.0:{rec.java_port}" and config["listeners"][0]["motd"] == "&aHello there"
    assert config["listeners"][0]["max_players"] == 50 and config["ip_forward"] is True and config["online_mode"] is True
    assert list(config["servers"]) == ["lobby"], "no other server to send players to: a placeholder, because BungeeCord does not start without one"

    assert (rec.policy.update_mode, rec.policy.start_timeout, rec.policy.stop_timeout) == ("notify", 300, 60)
    assert "libraries/*" in rec.policy.backup_exclude and "proxy.log*" in rec.policy.backup_exclude

    assert ctx.pm.runtime(rec.id).state == "running"
    argv = started_with(ctx, rec)["argv"]
    assert argv[0] == str(ctx.runtimes.executable(21)) and "-Xmx512M" in argv and argv[-2:] == ["-jar", "BungeeCord.jar"], "no nogui: it has no window"
    assert ctx.jdks.get(21) is None, "a proxy is not compiled: no JDK"

    view = ctx.servers.view(rec)
    assert view["proxy"] is True and view["software"]["proxy"] is True and view["software"]["label"] == "BungeeCord (build 2104)"
    assert view["software"]["minecraft"] is None and view["modpack"] is None and view["java"]["memory_mb"] == 512
    assert view["ports"]["java"] == rec.java_port


async def test_the_proxy_sends_players_to_the_servers_the_manager_runs(jvm):
    world, ctx = jvm
    survival = await create_software_server(ctx, "Survival", "paper", start=False)
    pumpkin = await create_server(ctx, "Pumpkin One", start=False)
    rec = await proxy(ctx, start=False)
    config = config_of(ctx, rec)
    assert sorted(config["servers"]) == sorted([survival.id, pumpkin.id])
    assert config["servers"][survival.id] == {"motd": "Survival", "address": f"127.0.0.1:{survival.java_port}", "restricted": False}
    assert config["servers"][pumpkin.id]["address"] == f"127.0.0.1:{pumpkin.java_port}" and config["servers"][pumpkin.id]["motd"] == "Pumpkin One"
    assert config["listeners"][0]["priorities"] == [next(iter(config["servers"]))], "players land on the first of them (the oldest server)"
    other = await proxy(ctx, "Second", start=False)
    assert survival.id in config_of(ctx, other)["servers"] and rec.id not in config_of(ctx, other)["servers"], "a proxy is not a server to send players to"


async def test_a_proxy_can_be_made_from_a_chosen_build(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Pinned", "bungeecord", software={"build": "2101"}, start=False)
    assert rec.spec["software"]["build"] == "2101" and jar_of(ctx, rec) == FakeBungee.jar(2101)


async def test_a_build_that_is_not_a_jar_is_refused_and_nothing_is_created(jvm):
    world, ctx = jvm
    world.bungee.corrupt.add(2104)
    row = await finish(ctx, ctx.servers.submit_create({"name": "Corrupt", "engine": "java", "software": {"kind": "bungeecord"}, "start": False}))
    assert row["status"] == "failed" and "not a jar file" in row["message"]
    assert ctx.repo.list_servers() == [] and not list(ctx.settings.servers_dir.iterdir())


async def test_a_build_that_is_gone_from_the_build_server_is_a_404_that_says_so(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Gone", "engine": "java", "software": {"kind": "bungeecord", "build": "1999"}, "start": False}))
    assert row["status"] == "failed" and "not there any more" in row["message"]


async def test_a_proxy_needs_no_eula_but_a_server_does(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Free", "engine": "java", "software": {"kind": "bungeecord"}, "start": False}))
    assert row["status"] == "success", row["message"]
    row = await finish(ctx, ctx.servers.submit_create({"name": "Needs", "engine": "java", "software": {"kind": "paper"}, "start": False}))
    assert row["status"] == "failed" and "Minecraft EULA" in row["message"]


async def test_the_settings_of_a_proxy_are_those_of_its_config_not_of_server_properties(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Strict", "engine": "java", "software": {"kind": "bungeecord"}, "start": False, "settings": {"difficulty": "hard"},
    }))
    assert row["status"] == "failed" and "unknown setting" in row["message"]
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Strict", "engine": "java", "software": {"kind": "bungeecord"}, "start": False, "settings": {"max_players": 0},
    }))
    assert row["status"] == "failed" and "between 1 and 100000" in row["message"]


# --------------------------------------------------------------------------------------------------------------- starting and stopping
async def test_a_proxy_is_stopped_with_end_because_it_has_no_stop(jvm):
    world, ctx = jvm
    rec = await proxy(ctx)
    assert any("Listening on" in line for line in console(ctx, rec))
    row = await finish(ctx, ctx.servers.submit_stop(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]
    runtime = ctx.pm.runtime(rec.id)
    assert runtime.state == "stopped" and runtime.exit_code == 0 and runtime.stop_command == "end"
    assert any("Thank you and goodbye" in line for line in console(ctx, rec)) and not any("Command not found" in line for line in console(ctx, rec))
    again = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert again["status"] == "success" and ctx.pm.runtime(rec.id).state == "running"


async def test_a_server_that_is_not_a_proxy_is_still_stopped_with_stop(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Plain", "paper")
    await finish(ctx, ctx.servers.submit_stop(ctx.servers.get(rec.id)))
    assert ctx.pm.runtime(rec.id).stop_command == "stop" and ctx.pm.runtime(rec.id).exit_code == 0


async def test_the_proxy_is_up_when_it_says_it_listens_and_answers_the_server_list(jvm):
    world, ctx = jvm
    rec = await proxy(ctx, settings={"motd": "&aAs the list says", "max_players": 33})
    await ctx.servers._poll_players(rec)
    runtime = ctx.pm.runtime(rec.id)
    assert runtime.ping_ok and runtime.mc_version == "BungeeCord 1.8.x-26.x" and runtime.players_max == 33 and runtime.motd == "&aAs the list says"
    await ctx.servers.command(rec, "fakejoin steve")
    await ctx.servers._poll_players(rec)
    assert runtime.players_online == 1


async def test_a_config_without_a_server_is_reported_at_once_not_after_the_start_timeout(jvm, monkeypatch):
    """BungeeCord logs an exception and then stays alive doing nothing: the manager acts on the line, as it does for Forge."""
    monkeypatch.setattr(process, "FAILURE_EXIT_GRACE", 0.5)
    world, ctx = jvm
    rec = await proxy(ctx, start=False)
    path = ctx.servers.server_dir(rec.id) / "config.yml"
    path.write_text(path.read_text().split("servers:")[0] + "servers: {}\nip_forward: true\n", encoding="utf-8")
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)), timeout=60)
    assert row["status"] == "failed" and "reported that it could not start" in row["message"] and "No servers defined" in row["message"]
    assert ctx.pm.runtime(rec.id).state in ("stopped", "crashed")


async def test_the_port_of_a_proxy_is_put_right_before_every_start(jvm):
    world, ctx = jvm
    rec = await proxy(ctx)
    await finish(ctx, ctx.servers.submit_stop(ctx.servers.get(rec.id)))
    path = ctx.servers.server_dir(rec.id) / "config.yml"
    path.write_text(path.read_text().replace(f"0.0.0.0:{rec.java_port}", "0.0.0.0:25577"), encoding="utf-8")  # somebody put BungeeCord's own port back
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]
    assert any("Updated manager-controlled settings in config.yml (port)" in s for s in steps_of(ctx, row))
    assert config_of(ctx, rec)["listeners"][0]["host"] == f"0.0.0.0:{rec.java_port}"


async def test_changing_the_port_of_a_proxy_changes_where_it_listens(jvm):
    world, ctx = jvm
    rec = await proxy(ctx, start=False)
    free = next(p for p in range(ctx.settings.port_range[0], ctx.settings.port_range[1] + 1) if p != rec.java_port)
    updated, restart = ctx.servers.patch(rec, {"java_port": free})
    assert updated.java_port == free
    assert config_of(ctx, rec)["listeners"][0]["host"] == f"0.0.0.0:{free}"
    row = await finish(ctx, ctx.servers.submit_start(updated))
    assert row["status"] == "success" and ctx.pm.runtime(rec.id).state == "running"


async def test_a_deleted_config_is_written_again_with_the_servers_of_the_manager_not_left_to_bungeecord(jvm):
    world, ctx = jvm
    survival = await create_software_server(ctx, "Survival", "paper", start=False)
    rec = await proxy(ctx, start=False)
    (ctx.servers.server_dir(rec.id) / "config.yml").unlink()
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]
    assert any("config.yml was missing; wrote a new one" in s for s in steps_of(ctx, row))
    config = config_of(ctx, rec)
    assert config["listeners"][0]["host"] == f"0.0.0.0:{rec.java_port}", "not BungeeCord's own port 25577"
    assert survival.id in config["servers"]


# ------------------------------------------------------------------------------------------------------------------------------ settings
async def test_the_settings_page_of_a_proxy_shows_its_config(jvm):
    world, ctx = jvm
    rec = await proxy(ctx, settings={"motd": "&aHi", "max_players": 25}, start=False)
    read = ctx.servers.read_config(rec)
    assert read["values"] == {"motd": "&aHi", "max_players": 25, "online_mode": True, "ip_forward": True} and read["title"] == "Proxy settings"
    assert [f["key"] for f in read["schema"]] == ["motd", "max_players", "online_mode", "ip_forward"] and read["exists"] and read["file"] == "config.yml"
    saved = ctx.servers.update_config(rec, {"motd": "&bNew", "max_players": 60, "online_mode": False})
    assert saved["saved"] is True
    config = config_of(ctx, rec)
    assert config["listeners"][0]["motd"] == "&bNew" and config["listeners"][0]["max_players"] == 60 and config["online_mode"] is False
    assert config["ip_forward"] is True and config["listeners"][0]["host"] == f"0.0.0.0:{rec.java_port}"
    assert (ctx.servers.server_dir(rec.id) / "config.yml.manager-bak").is_file(), "the file as it was before is kept"
    with pytest.raises(Exception, match="unknown setting"):
        ctx.servers.update_config(rec, {"difficulty": "hard"})


async def test_the_raw_config_of_a_proxy_is_config_yml_and_its_port_is_not_the_persons_to_change(jvm):
    world, ctx = jvm
    rec = await proxy(ctx, start=False)
    raw = ctx.servers.read_raw_config(rec)
    assert raw["file"] == "config.yml" and "listeners:" in raw["content"] and raw["managed_keys"]
    changed = raw["content"].replace(f"0.0.0.0:{rec.java_port}", "0.0.0.0:25999").replace("online_mode: true", "online_mode: false")
    result = ctx.servers.write_raw_config(rec, changed)
    assert result["reset_managed_keys"] == ["port"] and result["saved"] is True
    config = config_of(ctx, rec)
    assert config["online_mode"] is False and config["listeners"][0]["host"] == f"0.0.0.0:{rec.java_port}"
    with pytest.raises(Exception, match="config.yml is limited to 512 KiB"):
        ctx.servers.write_raw_config(rec, "x" * (513 * 1024))


async def test_a_server_that_is_not_a_proxy_keeps_its_server_properties(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Plain", "paper", start=False)
    assert "motd" in ctx.servers.read_config(rec)["values"] and ctx.servers.read_raw_config(rec)["file"] == "server.properties"
    assert ctx.servers.view(rec)["proxy"] is False


# ------------------------------------------------------------------------------------------------------------------------------- backups
async def test_a_proxy_is_backed_up_while_it_runs_without_asking_it_to_save_a_world(jvm):
    world, ctx = jvm
    rec = await proxy(ctx)
    directory = ctx.servers.server_dir(rec.id)
    (directory / "plugins").mkdir(exist_ok=True)
    (directory / "plugins" / "Perms.jar").write_bytes(b"a plugin")
    (directory / "libraries").mkdir(exist_ok=True)
    (directory / "libraries" / "netty.jar").write_bytes(b"x" * 500)
    (directory / "proxy.log.0").write_text("lines")
    mark = ctx.pm.runtime(rec.id).seq
    row = await finish(ctx, ctx.backups.submit_backup(ctx.servers.get(rec.id), "manual"))
    assert row["status"] == "success", row["message"]
    backup = ctx.repo.list_backups(rec.id)[0]
    assert backup["consistent"] and backup["build_tag"] == "BungeeCord (build 2104)"
    sent = [line["m"] for line in ctx.pm.backlog(rec.id, 600) if line["n"] > mark]
    assert not any("Command not found" in line for line in sent), "no save-off or save-all was typed into BungeeCord, which does not have them"
    assert not any("Flushing the world" in s for s in steps_of(ctx, row))
    with tarfile.open(ctx.settings.backup_dir / backup["file_name"]) as tar:
        names = tar.getnames()
    assert any(n.endswith("config.yml") for n in names) and any(n.endswith("plugins/Perms.jar") for n in names) and any(n.endswith("BungeeCord.jar") for n in names)
    assert not any(n.endswith("netty.jar") or n.endswith("proxy.log.0") for n in names), "what BungeeCord downloads or logs by itself stays out"


# ------------------------------------------------------------------------------------------------------------------------------- updating
async def test_a_newer_build_is_found_and_offered_once(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Old", "bungeecord", software={"build": "2101"}, start=False)
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 1, "errors": {}}
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["update"]["available"] is True and view["update"]["pending_software"]["label"] == "BungeeCord (build 2104)"
    assert view["software"]["pending"]["kind"] == "build" and view["software"]["pending"]["minecraft"] == "" and view["software"]["upgrade"] is None
    await ctx.software_updates.check()
    assert len([a for a in ctx.repo.list_activity(50, rec.id) if "Update available" in a["message"]]) == 1
    world.bungee.add_build(2110)
    await ctx.software_updates.check()
    assert ctx.servers.view(ctx.servers.get(rec.id))["software"]["pending"]["build"] == "2110"


async def test_the_newest_build_is_not_an_update(jvm):
    world, ctx = jvm
    await proxy(ctx, start=False)
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 0, "errors": {}}


async def test_the_update_stops_backs_up_switches_the_jar_and_starts_again(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Live", "bungeecord", software={"build": "2101"})
    directory = ctx.servers.server_dir(rec.id)
    (directory / "plugins").mkdir(exist_ok=True)
    (directory / "plugins" / "Perms.jar").write_bytes(b"a plugin")
    config_before = (directory / "config.yml").read_text()
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    steps = steps_of(ctx, row)
    positions = [next(i for i, s in enumerate(steps) if needle in s) for needle in ("Downloading", "Stopping", "Backup created", "Installing", "Starting")]
    assert positions == sorted(positions)
    assert jar_of(ctx, rec) == FakeBungee.jar(2104) and (directory / "plugins" / "Perms.jar").read_bytes() == b"a plugin"
    assert (directory / "config.yml").read_text() == config_before, "the person's configuration is not touched"
    rec = ctx.servers.get(rec.id)
    assert rec.spec["software"]["build"] == "2104" and rec.spec["previous"]["software"]["build"] == "2101" and rec.spec["minecraft"] == ""
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert (update_row["status"], update_row["from_tag"], update_row["to_tag"]) == ("success", "(build 2101)", "(build 2104)")
    assert ctx.repo.get_backup(update_row["backup_id"])["build_tag"] == "BungeeCord (build 2101)"
    assert ctx.pm.runtime(rec.id).state == "running" and ctx.jdks.get(21) is None


async def test_players_on_the_proxy_are_told_with_alert_because_bungeecord_has_no_say(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Busy", "bungeecord", software={"build": "2101"})
    await ctx.servers.command(rec, "fakejoin steve")
    await ctx.servers._poll_players(rec)
    assert ctx.pm.runtime(rec.id).players_online == 1
    await ctx.software_updates.check()
    row = await update(ctx, rec, announce_seconds=1)
    assert row["status"] == "success", row["message"]
    assert any("[Alert] The server is being updated and restarts in 1 seconds." in line for line in console(ctx, rec))


async def test_a_build_that_does_not_start_is_rolled_back_automatically(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Bad", "bungeecord", software={"build": "2104"})
    before = jar_of(ctx, rec)
    world.bungee.add_build(2110, broken=True)
    world.behavior["crash_if_jar_contains"] = "BROKEN"
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success"
    assert jar_of(ctx, rec) == before and ctx.servers.get(rec.id).spec["software"]["build"] == "2104" and ctx.pm.runtime(rec.id).state == "running"
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "rolled_back"
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0, "that build is not offered again"
    world.bungee.add_build(2111)
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1


async def test_a_download_that_is_not_a_jar_changes_nothing(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Steady", "bungeecord", software={"build": "2101"})
    before = jar_of(ctx, rec)
    world.bungee.add_build(2110, corrupt=True)
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success" and ctx.repo.list_updates(rec.id)[0]["status"] == "failed" and "not a jar file" in ctx.repo.list_updates(rec.id)[0]["message"]
    assert jar_of(ctx, rec) == before and ctx.pm.runtime(rec.id).state == "running"
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0, "a file that was not what was promised is not tried again"
    world.bungee.add_build(2111)
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1


async def test_going_back_to_the_build_before_an_update(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Undo", "bungeecord", software={"build": "2101"})
    await ctx.software_updates.check()
    await update(ctx, rec)
    assert jar_of(ctx, rec) == FakeBungee.jar(2104)
    row = await finish(ctx, ctx.software_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert jar_of(ctx, rec) == FakeBungee.jar(2101) and ctx.servers.get(rec.id).spec["software"]["build"] == "2101"


async def test_the_builds_of_a_proxy_can_be_listed_from_its_own_page(jvm):
    world, ctx = jvm
    rec = await proxy(ctx, start=False)
    assert (await ctx.software_updates.versions(rec))["versions"] == []
    builds = await ctx.software_updates.builds(rec, "")
    assert [b["build"] for b in builds["builds"]] == ["2104", "2103", "2101", "2100"] and builds["current_build"] == "2104" and builds["current"] == ""


async def test_a_missing_jar_is_downloaded_again_before_the_proxy_starts(jvm):
    world, ctx = jvm
    rec = await proxy(ctx, start=False)
    (ctx.servers.server_dir(rec.id) / "BungeeCord.jar").unlink()
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]
    assert any("missing from the server folder; downloading them again" in s for s in steps_of(ctx, row)) and jar_of(ctx, rec) == FakeBungee.jar(2104)
    assert ctx.pm.runtime(rec.id).state == "running"
