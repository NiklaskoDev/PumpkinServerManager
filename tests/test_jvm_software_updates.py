"""Updating Vanilla, Paper and Fabric servers: finding newer builds, the backup-first procedure, rollback, and what is never done by itself."""

from __future__ import annotations

import pytest
from conftest import create_java_server, create_software_server, finish
from jvmfakes import server_jar

from pumpkin_manager.jvm.softwareupdates import PREVIOUS_FIELDS, DowngradeNotConfirmed
from pumpkin_manager.servers import ServerError


def steps_of(ctx, row) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


async def update(ctx, rec, minecraft=None, build=None, *, confirm=False, timeout=120):
    """Prepare and run an update the way the API does; returns the finished job."""
    fresh = ctx.servers.get(rec.id)
    target = await ctx.software_updates.prepare(fresh, minecraft, build, confirm_downgrade=confirm)
    return await finish(ctx, ctx.software_updates.submit_update(fresh, target), timeout=timeout)


async def older_paper(ctx, name="Paper", **options):
    """A Paper server on build 10 of 1.21.4 (build 11 is the newest stable one)."""
    return await create_software_server(ctx, name, "paper", software={"minecraft": "1.21.4", "build": "10", **options.pop("software", {})}, **options)


def jar_of(ctx, rec, name="paper.jar") -> bytes:
    return (ctx.servers.server_dir(rec.id) / name).read_bytes()


# ----------------------------------------------------------------------------------------------- finding updates
async def test_a_newer_stable_build_is_found_and_offered_once(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False)
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 1, "errors": {}}
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["update"]["available"] is True and view["update"]["pending_software"]["label"] == "Paper 1.21.4 (build 11)"
    assert view["software"]["pending"]["kind"] == "build" and view["software"]["upgrade"] is None and view["update"]["pending_pack"] is None

    await ctx.software_updates.check()
    announcements = [a for a in ctx.repo.list_activity(50, rec.id) if "Update available" in a["message"]]
    assert len(announcements) == 1, "the same update is announced only once"

    world.paper.add_build("1.21.4", 13, "STABLE")
    await ctx.software_updates.check()
    assert ctx.servers.view(ctx.servers.get(rec.id))["software"]["pending"]["build"] == "13", "a still newer build replaces the offer"


async def test_experimental_builds_are_never_offered(jvm):
    world, ctx = jvm
    await create_software_server(ctx, "Current", "paper", software={"minecraft": "1.21.4"}, start=False)  # build 11; build 12 is experimental
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0


async def test_a_newer_minecraft_version_is_shown_but_is_not_an_update_for_paper_or_fabric(jvm):
    world, ctx = jvm
    paper = await create_software_server(ctx, "OldPaper", "paper", software={"minecraft": "1.21.1"}, start=False)
    fabric = await create_software_server(ctx, "OldFabric", "fabric", software={"minecraft": "1.21.1"}, start=False)
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 0, "errors": {}}
    for rec in (paper, fabric):
        view = ctx.servers.view(ctx.servers.get(rec.id))
        assert view["software"]["pending"] is None and view["update"]["available"] is False
        assert view["software"]["upgrade"]["minecraft"] == "1.21.4", "offered, but a person decides: plugins and mods must follow"


