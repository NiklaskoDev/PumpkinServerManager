"""Updating servers made from CurseForge packs: finding versions, the backup-first procedure, rollback, server packs, files to add by hand."""

from __future__ import annotations

import pytest
import pytest_asyncio
from cftools import (
    JADE,
    LITHIUM_V2,
    NEW,
    NEWEST,
    OLD,
    PACK,
    SECRET,
    SERVER_FILES,
    add_update,
    create_cf,
    files_under,
    give_key,
    standard_pack,
    steps_of,
)
from conftest import finish
from jvmfakes import FakeMod
from jvmworld import CREATE, LITHIUM

from pumpkin_manager.servers import ServerError


@pytest_asyncio.fixture
async def cfjvm(jvm):
    world, ctx = jvm
    give_key(ctx, world.cf.key)
    return world, ctx


# ----------------------------------------------------------------------------------- finding updates
async def test_a_newer_curseforge_version_is_found_and_offered_once(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Offer")
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "already on the newest version"

    add_update(world)
    world.cf.add_pack_version(PACK, 5200, "1.2.0-beta", mods=((2001, 3001),), date=NEWEST, release=2)
    assert await ctx.pack_updates.check() == {"servers_with_updates": 1, "errors": {}}
    view = ctx.servers.view(ctx.servers.get(rec.id))
    assert view["update"]["available"] is True and view["update"]["pending_pack"]["number"] == "1.1.0", "a release is not moved to a beta"
    assert view["update"]["pending_pack"]["id"] == "5100"

    await ctx.pack_updates.check()
    assert len([a for a in ctx.repo.list_activity(50, rec.id) if "Update available" in a["message"]]) == 1, "announced only once"
    available = await ctx.pack_updates.available(ctx.servers.get(rec.id))
    assert available["current"] == "5000" and [v["id"] for v in available["versions"]] == ["5200", "5100", "5000"]


async def test_a_failing_curseforge_lookup_is_reported_not_mistaken_for_up_to_date(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Offline")
    add_update(world)
    world.net.offline = True
    result = await ctx.pack_updates.check()
    assert result["servers_with_updates"] == 0 and result["errors"]
    world.net.offline = False
    give_key(ctx, "a-key-that-is-not-valid")
    result = await ctx.pack_updates.check()
    assert result["servers_with_updates"] == 0 and "did not accept the API key" in next(iter(result["errors"].values()))
    give_key(ctx, world.cf.key)
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 1


# ------------------------------------------------------------------------------ the list of files
async def test_the_update_stops_backs_up_switches_and_starts_again(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Update", start=True)
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
    assert (directory / "mods" / "extra.jar").read_bytes() == b"added by hand"
    assert (directory / "config" / "create.toml").read_text() == "speed = 7\n" and (directory / "config" / "create.toml.pack-new").read_text() == "speed = 2\n"
    assert (directory / "config" / "new.toml").exists()

    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "5100" and rec.spec["pack"]["source"] == "curseforge" and "pending" not in rec.spec["pack"]
    assert rec.spec["previous"]["pack"]["version_id"] == "5000"
    lock = ctx.servers.jvm.lock_of(rec)
    assert set(lock["files"]) == {"mods/lithium.jar", "mods/jade.jar"} and (lock["source"], lock["mode"]) == ("curseforge", "files")
    assert all(v.startswith("sha1:") for v in lock["files"].values())
    update = ctx.repo.list_updates(rec.id)[0]
    assert (update["status"], update["from_tag"], update["to_tag"]) == ("success", "1.0.0", "1.1.0") and update["backup_id"]
    assert ctx.repo.get_backup(update["backup_id"])["verified"]
    assert ctx.pm.runtime(rec.id).state == "running"
    assert files_under(ctx.settings.cache_dir / "mods") == [] and files_under(ctx.settings.cache_dir / "uploads") == [], "nothing from CurseForge is kept"
    assert world.net.keys_sent_to == {"api.curseforge.com", "edge.forgecdn.net"}

    # and the update can be undone from the backup made before it
    row = await finish(ctx, ctx.pack_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "5000" and "previous" not in rec.spec
    assert (directory / "mods" / "create.jar").read_bytes() == CREATE.content and (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM.content
    assert not (directory / "mods" / "jade.jar").exists() and ctx.pm.runtime(rec.id).state == "running"
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "the version that was undone is not pushed again"


async def test_a_curseforge_version_that_does_not_start_is_rolled_back_automatically(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Bad", start=True)
    directory = ctx.servers.server_dir(rec.id)
    add_update(world)
    await ctx.pack_updates.check()
    world.behavior["crash_if_present"] = "mods/jade.jar"  # the new version's new mod conflicts with something

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", "the job itself ends normally: the problem was handled"
    assert any("Rolling back" in s for s in steps_of(ctx, row))
    assert (directory / "mods" / "create.jar").read_bytes() == CREATE.content and not (directory / "mods" / "jade.jar").exists()
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "5000" and ctx.pm.runtime(rec.id).state == "running"
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "rolled_back"
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "a version that failed is not offered again"
    assert files_under(ctx.settings.cache_dir / "mods") == [], "the files of the failed update are gone"


async def test_a_file_that_does_not_match_its_checksum_stops_the_update_before_anything_changes(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Tampered", start=True)
    add_update(world)
    await ctx.pack_updates.check()
    world.net.blob(world.cf.files[3005]["downloadUrl"], b"not what CurseForge listed")

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "failed" and "jade.jar" in update["message"]
    assert (ctx.servers.server_dir(rec.id) / "mods" / "create.jar").exists() and ctx.pm.runtime(rec.id).state == "running", "never touched"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id))
    assert files_under(ctx.settings.cache_dir / "mods") == []


async def test_an_update_that_needs_mods_added_by_hand_waits_until_they_are_there(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Hand", start=True)
    directory = ctx.servers.server_dir(rec.id)
    add_update(world)
    world.cf.add_mod(2004, 3004, SECRET, name="Secret Sauce", downloadable=False)
    world.cf.add_pack_version(PACK, 5100, "1.1.0", mods=((2001, 3011), (2005, 3005), (2004, 3004)), date=NEW)
    await ctx.pack_updates.check()

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "failed" and "have to be added by hand" in update["message"] and "Secret Sauce" in update["message"]
    assert (directory / "mods" / "create.jar").exists() and ctx.pm.runtime(rec.id).state == "running", "nothing was changed"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id))
    assert ctx.servers.view(ctx.servers.get(rec.id))["update"]["available"], "and the update stays on offer"

    (directory / "mods" / "secret.jar").write_bytes(SECRET.content)
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "success"
    assert (directory / "mods" / "secret.jar").read_bytes() == SECRET.content and (directory / "mods" / "jade.jar").exists()
    lock = ctx.servers.jvm.lock_of(ctx.servers.get(rec.id))
    assert "mods/secret.jar" not in lock["files"] and [m["path"] for m in lock["manual"]] == ["mods/secret.jar"]
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["manual"] == []


async def test_a_mod_added_by_hand_for_the_old_version_is_pointed_out_when_the_new_version_wants_another_file(cfjvm):
    world, ctx = cfjvm
    cf = world.cf
    cf.add_mod(2001, 3001, LITHIUM)
    cf.add_mod(2004, 3004, SECRET, name="Secret Sauce", downloadable=False)
    cf.add_project(PACK, "Alpha Pack", class_id=4471)
    cf.add_pack_version(PACK, 5000, "1.0.0", mods=((2001, 3001), (2004, 3004)), date=OLD)
    rec, _ = await create_cf(ctx, "Renamed", start=True)
    directory = ctx.servers.server_dir(rec.id)
    (directory / "mods" / "secret.jar").write_bytes(SECRET.content)  # added by hand, as the dashboard asked

    newer = FakeMod("mods/secret-2.jar", b"secret, the second edition" * 20)  # the new version of that mod is a file with another name
    cf.add_mod(2004, 3014, newer, name="Secret Sauce", downloadable=False)
    cf.add_pack_version(PACK, 5100, "1.1.0", mods=((2001, 3001), (2004, 3014)), date=NEW)
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success"
    waiting = ctx.repo.list_updates(rec.id)[0]
    assert waiting["status"] == "failed" and "have to be added by hand" in waiting["message"] and "Secret Sauce" in waiting["message"]
    (directory / "mods" / "secret-2.jar").write_bytes(newer.content)
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "success"
    assert any("added by hand for the old version" in s and "mods/secret.jar" in s for s in steps_of(ctx, row))
    assert (directory / "mods" / "secret.jar").is_file(), "it is the person's file: pointed out, not removed"


async def test_a_server_installed_from_the_list_stays_on_the_list(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "List")
    add_update(world, server_pack=SERVER_FILES, wrapper="Server-Files-1.1.0")  # the author has added a server pack since
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["server_pack"] is False and ctx.servers.jvm.lock_of(rec)["mode"] == "files"
    assert not any("Server-Files" in url for url in world.net.requests)


# ------------------------------------------------------------------------------ the author's server pack
def server_files(**changes):
    files = {k: v for k, v in SERVER_FILES.items() if k not in ("config/other.toml",)}
    files.update(changes)
    return {k: v for k, v in files.items() if v is not None}


async def test_a_server_made_from_a_server_pack_is_updated_from_the_next_one(cfjvm):
    world, ctx = cfjvm
    standard_pack(world, server_pack=server_files(), wrapper="Server-Files-1.0.0")
    rec, _ = await create_cf(ctx, "Archive", start=True)
    directory = ctx.servers.server_dir(rec.id)
    assert rec.spec["pack"]["server_pack"] is True
    (directory / "config" / "create.toml").write_text("speed = 9\n")  # tuned by hand
    (directory / "mods" / "extra.jar").write_bytes(b"added by hand")
    (directory / "server.properties").write_text((directory / "server.properties").read_text() + "motd=Mine\n")

    add_update(world, server_pack=server_files(**{
        "mods/lithium.jar": LITHIUM_V2.content, "mods/create.jar": None, "mods/jade.jar": JADE.content, "config/create.toml": b"speed = 6\n",
        "config/new.toml": b"fresh = true\n", "server.properties": b"allow-flight=false\n",
    }), wrapper="Server-Files-1.1.0")
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]

    assert (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM_V2.content and (directory / "mods" / "jade.jar").is_file()
    assert not (directory / "mods" / "create.jar").exists(), "a mod the new server pack dropped is removed"
    assert (directory / "mods" / "extra.jar").read_bytes() == b"added by hand"
    assert (directory / "config" / "create.toml").read_text() == "speed = 9\n" and (directory / "config" / "create.toml.pack-new").read_text() == "speed = 6\n"
    assert (directory / "config" / "new.toml").is_file()
    assert "motd=Mine" in (directory / "server.properties").read_text() and "allow-flight=true" in (directory / "server.properties").read_text(), "never replaced"
    rec = ctx.servers.get(rec.id)
    lock = ctx.servers.jvm.lock_of(rec)
    assert lock["mode"] == "archive" and set(lock["files"]) == {"mods/lithium.jar", "mods/jade.jar"} and rec.spec["pack"]["server_pack"] is True
    assert any("1 mods added" in s or "mods added" in s for s in steps_of(ctx, row))
    assert ctx.pm.runtime(rec.id).state == "running"

    row = await finish(ctx, ctx.pack_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert (directory / "mods" / "create.jar").read_bytes() == CREATE.content and (directory / "config" / "create.toml").read_text() == "speed = 9\n"
    assert not (directory / "mods" / "jade.jar").exists()


@pytest.mark.parametrize("how,message", [
    ("none", "no server files of its own"),
    ("no mods", "hold no mods"),
    ("not downloadable", "does not allow this version's server files"),
])
async def test_a_version_that_would_change_how_the_server_was_installed_is_refused(cfjvm, how, message):
    world, ctx = cfjvm
    standard_pack(world, server_pack=server_files(), wrapper="Server-Files-1.0.0")
    rec, _ = await create_cf(ctx, "Archive", start=True)
    directory = ctx.servers.server_dir(rec.id)
    options = {
        "none": {}, "no mods": {"server_pack": {"config/a.toml": b"a = 1\n"}},
        "not downloadable": {"server_pack": server_files(), "wrapper": "Server-Files-1.1.0", "server_pack_downloadable": False},
    }[how]
    add_update(world, **options)
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "failed" and message in update["message"], update["message"]
    assert (directory / "mods" / "create.jar").exists() and ctx.pm.runtime(rec.id).state == "running", "never touched"
    assert not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id))
    assert ctx.servers.get(rec.id).spec["pack"]["version_id"] == "5000"


async def test_a_server_pack_with_a_hostile_path_stops_an_update_before_anything_changes(cfjvm):
    world, ctx = cfjvm
    standard_pack(world, server_pack=server_files(), wrapper="Server-Files-1.0.0")
    rec, _ = await create_cf(ctx, "Archive", start=True)
    add_update(world, server_pack={"mods/a.jar": b"mod", "mods/../../escape.txt": b"gotcha"})
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "failed" and "outside the server folder" in update["message"]
    assert not (ctx.settings.data_dir / "escape.txt").exists() and not (ctx.settings.servers_dir / "escape.txt").exists()
    assert ctx.pm.runtime(rec.id).state == "running"
    assert files_under(ctx.settings.cache_dir / "uploads") == []


# ----------------------------------------------------------------------------------- other kinds of packs
async def test_an_uploaded_curseforge_pack_cannot_be_updated_from_here(cfjvm):
    world, ctx = cfjvm
    file = standard_pack(world)

    async def stream(data: bytes):
        yield data

    upload = await ctx.servers.jvm.accept_upload(stream(world.cf.blobs[file["id"]]))
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Uploaded", "engine": "java", "eula": True, "modpack": {"source": "upload", "upload": upload["upload"]}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    rec = next(s for s in ctx.repo.list_servers() if s.name == "Uploaded")
    with pytest.raises(ServerError, match="Only modpacks from Modrinth, CurseForge or GT New Horizons"):
        ctx.pack_updates.submit_update(rec, "5100")
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0


async def test_a_version_of_another_modpack_cannot_be_installed_as_an_update(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, "Mixed", start=False)
    world.cf.add_project(1001, "Beta Pack", class_id=4471)
    world.cf.add_pack_version(1001, 6000, "9.9.9", mods=((2001, 3001),), date=NEW)
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id), "6000"))
    assert row["status"] == "success"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "failed" and "another modpack" in update["message"] or "does not know" in update["message"]
    assert ctx.servers.get(rec.id).spec["pack"]["version_id"] == "5000"
