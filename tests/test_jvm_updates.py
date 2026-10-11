"""Updating a modpack server: finding newer versions, the backup-first procedure, rollback, and what is never overwritten."""

from __future__ import annotations

import pytest
from conftest import create_java_server, finish
from jvmfakes import FakeMod
from jvmworld import CREATE, LITHIUM

from pumpkin_manager.jvm.packupdates import PREVIOUS_FIELDS
from pumpkin_manager.servers import ServerError

LITHIUM_V2 = FakeMod("mods/lithium.jar", b"lithium-second-edition" * 100)
JADE = FakeMod("mods/jade.jar", b"jade" * 80)


def add_update(world, number: str = "1.1.0", **options):
    """A newer version of the pack: lithium updated, create dropped, jade added, a changed and a new configuration file."""
    options.setdefault("mods", [LITHIUM_V2, JADE])
    options.setdefault("overrides", {"config/create.toml": b"speed = 2\n", "config/new.toml": b"fresh = true\n"})
    options.setdefault("server_overrides", {"server.properties": b"allow-flight=false\n"})
    return world.pack.add_version(number, **options)


def steps_of(ctx, row) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


# ----------------------------------------------------------------------------------- finding updates
async def test_a_newer_version_is_found_and_offered_once(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Offer", start=False)
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "already on the newest version"
    add_update(world)
    result = await ctx.pack_updates.check()
    assert result == {"servers_with_updates": 1, "errors": {}}
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["update"]["available"] is True and view["update"]["pending_pack"]["number"] == "1.1.0"
    assert view["modpack"]["pending"]["id"] == view["update"]["pending_pack"]["id"]

    await ctx.pack_updates.check()
    announcements = [a for a in ctx.repo.list_activity(50, rec.id) if "Update available" in a["message"]]
    assert len(announcements) == 1, "the same update is announced only once"


async def test_a_beta_is_only_offered_to_a_server_that_runs_a_beta(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Stable", start=False)
    add_update(world, "1.1.0-beta", version_type="beta")
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "a release does not move to a beta by itself"
    spec = ctx.servers.get(rec.id).spec
    ctx.repo.update_server(rec.id, spec={**spec, "pack": {**spec["pack"], "version_type": "beta"}})
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 1


async def test_a_server_that_does_not_want_updates_is_left_alone_and_a_failing_lookup_is_reported(jvm):
    world, ctx = jvm
    quiet = await create_java_server(ctx, "Quiet", start=False, policy={"update_mode": "off"})
    add_update(world)
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0 and not ctx.servers.view(quiet)["update"]["available"]
    ctx.repo.update_server(quiet.id, policy={**quiet.policy.model_dump(), "update_mode": "notify"})
    assert (await ctx.pack_updates.check(ctx.servers.get(quiet.id)))["servers_with_updates"] == 1, "a manual check ignores the setting"
    world.net.offline = True
    result = await ctx.pack_updates.check()
    assert result["servers_with_updates"] == 0 and result["errors"], "a failed lookup is reported, not mistaken for 'up to date'"
    assert ctx.servers.view(ctx.servers.get(quiet.id))["update"]["available"], "and the update that was known stays known"


# ------------------------------------------------------------------------------ applying updates
async def test_the_update_stops_backs_up_switches_and_starts_again(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Update")
    directory = ctx.servers.server_dir(rec.id)
    (directory / "config" / "create.toml").write_text("speed = 7\n")  # tuned by hand
    (directory / "mods" / "extra.jar").write_bytes(b"added by hand")
    add_update(world)
    await ctx.pack_updates.check()

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    steps = steps_of(ctx, row)
    positions = [next(i for i, s in enumerate(steps) if needle in s) for needle in ("Stopping", "Backup created", "Switching", "Starting")]
    assert positions == sorted(positions), "the backup is made after the server stopped and before anything changed"

    assert (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM_V2.content and (directory / "mods" / "jade.jar").exists()
    assert not (directory / "mods" / "create.jar").exists(), "a mod the new version dropped is removed"
    assert (directory / "mods" / "extra.jar").read_bytes() == b"added by hand", "what somebody added is not the update's business"
    assert (directory / "config" / "create.toml").read_text() == "speed = 7\n", "an edited configuration file is kept"
    assert (directory / "config" / "create.toml.pack-new").read_text() == "speed = 2\n", "and the pack's new version is saved beside it"
    assert (directory / "config" / "new.toml").exists()
    assert "allow-flight=true" in (directory / "server.properties").read_text(), "server.properties is never replaced by an update"
    assert any("were left alone" in s or "configuration file(s) you changed" in s for s in steps)

    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_number"] == "1.1.0" and "pending" not in rec.spec["pack"]
    assert rec.spec["previous"]["pack"]["version_number"] == "1.0.0" and set(rec.spec["previous"]) == set(PREVIOUS_FIELDS)
    lock = ctx.servers.jvm.lock_of(rec)
    assert set(lock["files"]) == {"mods/lithium.jar", "mods/jade.jar"} and lock["version_id"] == "1.1.0"
    update = ctx.repo.list_updates(rec.id)[0]
    assert (update["status"], update["from_tag"], update["to_tag"]) == ("success", "1.0.0", "1.1.0") and update["backup_id"]
    backup = ctx.repo.get_backup(update["backup_id"])
    assert backup["kind"] == "pre-update" and backup["verified"] and backup["build_tag"] == "Cool Pack 1.0.0"
    assert ctx.pm.runtime(rec.id).state == "running" and ctx.servers.view(rec)["update"]["available"] is False
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0


async def test_a_version_that_does_not_start_is_rolled_back_automatically(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Bad")
    directory = ctx.servers.server_dir(rec.id)
    add_update(world)
    await ctx.pack_updates.check()
    world.behavior["crash_if_present"] = "mods/jade.jar"  # the new version's new mod conflicts with something

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", "the job itself ends normally: the problem was handled"
    steps = steps_of(ctx, row)
    assert any("Update failed" in s for s in steps) and any("Rolling back" in s for s in steps)

    assert (directory / "mods" / "create.jar").read_bytes() == CREATE.content and (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM.content
    assert not (directory / "mods" / "jade.jar").exists()
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_number"] == "1.0.0" and "previous" not in rec.spec
    assert ctx.pm.runtime(rec.id).state == "running", "the old version is running again"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "rolled_back" and "simulated mod conflict" in update["message"], "the history says why"
    assert any("rolled back" in a["message"] for a in ctx.repo.list_activity(30, rec.id))
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "a version that failed is not offered again"


async def test_a_download_that_fails_changes_nothing_and_the_update_stays_on_offer(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Net")
    directory = ctx.servers.server_dir(rec.id)
    add_update(world)
    await ctx.pack_updates.check()
    world.net.routes.pop(("cdn.modrinth.com", JADE.url.split("cdn.modrinth.com", 1)[1]))  # the new mod has vanished from the CDN

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "failed" and "jade.jar" in update["message"]
    assert (directory / "mods" / "create.jar").exists() and ctx.pm.runtime(rec.id).state == "running", "the server was never touched"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id)), "it failed before the backup was needed"
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 1, "a download problem is not held against the version"


async def test_an_update_to_another_minecraft_and_loader_brings_its_java_and_loader(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Big")
    world.net.maven_installer("forge", "1.20.1", "47.2.0")
    world.pack.add_version("2.0.0", minecraft="1.20.1", loader=("forge", "47.2.0"), mods=[LITHIUM_V2])
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert any("changes Minecraft from 1.21.1 to 1.20.1" in s for s in steps_of(ctx, row))
    rec = ctx.servers.get(rec.id)
    assert (rec.spec["minecraft"], rec.spec["java"], rec.spec["loader"]) == ("1.20.1", 17, {"kind": "forge", "version": "47.2.0"})
    assert rec.spec["launch"]["argfile"].startswith("libraries/net/minecraftforge/forge/1.20.1-47.2.0/")
    assert ctx.runtimes.get(17) is not None and ctx.pm.runtime(rec.id).state == "running"
    assert rec.spec["previous"]["java"] == 21 and rec.spec["previous"]["loader"]["kind"] == "neoforge"


async def test_a_specific_version_can_be_chosen_and_the_same_one_is_refused(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Pick", start=False)
    second = add_update(world)
    with pytest.raises(ServerError, match="no newer version"):
        ctx.pack_updates.submit_update(ctx.servers.get(rec.id))
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id), second["id"]), timeout=120)
    assert row["status"] == "success", row["message"]
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id), second["id"]))
    assert row["status"] == "failed" and "already runs that version" in row["message"]
    available = await ctx.pack_updates.available(ctx.servers.get(rec.id))
    assert available["current"] == second["id"] and [v["number"] for v in available["versions"]] == ["1.1.0", "1.0.0"]


async def test_only_modrinth_packs_can_be_updated_and_an_unknown_installation_is_not_guessed_at(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Plain", start=False)
    uploaded = {**rec.spec, "pack": {"source": "upload", "title": "Mine", "version_number": "1"}}
    ctx.repo.update_server(rec.id, spec=uploaded)
    with pytest.raises(ServerError, match="Only modpacks from Modrinth"):
        ctx.pack_updates.submit_update(ctx.servers.get(rec.id), "x")
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0

    other = await create_java_server(ctx, "Lockless", start=False)
    second = add_update(world)
    (ctx.servers.server_dir(other.id) / ".manager" / "pack.json").unlink()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(other.id), second["id"]))
    assert row["status"] == "failed" and "no record of the files" in row["message"]


# ----------------------------------------------------------------------------------- rolling back
async def test_the_last_update_can_be_undone_from_the_backup_made_before_it(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Undo")
    directory = ctx.servers.server_dir(rec.id)
    add_update(world)
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["can_roll_back"] is True

    row = await finish(ctx, ctx.pack_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_number"] == "1.0.0" and "previous" not in rec.spec
    assert (directory / "mods" / "create.jar").read_bytes() == CREATE.content and not (directory / "mods" / "jade.jar").exists()
    assert ctx.pm.runtime(rec.id).state == "running"
    history = ctx.repo.list_updates(rec.id)
    assert (history[0]["trigger"], history[0]["status"], history[0]["to_tag"]) == ("rollback", "success", "1.0.0")
    assert any(b["kind"] == "pre-restore" for b in ctx.repo.list_backups(rec.id)), "the state before going back is kept as well"
    with pytest.raises(ServerError, match="no earlier modpack version"):
        ctx.pack_updates.submit_rollback(rec)
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "the version that was undone is not pushed again"