async def test_vanilla_has_no_builds_so_a_newer_release_is_its_update(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Pure", "vanilla", software={"minecraft": "1.20.4"}, start=False)
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["update"]["available"] is True and view["software"]["pending"]["kind"] == "minecraft"
    assert view["software"]["pending"]["label"] == "Vanilla 1.21.4" and view["software"]["upgrade"] is None
    current = await create_software_server(ctx, "Latest", "vanilla", software={"minecraft": "1.21.4"}, start=False)
    await ctx.software_updates.check()
    assert ctx.servers.view(ctx.servers.get(current.id))["update"]["available"] is False


async def test_a_vanilla_snapshot_is_offered_newer_snapshots_and_releases(jvm):
    world, ctx = jvm
    snap = await create_software_server(ctx, "Snap", "vanilla", software={"minecraft": "1.21.5-snapshot-1"}, start=False)
    assert snap.spec["software"]["channel"] == "snapshot"
    world.net.minecraft({"1.21.5": 21, "1.21.4": 21}, snapshots={"1.21.6-snapshot-1": 21, "1.21.5-snapshot-1": 21})
    ctx.software.invalidate()
    await ctx.software_updates.check()
    assert ctx.servers.view(ctx.servers.get(snap.id))["software"]["pending"]["minecraft"] == "1.21.6-snapshot-1"


async def test_a_server_that_does_not_want_updates_is_left_alone_and_a_manual_check_ignores_that(jvm):
    world, ctx = jvm
    quiet = await older_paper(ctx, "Quiet", start=False, policy={"update_mode": "off"})
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0 and not ctx.servers.view(quiet)["update"]["available"]
    assert (await ctx.software_updates.check(ctx.servers.get(quiet.id)))["servers_with_updates"] == 1, "a manual check ignores the setting"
    assert ctx.servers.view(ctx.servers.get(quiet.id))["update"]["available"] is True
    await ctx.software_updates.check()
    assert not ctx.servers.view(ctx.servers.get(quiet.id))["update"]["available"], "the scheduled check clears it again for a server set to off"


async def test_a_failing_lookup_is_reported_and_does_not_hide_what_was_known(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False)
    await ctx.software_updates.check()
    world.net.offline = True
    result = await ctx.software_updates.check()
    assert result["servers_with_updates"] == 0 and rec.id in result["errors"] and "Could not reach" in result["errors"][rec.id]
    assert ctx.servers.view(ctx.servers.get(rec.id))["update"]["available"] is True, "the update that was known stays known"


async def test_modpack_servers_are_not_the_business_of_the_software_updater(jvm):
    world, ctx = jvm
    modded = await create_java_server(ctx, "Packed", start=False)
    assert (await ctx.software_updates.check()) == {"servers_with_updates": 0, "errors": {}}
    with pytest.raises(ServerError, match="does not run server software"):
        await ctx.software_updates.prepare(modded, None, None)


async def test_a_server_that_is_being_worked_on_is_skipped_by_the_scheduled_check(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False)
    job = ctx.servers.submit_start(rec)  # busy right now
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0
    await finish(ctx, job)


# ------------------------------------------------------------------------------------------- choosing a target
async def test_nothing_that_makes_no_sense_is_prepared(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Sense", "paper", software={"minecraft": "1.21.4"}, start=False)
    prepare = ctx.software_updates.prepare
    with pytest.raises(ServerError, match="no newer version to install"):
        await prepare(rec, None, None)
    with pytest.raises(ServerError, match="already runs that version"):
        await prepare(rec, "1.21.4", "11")
    with pytest.raises(Exception, match="has no build 99"):
        await prepare(rec, None, "99")
    with pytest.raises(Exception, match="Paper has no builds for Minecraft 1.7.10"):
        await prepare(rec, "1.7.10", None)


async def test_the_newest_stable_build_of_another_minecraft_version_is_what_a_version_switch_means(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Switch", "paper", software={"minecraft": "1.21.1"}, start=False)
    target = await ctx.software_updates.prepare(rec, "1.21.4", None)
    assert (target.minecraft, target.build) == ("1.21.4", "11")


async def test_an_older_minecraft_version_has_to_be_confirmed(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Back", "paper", software={"minecraft": "1.21.4"}, start=False)
    with pytest.raises(DowngradeNotConfirmed, match="older than the 1.21.4") as caught:
        await ctx.software_updates.prepare(rec, "1.21.1", None)
    assert caught.value.status == 409 and caught.value.code == "downgrade"
    older = await ctx.software_updates.prepare(rec, "1.21.1", None, confirm_downgrade=True)
    assert older.minecraft == "1.21.1"
    assert (await ctx.software_updates.prepare(rec, None, "10")).build == "10", "an older build of the same version needs no confirmation"


# ----------------------------------------------------------------------------------------- applying updates
async def test_the_update_stops_backs_up_switches_and_starts_again(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx)
    directory = ctx.servers.server_dir(rec.id)
    (directory / "plugins").mkdir()
    (directory / "plugins" / "Essentials.jar").write_bytes(b"a plugin")
    (directory / "world" / "region").mkdir(parents=True, exist_ok=True)
    old_jar = jar_of(ctx, rec)
    await ctx.software_updates.check()

    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    steps = steps_of(ctx, row)
    positions = [next(i for i, s in enumerate(steps) if needle in s) for needle in ("Downloading", "Stopping", "Backup created", "Installing", "Starting")]
    assert positions == sorted(positions), "the new build is fetched while the server runs; the backup is made after it stopped and before anything changed"

    assert jar_of(ctx, rec) != old_jar and b'"build": 11' in jar_of(ctx, rec)
    assert (directory / "plugins" / "Essentials.jar").read_bytes() == b"a plugin" and (directory / "world").is_dir()
    assert not list(ctx.settings.servers_dir.glob(".update-*")), "the staging folder is gone"

    rec = ctx.servers.get(rec.id)
    assert rec.spec["software"]["build"] == "11" and "pending" not in rec.spec["software"] and rec.spec["minecraft"] == "1.21.4"
    assert rec.spec["previous"]["software"]["build"] == "10" and set(rec.spec["previous"]) == set(PREVIOUS_FIELDS)
    assert "pending" not in rec.spec["previous"]["software"], "what a check found is not part of what the server was"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert (update_row["status"], update_row["from_tag"], update_row["to_tag"]) == ("success", "1.21.4 (build 10)", "1.21.4 (build 11)")
    backup = ctx.repo.get_backup(update_row["backup_id"])
    assert backup["kind"] == "pre-update" and backup["verified"] and backup["build_tag"] == "Paper 1.21.4 (build 10)"
    assert ctx.pm.runtime(rec.id).state == "running" and ctx.servers.view(rec)["update"]["available"] is False
    assert ctx.servers.view(rec)["software"]["can_roll_back"] is True
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0
    assert any("updated to Paper 1.21.4 (build 11)" in a["message"] for a in ctx.repo.list_activity(30, rec.id))


async def test_a_stopped_server_is_updated_and_stays_stopped(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False)
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "11" and ctx.pm.runtime(rec.id).state == "stopped"


async def test_a_build_that_does_not_start_is_rolled_back_automatically(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Bad", "paper", software={"minecraft": "1.21.4"})
    before = jar_of(ctx, rec)
    world.paper.add_build("1.21.4", 13, "STABLE", broken=True)
    world.behavior["crash_if_jar_contains"] = "BROKEN"
    await ctx.software_updates.check()

    row = await update(ctx, rec)
    assert row["status"] == "success", "the job itself ends normally: the problem was handled"
    steps = steps_of(ctx, row)
    assert any("Update failed" in s for s in steps) and any("Rolling back" in s for s in steps)
    assert jar_of(ctx, rec) == before, "the old build is back"
    rec = ctx.servers.get(rec.id)
    assert rec.spec["software"]["build"] == "11" and "previous" not in rec.spec
    assert ctx.pm.runtime(rec.id).state == "running", "the old build is running again"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert update_row["status"] == "rolled_back" and "simulated broken build" in update_row["message"], "the history says why"
    assert any("rolled back" in a["message"] for a in ctx.repo.list_activity(30, rec.id))
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0, "a build that failed is not offered again"
    world.paper.add_build("1.21.4", 14, "STABLE")
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1, "but the next one is"


async def test_a_download_that_fails_changes_nothing_and_the_update_stays_on_offer(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx)
    before = jar_of(ctx, rec)
    await ctx.software_updates.check()
    newest = world.paper.versions["1.21.4"]["builds"][1]  # build 11
    world.paper.blobs.pop(newest["downloads"]["server:default"]["checksums"]["sha256"])  # gone from the download host

    row = await update(ctx, rec)
    assert row["status"] == "success"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert update_row["status"] == "failed" and "not there any more" in update_row["message"]
    assert jar_of(ctx, rec) == before and ctx.pm.runtime(rec.id).state == "running", "the server was never touched"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id)), "it failed before a backup was needed"
    assert (await ctx.software_updates.check())["servers_with_updates"] == 1, "a download problem is not held against the build"
    assert ctx.repo.get_kv(f"autofails:{rec.id}") == "1"


async def test_a_file_that_does_not_match_its_checksum_is_never_installed_and_not_offered_again(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False)
    before = jar_of(ctx, rec)
    await ctx.software_updates.check()
    sha = world.paper.versions["1.21.4"]["builds"][1]["downloads"]["server:default"]["checksums"]["sha256"]
    world.paper.blobs[sha] = b"PK\x03\x04 swapped on the way"
    row = await update(ctx, rec)
    assert row["status"] == "success" and "SHA256" in ctx.repo.list_updates(rec.id)[0]["message"]
    assert jar_of(ctx, rec) == before
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0, "this exact build is not tried again"


async def test_switching_to_another_minecraft_version_brings_its_java_and_build(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Big", "paper", software={"minecraft": "1.20.4"})
    assert rec.spec["java"] == 17
    row = await update(ctx, rec, "1.21.4")
    assert row["status"] == "success", row["message"]
    assert any("changes Minecraft from 1.20.4 to 1.21.4" in s and "Plugins and mods have to support" in s for s in steps_of(ctx, row))
    rec = ctx.servers.get(rec.id)
    assert (rec.spec["minecraft"], rec.spec["java"], rec.spec["software"]["build"]) == ("1.21.4", 21, "11")
    assert rec.spec["previous"]["java"] == 17 and rec.spec["previous"]["minecraft"] == "1.20.4"
    assert ctx.runtimes.get(21) is not None and ctx.pm.runtime(rec.id).state == "running"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert (update_row["from_tag"], update_row["to_tag"]) == ("1.20.4 (build 5)", "1.21.4 (build 11)")


async def test_going_back_to_an_older_version_works_once_confirmed(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Down", "paper", software={"minecraft": "1.21.4"})
    row = await update(ctx, rec, "1.20.4", confirm=True)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert (rec.spec["minecraft"], rec.spec["java"]) == ("1.20.4", 17) and ctx.pm.runtime(rec.id).state == "running"


async def test_a_vanilla_server_is_updated_to_a_newer_release(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Pure", "vanilla", software={"minecraft": "1.20.4"})
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    assert jar_of(ctx, rec, "server.jar") == server_jar("1.21.4")
    rec = ctx.servers.get(rec.id)
    assert (rec.spec["minecraft"], rec.spec["java"], rec.spec["software"]["build"]) == ("1.21.4", 21, "")
    assert ctx.repo.list_updates(rec.id)[0]["backup_id"] and ctx.pm.runtime(rec.id).state == "running"


# ------------------------------------------------------------------------------------------------ Fabric
async def test_a_fabric_loader_update_replaces_the_launcher_and_keeps_fabric_api(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Loader", "fabric", software={"minecraft": "1.21.1", "build": "0.16.9"})
    directory = ctx.servers.server_dir(rec.id)
    api_file = directory / rec.spec["software"]["fabric_api"]["file"]
    api_bytes = api_file.read_bytes()
    before = jar_of(ctx, rec, "fabric-server-launch.jar")
    await ctx.software_updates.check()
    assert ctx.servers.view(ctx.servers.get(rec.id))["software"]["pending"]["label"] == "Fabric 1.21.1 (loader 0.16.10)"
    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    assert jar_of(ctx, rec, "fabric-server-launch.jar") != before and b"0.16.10" in jar_of(ctx, rec, "fabric-server-launch.jar")
    assert api_file.read_bytes() == api_bytes and ctx.servers.get(rec.id).spec["software"]["fabric_api"]["file"] == api_file.relative_to(directory).as_posix()
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "0.16.10"


async def test_switching_fabric_to_another_minecraft_version_brings_the_matching_fabric_api(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Api", "fabric", software={"minecraft": "1.21.1"})
    directory = ctx.servers.server_dir(rec.id)
    (directory / "mods" / "my-own-mod.jar").write_bytes(b"somebody's mod")
    old_api = directory / rec.spec["software"]["fabric_api"]["file"]
    row = await update(ctx, rec, "1.21.4")
    assert row["status"] == "success", row["message"]
    assert not old_api.exists(), "the Fabric API of the old version is replaced"
    new_api = ctx.servers.get(rec.id).spec["software"]["fabric_api"]
    assert new_api["number"] == "0.110.0+1.21.4" and (directory / new_api["file"]).is_file()
    assert (directory / "mods" / "my-own-mod.jar").read_bytes() == b"somebody's mod", "other mods are the person's business"
    assert any("Fabric API is now 0.110.0+1.21.4" in s for s in steps_of(ctx, row))


async def test_fabric_api_missing_for_the_new_version_stops_the_update_before_anything_changes(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "NoApi", "fabric", software={"minecraft": "1.21.1"})
    world.fabric_api.versions = [v for v in world.fabric_api.versions if "1.21.4" not in v["game_versions"]]
    row = await update(ctx, rec, "1.21.4")
    assert row["status"] == "success"
    update_row = ctx.repo.list_updates(rec.id)[0]
    assert update_row["status"] == "failed" and "Fabric API has no version for Minecraft 1.21.4 yet" in update_row["message"]
    assert ctx.servers.get(rec.id).spec["minecraft"] == "1.21.1" and ctx.pm.runtime(rec.id).state == "running"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id))


async def test_a_fabric_api_the_person_changed_is_left_alone(jvm):
    world, ctx = jvm
    rec = await create_software_server(ctx, "Mine", "fabric", software={"minecraft": "1.21.1"}, start=False)
    directory = ctx.servers.server_dir(rec.id)
    api_file = directory / rec.spec["software"]["fabric_api"]["file"]
    api_file.write_bytes(b"my own build of fabric api")
    row = await update(ctx, rec, "1.21.4")
    assert row["status"] == "success", row["message"]
    assert api_file.read_bytes() == b"my own build of fabric api", "not deleted"
    assert "fabric_api" not in ctx.servers.get(rec.id).spec["software"], "and not managed any more"
    assert any("changed by hand" in s for s in steps_of(ctx, row))
    assert not (directory / "mods" / "fabric-api-0.110.0+1.21.4.jar").exists()


# ------------------------------------------------------------------------------------------------- rollback
async def test_the_last_update_can_be_undone_from_the_backup_made_before_it(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx)
    directory = ctx.servers.server_dir(rec.id)
    before = jar_of(ctx, rec)
    (directory / "plugins").mkdir()
    (directory / "plugins" / "kept.txt").write_text("before the update")
    await ctx.software_updates.check()
    row = await update(ctx, rec)
    assert row["status"] == "success", row["message"]
    (directory / "plugins" / "kept.txt").write_text("after the update")

    row = await finish(ctx, ctx.software_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert jar_of(ctx, rec) == before and rec.spec["software"]["build"] == "10" and "previous" not in rec.spec
    assert (directory / "plugins" / "kept.txt").read_text() == "before the update", "plugins and the world come back with the backup"
    assert ctx.pm.runtime(rec.id).state == "running"
    history = ctx.repo.list_updates(rec.id)
    assert (history[0]["trigger"], history[0]["status"], history[0]["to_tag"]) == ("rollback", "success", "1.21.4 (build 10)")
    assert any(b["kind"] == "pre-restore" for b in ctx.repo.list_backups(rec.id)), "the state before going back is kept as well"
    with pytest.raises(ServerError, match="no earlier version"):
        ctx.software_updates.submit_rollback(rec)
    assert (await ctx.software_updates.check())["servers_with_updates"] == 0, "the build that was undone is not pushed again"
    assert ctx.servers.view(rec)["software"]["can_roll_back"] is False


# ---------------------------------------------------------------------------------------- automatic updates
async def test_new_builds_are_installed_by_themselves_for_servers_set_to_auto(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx)  # Paper defaults to automatic builds
    assert rec.policy.update_mode == "auto"
    assert await ctx.software_updates.process_pending() == 0, "nothing is pending yet"
    await ctx.software_updates.check()
    assert await ctx.software_updates.process_pending() == 1
    await wait_for_idle(ctx, rec)
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "11" and ctx.repo.list_updates(rec.id)[0]["trigger"] == "auto"
    assert ctx.pm.runtime(rec.id).state == "running"


async def wait_for_idle(ctx, rec) -> None:
    from conftest import wait_until

    await wait_until(lambda: not ctx.jobs.is_busy(rec.id), 120, what="the update to finish")


async def test_a_new_minecraft_version_is_never_installed_by_itself(jvm):
    world, ctx = jvm
    vanilla = await create_software_server(ctx, "Vanilla", "vanilla", software={"minecraft": "1.20.4"}, start=False, policy={"update_mode": "auto"})
    paper = await create_software_server(ctx, "Paper", "paper", software={"minecraft": "1.21.1"}, start=False)
    assert paper.policy.update_mode == "auto"
    await ctx.software_updates.check()
    assert ctx.servers.get(vanilla.id).spec["software"]["pending"] and ctx.servers.get(paper.id).spec["software"]["upgrade"]
    assert await ctx.software_updates.process_pending() == 0
    assert ctx.repo.list_updates(vanilla.id) == [] and ctx.repo.list_updates(paper.id) == []


async def test_servers_that_only_want_to_be_told_are_not_updated_by_themselves(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False, policy={"update_mode": "notify"})
    await ctx.software_updates.check()
    assert await ctx.software_updates.process_pending() == 0 and ctx.servers.get(rec.id).spec["software"]["build"] == "10"


async def test_automatic_updates_wait_while_players_are_online(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx)
    await ctx.software_updates.check()
    ctx.pm.runtime(rec.id).players_online = 2
    assert await ctx.software_updates.process_pending() == 0, "somebody is playing: the update waits"
    ctx.pm.runtime(rec.id).players_online = 0
    assert await ctx.software_updates.process_pending() == 1
    await wait_for_idle(ctx, rec)
    assert ctx.servers.get(rec.id).spec["software"]["build"] == "11"


async def test_a_failed_automatic_update_waits_before_the_next_attempt(jvm):
    world, ctx = jvm
    rec = await older_paper(ctx, start=False)
    await ctx.software_updates.check()
    sha = world.paper.versions["1.21.4"]["builds"][1]["downloads"]["server:default"]["checksums"]["sha256"]
    blob = world.paper.blobs.pop(sha)
    assert await ctx.software_updates.process_pending() == 1
    await wait_for_idle(ctx, rec)
    assert ctx.repo.get_kv(f"autofails:{rec.id}") == "1" and ctx.repo.list_updates(rec.id)[0]["status"] == "failed"
    assert any("next automatic attempt is in about 30 minutes" in a["message"] for a in ctx.repo.list_activity(30, rec.id))
    world.paper.blobs[sha] = blob
    assert await ctx.software_updates.process_pending() == 0, "it does not hammer a service that just failed"


# ------------------------------------------------------------------------------------------------ housekeeping
async def test_the_leftovers_of_an_interrupted_update_are_removed_when_the_manager_starts(jvm):
    world, ctx = jvm
    leftover = ctx.settings.servers_dir / ".update-something-1a2b3c4d"
    leftover.mkdir()
    (leftover / "paper.jar").write_bytes(b"half")
    ctx.servers.reconcile()
    assert not leftover.exists()
