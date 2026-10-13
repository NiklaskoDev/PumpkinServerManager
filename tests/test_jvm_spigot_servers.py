"""Spigot servers end to end: what the catalog offers, building one, starting it, repairing it, and updating it (a new build is a new
BuildTools run, which is what makes it different from every other software)."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from conftest import create_software_server, finish
from jvmfakes import FakeSpigot

from pumpkin_manager.jvm.software import SoftwareError
from pumpkin_manager.jvm.softwareupdates import PREVIOUS_FIELDS


def steps_of(ctx, row) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


def started_with(ctx, rec) -> dict:
    return json.loads((ctx.servers.server_dir(rec.id) / "fake-java.json").read_text(encoding="utf-8"))


def jar_of(ctx, rec) -> bytes:
    return (ctx.servers.server_dir(rec.id) / "spigot.jar").read_bytes()


def build_folders(ctx) -> list[Path]:
    folder = ctx.settings.cache_dir / "spigot-build"
    return list(folder.iterdir()) if folder.is_dir() else []


async def update(ctx, rec, minecraft=None, build=None, *, confirm=False, timeout=120):
    fresh = ctx.servers.get(rec.id)
    target = await ctx.software_updates.prepare(fresh, minecraft, build, confirm_downgrade=confirm)
    return await finish(ctx, ctx.software_updates.submit_update(fresh, target), timeout=timeout)


# ----------------------------------------------------------------------------------------------------------------------------- catalog
async def test_spigot_offers_the_releases_it_has_a_build_for_newest_first(jvm):
    world, ctx = jvm
    listed = await ctx.software.versions("spigot")
    assert [v["id"] for v in listed] == ["1.21.4", "1.21.1", "1.20.4", "1.16.5"], "no pre-releases, newest first"
    assert all(v["type"] == "release" and v["published"] for v in listed)
    assert (await ctx.software.versions("spigot", snapshots=True)) == listed, "SpigotMC lists no snapshots of its own"
    assert await ctx.software.recommended_minecraft("spigot") == "1.21.4"


async def test_a_minecraft_version_has_one_build_the_one_spigotmc_names(jvm):
    world, ctx = jvm
    options = await ctx.software.options("spigot", "1.21.4")
    assert [(b.build, b.channel, b.stable, b.url) for b in options.builds] == [("4458", "stable", True, None)] and options.java_minimum == 21
    assert options.public()["builds"][0]["label"] == "build 4458" and options.default().label == "Spigot 1.21.4 (build 4458)"
    assert (await ctx.software.options("spigot", "1.16.5")).builds[0].build == "3096a", "SpigotMC's names are not always numbers"
    with pytest.raises(SoftwareError, match="Spigot has no build for Minecraft 1.19"):
        await ctx.software.options("spigot", "1.19")


async def test_a_build_that_is_not_the_one_on_offer_is_refused_and_the_message_names_the_one_that_is(jvm):
    world, ctx = jvm
    with pytest.raises(SoftwareError, match="Spigot has no build 4400 for Minecraft 1.21.4. The one SpigotMC offers is 4458"):
        await ctx.software.resolve("spigot", "1.21.4", "4400")
    build, _ = await ctx.software.resolve("spigot", "1.21.4", "4458")
    assert build.build == "4458"


@pytest.mark.parametrize("version", ["latest", "1.18-rc3", "1.13-pre7", "9.9", "1.21.4.1"])
async def test_only_a_release_that_spigotmc_lists_can_be_chosen(jvm, version):
    """BuildTools takes 'latest' for the newest Minecraft version: a server recorded as 'latest' would not say what it runs."""
    world, ctx = jvm
    with pytest.raises(SoftwareError, match=f"Spigot has no build for Minecraft {version}"):
        await ctx.software.options("spigot", version)
    assert not [url for url in world.net.requests if url.endswith(f"/versions/{version}.json")], "no question is asked about it"


async def test_a_name_that_could_end_up_in_an_address_is_refused(jvm):
    world, ctx = jvm
    world.spigot.versions["1.21.4"]["name"] = "../../etc"
    with pytest.raises(SoftwareError, match="cannot be used"):
        await ctx.software.options("spigot", "1.21.4")


# --------------------------------------------------------------------------------------------------------------------------- creating
async def test_a_spigot_server_is_built_for_the_newest_version_and_started(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Plugins", "spigot", settings={"motd": "Welcome", "max-players": 12})
    spec = rec.spec
    assert (spec["kind"], spec["minecraft"], spec["java"], spec["memory_mb"]) == ("software", "1.21.4", 21, 2048)
    assert spec["software"]["kind"] == "spigot" and spec["software"]["build"] == "4458" and spec["software"]["channel"] == "stable"
    assert spec["launch"] == {"jar": "spigot.jar"} and spec["eula_accepted_at"] and spec["jvm_args"] == "" and "pack" not in spec

    directory = ctx.servers.server_dir(rec.id)
    assert jar_of(ctx, rec) == FakeSpigot.jar("1.21.4", "4458")
    assert "eula=true" in (directory / "eula.txt").read_text() and "motd=Welcome" in (directory / "server.properties").read_text()
    assert not list(ctx.settings.servers_dir.glob(".create-*")) and not build_folders(ctx), "neither the staging folder nor the build's folder is left"
    assert rec.policy.update_mode == "notify", "a build that takes a quarter of an hour is the person's call"
    assert rec.policy.start_timeout == 600 and rec.policy.stop_timeout == 120 and "bundler/*" in rec.policy.backup_exclude

    # built with a JDK, run with the JRE
    assert Path(world.spigot.calls[0]["argv"][0]) == ctx.jdks.executable(21) and world.net.jdk_downloads == 1 and world.spigot.buildtools_downloads == 1
    assert ctx.runtimes.get(21) is not None and ctx.runtimes.executable(21) != ctx.jdks.executable(21)
    assert ctx.pm.runtime(rec.id).state == "running"
    argv = started_with(ctx, rec)["argv"]
    assert argv[0] == str(ctx.runtimes.executable(21)) and argv[-3:] == ["-jar", "spigot.jar", "nogui"] and "-Xmx2048M" in argv

    view = ctx.servers.view(rec)
    assert view["software"]["label"] == "Spigot 1.21.4 (build 4458)" and view["software"]["name"] == "Spigot" and view["software"]["proxy"] is False
    assert view["proxy"] is False and view["modpack"] is None and view["java"]["major"] == 21
    steps = steps_of(ctx, ctx.repo.list_jobs()[0])
    assert any("Using Spigot 1.21.4 (build 4458)" in s for s in steps)


async def test_each_minecraft_version_is_built_with_the_java_it_needs(jvm):
    world, ctx = jvm
    for version, java in (("1.20.4", 17), ("1.16.5", 8)):
        rec = await create_software_server(ctx, f"Old {version}", "spigot", software={"minecraft": version}, start=False)
        assert rec.spec["java"] == java and ctx.jdks.get(java) is not None, version
    assert [Path(c["argv"][0]) for c in world.spigot.calls] == [ctx.jdks.executable(17), ctx.jdks.executable(8)]
    assert world.net.jdk_downloads == 2


async def test_the_build_is_told_to_the_job_with_its_stages(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Staged", "engine": "java", "eula": True, "software": {"kind": "spigot"}, "start": False}))
    assert row["status"] == "success", row["message"]
    job = ctx.repo.get_job(row["id"])
    assert job["progress"] is not None
    texts = steps_of(ctx, row)
    assert any("Looking up Spigot" in t for t in texts) and any("Server 'Staged' created" in t for t in texts)


async def test_a_build_that_fails_creates_no_server_and_says_why(jvm):
    world, ctx = jvm
    world.spigot.fail = "java.net.UnknownHostException: hub.spigotmc.org"
    row = await finish(ctx, ctx.servers.submit_create({"name": "Doomed", "engine": "java", "eula": True, "software": {"kind": "spigot"}, "start": False}))
    assert row["status"] == "failed" and "BuildTools failed (exit code 1)" in row["message"] and "UnknownHostException" in row["message"]
    assert ctx.repo.list_servers() == [] and not list(ctx.settings.servers_dir.iterdir()), "no half-made server"
    assert not build_folders(ctx)


async def test_a_minecraft_version_spigot_does_not_know_is_refused_before_anything_is_downloaded(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Nope", "engine": "java", "eula": True, "software": {"kind": "spigot", "minecraft": "1.19"}, "start": False}))
    assert row["status"] == "failed" and "Spigot has no build for Minecraft 1.19" in row["message"]
    assert world.net.jdk_downloads == 0 and not world.spigot.calls


async def test_spigot_needs_the_eula_like_every_minecraft_server(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Without", "engine": "java", "software": {"kind": "spigot"}, "start": False}))
    assert row["status"] == "failed" and "Minecraft EULA" in row["message"]
    assert world.net.jdk_downloads == 0 and not world.spigot.calls, "refused before the long build"


# --------------------------------------------------------------------------------------------------------------------------- repairing
async def test_a_missing_jar_is_built_again_before_the_server_starts_and_a_newer_build_is_noted(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Lost", "spigot", start=False)
    (ctx.servers.server_dir(rec.id) / "spigot.jar").unlink()
    world.spigot.new_build("1.21.4", "4470")  # SpigotMC has published a newer build for this version meanwhile
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert any("missing from the server folder; building it again" in s for s in steps_of(ctx, row))
    assert jar_of(ctx, rec) == FakeSpigot.jar("1.21.4", "4470"), "SpigotMC keeps no older builds: this is the newest"
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "4470" and ctx.pm.runtime(rec.id).state == "running"


async def test_what_a_build_left_behind_is_removed_when_the_manager_starts(tmp_path):
    """The manager can be stopped in the middle of a build (it ends the build, but a power cut or a kill does not): the half gigabyte of
    sources, decompiled Minecraft and Maven downloads must not stay for ever."""
    from conftest import make_settings
    from fakes import FakeReleaseSource
    from jvmworld import JavaWorld

    from pumpkin_manager.app import AppContext

    settings = make_settings(tmp_path)
    leftover = settings.cache_dir / "spigot-build" / "dead1234" / "work"
    leftover.mkdir(parents=True)
    (leftover / "big.jar").write_bytes(b"x" * 1000)
    ctx = AppContext(settings, release_source=FakeReleaseSource(), verify_elf=False, background=False, **JavaWorld().app_options())
    await ctx.startup()
    try:
        assert not leftover.exists() and list((settings.cache_dir / "spigot-build").iterdir()) == []
    finally:
        await ctx.shutdown()


# ----------------------------------------------------------------------------------------------------------------------------- backups
async def test_backups_leave_out_what_spigot_unpacks_by_itself(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Saved", "spigot", start=False)
    directory = ctx.servers.server_dir(rec.id)
    (directory / "bundler" / "libraries").mkdir(parents=True)
    (directory / "bundler" / "libraries" / "guava.jar").write_bytes(b"x" * 1000)
    (directory / "plugins").mkdir()
    (directory / "plugins" / "Essentials.jar").write_bytes(b"a plugin")
    (directory / "plugins" / "bundler").mkdir()
    (directory / "plugins" / "bundler" / "keep.txt").write_bytes(b"a plugin's own folder of that name")
    (directory / "world").mkdir()
    (directory / "world" / "level.dat").write_bytes(b"the world")
    row = await finish(ctx, ctx.backups.submit_backup(ctx.servers.get(rec.id), "manual"))
    assert row["status"] == "success", row["message"]
    with tarfile.open(ctx.settings.backup_dir / ctx.repo.list_backups(rec.id)[0]["file_name"]) as tar:
        names = tar.getnames()
    assert not any(n.endswith("guava.jar") for n in names), "what the server unpacks by itself is not in the backup"
    assert any(n.endswith("plugins/bundler/keep.txt") for n in names), "but a folder of that name somewhere else is the person's"
    assert any(n.endswith("plugins/Essentials.jar") for n in names)
    assert any(n.endswith("world/level.dat") for n in names) and any(n.endswith("spigot.jar") for n in names)
    assert ctx.repo.list_backups(rec.id)[0]["build_tag"] == "Spigot 1.21.4 (build 4458)"


# ----------------------------------------------------------------------------------------------------------------------------- updates
async def test_a_newer_build_is_found_by_its_name_and_offered_once(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Current", "spigot", start=False)
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 0, "errors": {}}, "the build SpigotMC offers is the one it runs"
    world.spigot.new_build("1.21.4", "4470")
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 1, "errors": {}}
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["update"]["available"] is True and view["update"]["pending_software"]["label"] == "Spigot 1.21.4 (build 4470)"
    assert view["software"]["pending"]["kind"] == "build" and view["software"]["upgrade"] is None
    await ctx.software_updates.check()
    assert len([a for a in ctx.repo.list_activity(50, rec.id) if "Update available" in a["message"]]) == 1, "announced once"
    world.spigot.new_build("1.21.4", "4471")
    await ctx.software_updates.check()
    assert ctx.servers.view(ctx.servers.get(rec.id))["software"]["pending"]["build"] == "4471", "a still newer build replaces the offer"


async def test_a_newer_minecraft_version_is_shown_but_is_not_an_update(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Older", "spigot", software={"minecraft": "1.21.1"}, start=False)
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["software"]["pending"] is None and view["software"]["upgrade"]["minecraft"] == "1.21.4"


async def test_the_new_build_is_made_while_the_server_runs_and_switched_in_after_the_backup(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Live", "spigot")
    directory = ctx.servers.server_dir(rec.id)
    (directory / "plugins").mkdir(exist_ok=True)
    (directory / "plugins" / "Essentials.jar").write_bytes(b"a plugin")
    old_jar = jar_of(ctx, rec)
    world.spigot.new_build("1.21.4", "4470")
    await ctx.software_updates.check()
    states: list[str] = []
    world.spigot.on_build = lambda version: states.append(ctx.pm.runtime(rec.id).state)

    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    assert states == ["running"], "the server runs while its new build is made"
    steps = steps_of(ctx, row)
    positions = [next(i for i, s in enumerate(steps) if needle in s) for needle in ("Building Spigot 1.21.4 (build 4470)", "Stopping", "Backup created", "Installing", "Starting")]
    assert positions == sorted(positions) and any("five to fifteen minutes" in s for s in steps)

    assert jar_of(ctx, rec) == FakeSpigot.jar("1.21.4", "4470") != old_jar
    assert (directory / "plugins" / "Essentials.jar").read_bytes() == b"a plugin"
    assert not list(ctx.settings.servers_dir.glob(".update-*")) and not build_folders(ctx)
    rec = ctx.servers.get(rec.id)
    assert rec.spec["software"]["build"] == "4470" and "pending" not in rec.spec["software"] and rec.spec["previous"]["software"]["build"] == "4458"
    assert set(rec.spec["previous"]) == set(PREVIOUS_FIELDS)
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert (update_row["status"], update_row["from_tag"], update_row["to_tag"]) == ("success", "1.21.4 (build 4458)", "1.21.4 (build 4470)")
    backup = ctx.repo.get_backup(update_row["backup_id"])
    assert backup["kind"] == "pre-update" and backup["verified"] and backup["build_tag"] == "Spigot 1.21.4 (build 4458)"
    assert ctx.pm.runtime(rec.id).state == "running" and (await ctx.software_updates.check())["servers_with_updates"] == 0


async def test_a_build_that_does_not_start_is_rolled_back_and_not_offered_again(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Bad", "spigot")
    before = jar_of(ctx, rec)
    world.spigot.new_build("1.21.4", "4470", broken=True)
    world.behavior["crash_if_jar_contains"] = "BROKEN"
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success", "the job itself ends normally: the problem was handled"
    assert any("Rolling back" in s for s in steps_of(ctx, row))
    assert jar_of(ctx, rec) == before and ctx.servers.get(rec.id).spec["software"]["build"] == "4458" and ctx.pm.runtime(rec.id).state == "running"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert update_row["status"] == "rolled_back" and "simulated broken build" in update_row["message"]
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0, "that exact build is not offered again"
    world.spigot.new_build("1.21.4", "4471")
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1, "the next one is"


async def test_a_build_that_cannot_be_made_changes_nothing_and_stays_on_offer(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Steady", "spigot")
    before = jar_of(ctx, rec)
    world.spigot.new_build("1.21.4", "4470")
    await ctx.software_updates.check()
    world.spigot.fail = "Could not resolve dependencies for project org.spigotmc:spigot-api"
    row = await update(ctx, rec)
    assert row["status"] == "success"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert update_row["status"] == "failed" and "BuildTools failed" in update_row["message"] and "Could not resolve dependencies" in update_row["message"]
    assert jar_of(ctx, rec) == before and ctx.pm.runtime(rec.id).state == "running", "the server was never touched"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id)) and not build_folders(ctx)
    world.spigot.fail = None
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1, "a build that could not be made is not held against the version"


async def test_switching_to_another_minecraft_version_builds_it_with_its_own_jdk(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Big", "spigot", software={"minecraft": "1.20.4"})
    assert rec.spec["java"] == 17
    row = await update(ctx, rec, "1.21.4")
    assert row["status"] == "success", row["message"]
    assert any("changes Minecraft from 1.20.4 to 1.21.4" in s and "Plugins and mods have to support" in s for s in steps_of(ctx, row))
    rec = ctx.servers.get(rec.id)
    assert (rec.spec["minecraft"], rec.spec["java"], rec.spec["software"]["build"]) == ("1.21.4", 21, "4458")
    assert Path(world.spigot.calls[-1]["argv"][0]) == ctx.jdks.executable(21) and rec.spec["previous"]["java"] == 17
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert (update_row["from_tag"], update_row["to_tag"]) == ("1.20.4 (build 4023)", "1.21.4 (build 4458)")


async def test_going_back_to_the_build_before_an_update_restores_it_from_the_backup(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Undo", "spigot")
    before = jar_of(ctx, rec)
    world.spigot.new_build("1.21.4", "4470")
    await ctx.software_updates.check()
    await update(ctx, rec)
    assert jar_of(ctx, rec) != before
    row = await finish(ctx, ctx.software_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert jar_of(ctx, rec) == before and ctx.servers.get(rec.id).spec["software"]["build"] == "4458"


async def test_an_automatic_update_is_made_when_the_person_chose_that(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Auto", "spigot", start=False, policy={"update_mode": "auto"})
    world.spigot.new_build("1.21.4", "4470")
    await ctx.software_updates.check()
    assert await ctx.software_updates.process_pending() == 1
    await finish(ctx, ctx.repo.list_jobs()[0], timeout=120)
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "4470"
    assert ctx.repo.list_updates(rec.id)[0]["trigger"] == "auto"


async def test_a_spigot_server_that_is_left_to_notify_is_never_updated_by_itself(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Notify", "spigot", start=False)
    world.spigot.new_build("1.21.4", "4470")
    await ctx.software_updates.check()
    assert await ctx.software_updates.process_pending() == 0
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "4458"


async def test_the_builds_and_versions_of_a_spigot_server_can_be_listed(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Lists", "spigot", start=False)
    versions = await ctx.software_updates.versions(rec)
    assert versions["kind"] == "spigot" and versions["current"] == "1.21.4" and [v["id"] for v in versions["versions"]][0] == "1.21.4"
    builds = await ctx.software_updates.builds(rec, "1.21.1")
    assert builds["builds"][0]["build"] == "4205" and builds["current_build"] == "4458" and builds["current"] == "1.21.4"
