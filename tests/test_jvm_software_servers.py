"""Vanilla, Paper and Fabric servers end to end: create, start, repair, what they remember, and what must not happen to them."""

from __future__ import annotations

import json

import pytest
from conftest import create_java_server, create_software_server, finish, wait_until
from jvmfakes import server_jar

from pumpkin_manager.jvm import properties as props
from pumpkin_manager.jvm.software import JAR_NAMES


def props_of(ctx, rec) -> dict[str, str]:
    return props.parse((ctx.servers.server_dir(rec.id) / "server.properties").read_text(encoding="utf-8"))


def started_with(ctx, rec) -> dict:
    return json.loads((ctx.servers.server_dir(rec.id) / "fake-java.json").read_text(encoding="utf-8"))


def steps_of(ctx, row) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


# --------------------------------------------------------------------------------------------------- Paper
async def test_a_paper_server_is_created_with_the_newest_stable_build_and_started(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Plugins", "paper", settings={"motd": "Welcome", "max-players": 12})
    assert rec.engine == "java" and rec.is_java and rec.build_id is None

    spec = rec.spec
    assert (spec["kind"], spec["minecraft"], spec["java"], spec["memory_mb"]) == ("software", "1.21.4", 21, 2048)
    assert spec["software"]["kind"] == "paper" and spec["software"]["build"] == "11" and spec["software"]["channel"] == "stable"
    assert spec["software"]["support"] == {"status": "SUPPORTED"} and spec["software"]["installed_at"]
    assert spec["launch"] == {"jar": "paper.jar"} and spec["eula_accepted_at"] and "pack" not in spec and "loader" not in spec

    directory = ctx.servers.server_dir(rec.id)
    jar = (directory / "paper.jar").read_bytes()
    assert jar == world.paper.blobs[next(iter(k for k, v in world.paper.blobs.items() if v == jar))] and b"paperclip.json" in jar
    assert not list(ctx.settings.servers_dir.glob(".create-*")), "the staging folder is gone"
    assert "eula=true" in (directory / "eula.txt").read_text()
    values = props_of(ctx, rec)
    assert values["server-port"] == str(rec.java_port) and values["motd"] == "Welcome" and values["max-players"] == "12"
    assert values["op-permission-level"] == "1"

    assert rec.policy.update_mode == "auto", "Paper builds are small steps: installed by themselves unless told otherwise"
    assert rec.policy.start_timeout == 600 and rec.policy.stop_timeout == 120 and "cache" in rec.policy.backup_exclude
    assert spec["jvm_args"] == "-XX:+UseG1GC -XX:MaxGCPauseMillis=200", "PaperMC's recommended options for the version"

    assert ctx.pm.runtime(rec.id).state == "running"
    argv = started_with(ctx, rec)["argv"]
    assert argv[0] == str(ctx.runtimes.executable(21)) and "-Xms2048M" in argv and "-Xmx2048M" in argv
    assert "-XX:MaxGCPauseMillis=200" in argv and argv[-3:] == ["-jar", "paper.jar", "nogui"]

    view = ctx.servers.view(rec)
    assert view["modpack"] is None and view["software"]["label"] == "Paper 1.21.4 (build 11)" and view["software"]["name"] == "Paper"
    assert view["software"]["pending"] is None and view["software"]["can_roll_back"] is False and view["update"]["available"] is False
    assert view["java"]["major"] == 21 and view["java"]["memory_mb"] == 2048
    assert any("Done" in step["text"] or "Starting" in step["text"] for step in ctx.repo.get_job(ctx.repo.list_jobs()[0]["id"])["steps"])


async def test_a_paper_server_can_be_made_from_a_chosen_version_and_build_without_the_recommended_options(jvm):
    world, ctx = jvm
    rec = await create_software_server(
        ctx, "Older", "paper", software={"minecraft": "1.21.1", "build": "100", "recommended_flags": False}, start=False, memory_mb=3072,
    )
    assert (rec.spec["minecraft"], rec.spec["software"]["build"], rec.spec["memory_mb"], rec.spec["jvm_args"]) == ("1.21.1", "100", 3072, "")
    assert (ctx.servers.server_dir(rec.id) / "paper.jar").is_file() and ctx.pm.runtime(rec.id).state == "stopped"


async def test_your_own_java_options_win_over_the_recommended_ones(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Mine", "paper", jvm_args="-Xss4M", start=False)
    assert rec.spec["jvm_args"] == "-Xss4M"


async def test_an_experimental_paper_build_is_used_with_a_warning_when_there_is_no_stable_one(jvm):
    world, ctx = jvm
    world.paper.add_version("1.21.5")
    world.paper.add_build("1.21.5", 3, "BETA")
    world.net.minecraft({"1.21.5": 21, "1.21.4": 21, "1.21.1": 21, "1.20.4": 17, "1.20.1": 17, "1.16.5": 8})
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Early", "engine": "java", "eula": True, "software": {"kind": "paper", "minecraft": "1.21.5"}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    assert any("no stable build for Minecraft 1.21.5" in s for s in steps_of(ctx, row))
    assert ctx.repo.list_servers()[0].spec["software"]["channel"] == "beta"


async def test_an_old_version_that_paper_no_longer_maintains_comes_with_a_warning_and_runs_on_java_8(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Ancient", "engine": "java", "eula": True, "software": {"kind": "paper", "minecraft": "1.16.5"}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    assert any("no longer maintains Minecraft 1.16.5 (since 2025-11-14)" in s for s in steps_of(ctx, row))
    rec = ctx.repo.list_servers()[0]
    assert rec.spec["java"] == 8 and ctx.runtimes.get(8) is not None and rec.spec["software"]["support"]["status"] == "UNSUPPORTED"


# -------------------------------------------------------------------------------------------------- Vanilla
async def test_a_vanilla_server_runs_mojangs_own_jar_on_the_java_it_needs(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Pure", "vanilla", software={"minecraft": "1.20.4"})
    assert rec.spec["software"] == {
        "kind": "vanilla", "minecraft": "1.20.4", "build": "", "channel": "release", "installed_at": rec.spec["software"]["installed_at"],
        "support": None,
    }
    assert rec.spec["java"] == 17 and rec.spec["launch"] == {"jar": "server.jar"} and rec.spec["jvm_args"] == ""
    assert rec.policy.update_mode == "notify", "a new Minecraft version is never installed by itself"
    assert (ctx.servers.server_dir(rec.id) / "server.jar").read_bytes() == server_jar("1.20.4")
    assert started_with(ctx, rec)["argv"][-3:] == ["-jar", "server.jar", "nogui"] and ctx.pm.runtime(rec.id).state == "running"
    view = ctx.servers.view(rec)
    assert view["software"]["label"] == "Vanilla 1.20.4" and view["software"]["build"] is None


async def test_without_a_chosen_version_the_newest_release_is_used(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Newest", "vanilla", start=False)
    assert rec.spec["minecraft"] == "1.21.4"


# --------------------------------------------------------------------------------------------------- Fabric
async def test_a_fabric_server_gets_its_launcher_and_fabric_api(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Modded", "fabric", software={"minecraft": "1.21.1"})
    directory = ctx.servers.server_dir(rec.id)
    assert rec.spec["software"]["kind"] == "fabric" and rec.spec["software"]["build"] == "0.16.10", "the newest stable loader"
    assert rec.spec["launch"] == {"jar": "fabric-server-launch.jar"} and (directory / "fabric-server-launch.jar").is_file()
    api = rec.spec["software"]["fabric_api"]
    assert api["number"] == "0.106.0+1.21.1" and api["file"] == "mods/fabric-api-0.106.0+1.21.1.jar" and (directory / api["file"]).is_file()
    assert rec.policy.update_mode == "notify" and rec.spec["jvm_args"] == ""
    assert started_with(ctx, rec)["argv"][-3:] == ["-jar", "fabric-server-launch.jar", "nogui"]
    view = ctx.servers.view(rec)
    assert view["software"]["label"] == "Fabric 1.21.1 (loader 0.16.10)" and view["software"]["fabric_api"] == {"number": "0.106.0+1.21.1"}


async def test_fabric_api_can_be_left_out_and_a_missing_one_is_only_a_warning(jvm):
    world, ctx = jvm
    bare = await create_software_server(ctx, "Bare", "fabric", software={"minecraft": "1.21.1", "fabric_api": False}, start=False)
    assert "fabric_api" not in bare.spec["software"] and not (ctx.servers.server_dir(bare.id) / "mods").exists()

    row = await finish(ctx, ctx.servers.submit_create({
        "name": "NoApi", "engine": "java", "eula": True, "software": {"kind": "fabric", "minecraft": "1.20.4"}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    assert any("Fabric API has no version for Minecraft 1.20.4 yet" in s for s in steps_of(ctx, row))
    assert "fabric_api" not in next(s for s in ctx.repo.list_servers() if s.name == "NoApi").spec["software"]


# ------------------------------------------------------------------------------------------ what is refused
@pytest.mark.parametrize("software,message", [
    ({"kind": "forge"}, "Unknown server software 'forge'"),
    ({}, "Unknown server software 'None'"),
    ({"kind": "paper", "minecraft": "1.7.10"}, "Paper has no builds for Minecraft 1.7.10"),
    ({"kind": "paper", "minecraft": "1.21.4", "build": "999"}, "has no build 999"),
    ({"kind": "fabric", "minecraft": "1.13.2"}, "Fabric does not support Minecraft 1.13.2"),
    ({"kind": "vanilla", "minecraft": "../../x"}, "not a valid Minecraft version"),
])
async def test_software_that_does_not_exist_is_explained_and_nothing_is_left_behind(jvm, software, message):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Nope", "engine": "java", "eula": True, "software": software}))
    assert row["status"] == "failed" and message in row["message"]
    assert ctx.repo.list_servers() == [] and list(ctx.settings.servers_dir.iterdir()) == []


async def test_the_minecraft_eula_and_the_name_are_checked_before_anything_is_downloaded(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "NoEula", "engine": "java", "eula": False, "software": {"kind": "paper"}}))
    assert row["status"] == "failed" and "EULA" in row["message"]
    await create_software_server(ctx, "Taken", "vanilla", start=False)
    downloads = world.net.server_jar_downloads
    row = await finish(ctx, ctx.servers.submit_create({"name": "taken", "engine": "java", "eula": True, "software": {"kind": "vanilla"}}))
    assert row["status"] == "failed" and "already exists" in row["message"] and world.net.server_jar_downloads == downloads
    row = await finish(ctx, ctx.servers.submit_create({"name": "x" * 41, "engine": "java", "eula": True, "software": {"kind": "vanilla"}}))
    assert row["status"] == "failed" and "1-40 characters" in row["message"]
    row = await finish(ctx, ctx.servers.submit_create({"name": "Bad", "engine": "java", "eula": True, "software": {"kind": "vanilla"}, "memory_mb": 600, "jvm_args": "-jar evil.jar"}))
    assert row["status"] == "failed" and "not allowed" in row["message"]


async def test_a_download_that_does_not_match_its_checksum_creates_no_server(jvm):
    world, ctx = jvm
    build_sha = next(iter(world.paper.blobs))
    world.paper.blobs = {sha: b"PK\x03\x04 swapped on the way" for sha in world.paper.blobs}
    del build_sha
    row = await finish(ctx, ctx.servers.submit_create({"name": "Tampered", "engine": "java", "eula": True, "software": {"kind": "paper"}}))
    assert row["status"] == "failed" and "SHA256" in row["message"]
    assert ctx.repo.list_servers() == [] and list(ctx.settings.servers_dir.iterdir()) == []


async def test_a_service_that_is_down_is_reported_when_the_server_is_created(jvm):
    world, ctx = jvm
    world.net.offline = True
    row = await finish(ctx, ctx.servers.submit_create({"name": "Offline", "engine": "java", "eula": True, "software": {"kind": "paper"}}), timeout=120)
    assert row["status"] == "failed" and "Could not reach" in row["message"] and ctx.repo.list_servers() == []


# ------------------------------------------------------------------------------------------ living with them
async def test_a_missing_jar_is_fetched_again_before_the_server_starts(jvm):
    world, ctx = jvm
    for kind in ("paper", "vanilla", "fabric"):
        rec = await create_software_server(ctx, f"Heal {kind}", kind, software={"minecraft": "1.21.1"}, start=False)
        jar = ctx.servers.server_dir(rec.id) / JAR_NAMES[kind]
        original = jar.read_bytes()
        jar.unlink()
        row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
        assert row["status"] == "success", row["message"]
        assert any("missing from the server folder; downloading them again" in s for s in steps_of(ctx, row)), kind
        assert jar.read_bytes() == original and ctx.pm.runtime(rec.id).state == "running", kind
        await finish(ctx, ctx.servers.submit_stop(ctx.servers.get(rec.id)))


async def test_backups_of_these_servers_are_tagged_with_what_they_run(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Tagged", "paper", software={"minecraft": "1.21.1"}, start=False)
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_backups(rec.id)[0]["build_tag"] == "Paper 1.21.1 (build 101)"
    modded = await create_java_server(ctx, "Packed", start=False)
    assert modded.java_tag == "Cool Pack 1.0.0" and rec.java_tag == "Paper 1.21.1 (build 101)"


async def test_backups_leave_out_what_the_software_unpacks_by_itself_but_nothing_that_is_the_persons(jvm):
    """A fresh server is mostly libraries the software extracts or downloads again on its own: two thirds of every backup for nothing."""
    import tarfile

    world, ctx = jvm
    for kind, regenerated in (("paper", ["cache/mojang.jar", "libraries/org/lib.jar", "versions/1.21.4/server.jar"]),
                              ("fabric", [".fabric/server/mojang.jar", "libraries/org/lib.jar", "versions/1.21.4/server.jar"])):
        rec = await create_software_server(ctx, f"Lean {kind}", kind, software={"minecraft": "1.21.4"}, start=False)
        directory = ctx.servers.server_dir(rec.id)
        kept = ["plugins/Foo/versions/keep.txt", "plugins/Foo/libraries/keep.txt", "mods/libraries/keep.txt", "world/level.dat", "server.properties"]
        for path in [*regenerated, *kept[:-2]]:
            (directory / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / path).write_bytes(b"x" * 100)
        (directory / "world").mkdir(exist_ok=True)
        (directory / "world" / "level.dat").write_bytes(b"world")
        row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
        assert row["status"] == "success", row["message"]
        with tarfile.open(ctx.settings.backup_dir / ctx.repo.list_backups(rec.id)[0]["file_name"]) as tar:
            names = tar.getnames()
        assert not any(n.endswith(path) for n in names for path in regenerated), f"{kind}: {[n for n in names if 'libraries' in n or 'versions' in n]}"
        assert all(any(n.endswith(path) for n in names) for path in kept), f"{kind}: what is the person's stays in"
        assert any(n.endswith(JAR_NAMES[kind]) for n in names), "the software's own jar is in the backup"


async def test_a_pumpkin_server_has_no_software_and_no_modpack(jvm):
    world, ctx = jvm
    modded = await create_java_server(ctx, "Packed", start=False)
    view = ctx.servers.view(modded)
    assert view["software"] is None and view["modpack"]["title"] == "Cool Pack"
    assert ctx.servers.jvm.describe_software(modded) is None


async def test_settings_players_and_the_console_work_like_on_any_other_java_server(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Normal", "paper")
    config = ctx.servers.read_config(rec)
    assert config["values"]["op-permission-level"] == 1 and any(f["key"] == "max-players" for f in config["schema"])
    saved = ctx.servers.update_config(rec, {"max-players": 7})
    assert saved["restart_required"] is True and props_of(ctx, rec)["max-players"] == "7"
    await ctx.servers.player_action(rec, "whitelist_add", "Steve")
    await wait_until(lambda: ctx.servers.read_player_lists(rec)["whitelist"], 10, what="the server to write the whitelist")
    assert ctx.servers.read_player_lists(rec)["whitelist"][0]["name"] == "Steve"
    assert ctx.servers.op_default_level(rec) == 1
    result = ctx.servers.patch(rec, {"java": {"memory_mb": 4096}})
    assert result[1] is True and ctx.servers.get(rec.id).spec["memory_mb"] == 4096
