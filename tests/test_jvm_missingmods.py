"""Packs with the Missing Mods Checker (Better MC): the mods its list asks for are installed, and its list stays off the server."""

from __future__ import annotations

import hashlib
import json

import pytest
import pytest_asyncio
from cftools import files_under, give_key, steps_of, zip_at
from conftest import finish
from jvmfakes import FakeMod, FakeModrinth

from pumpkin_manager.jvm import missingmods
from pumpkin_manager.jvm.mrpack import PackError

BALM = FakeMod("mods/balm-fabric-1.20.1-7.3.38.jar", b"balm" * 60)
FTB = FakeMod("mods/ftb-library-fabric-2001.2.12.jar", b"ftb library" * 40)
SECRET = FakeMod("mods/secret-sauce-1.0.jar", b"secret sauce" * 30)
LITHIUM = FakeMod("mods/lithium.jar", b"lithium" * 100)
CHECKER = b"the missing mods checker, as a jar"
PAGE = "https://www.curseforge.com/minecraft/mc-mods/{}/download/{}"


def entry(name: str, pattern: str, url: str, destination: str = "mods") -> dict:
    return {"displayName": name, "pattern": pattern, "url": url, "destination": destination}


LIST = [
    entry("Balm", BALM.filename, PAGE.format("balm-fabric", 7001)),
    entry("FTB Library", FTB.filename, PAGE.format("ftb-library-fabric", 7002)),
    entry("Stay True", "Stay_True_1.20.zip", "https://www.curseforge.com/minecraft/texture-packs/stay-true/download/7004", "resourcepacks"),
]


@pytest_asyncio.fixture
async def checked(jvm):
    """A Modrinth pack that ships the Missing Mods Checker and its list, and a CurseForge that has the listed mods."""
    world, ctx = jvm
    cf = world.cf
    cf.add_mod(5001, 7001, BALM, name="Balm")
    cf.add_mod(5002, 7002, FTB, name="FTB Library")
    pack = FakeModrinth(world.net, slug="checked-pack", title="Checked Pack", project_id="ChKd1234")
    world.checked = pack
    world.behavior["missing_mods_checker"] = True  # the fake server dies like the real one when the list is on the server
    return world, ctx


def add_version(world, number="1.0.0", entries=None, mods=(LITHIUM,), **options):
    overrides = {"mods/missingmodschecker.jar": CHECKER, "config/missing_mods_checker.json": json.dumps(LIST if entries is None else entries).encode()}
    overrides.update(options.pop("overrides", {}))
    return world.checked.add_version(number, mods=list(mods), overrides=overrides, **options)


async def create(ctx, name="Checked", **spec):
    body = {"name": name, "engine": "java", "eula": True, "modpack": {"source": "modrinth", "project": "checked-pack"}, "start": spec.pop("start", False), **spec}
    row = await finish(ctx, ctx.servers.submit_create(body))
    assert row["status"] == "success", row["message"]
    return next(s for s in ctx.repo.list_servers() if s.name == name), row


# ------------------------------------------------------------------------------------------------- with a key
async def test_the_mods_the_checker_asks_for_are_installed_from_curseforge_and_the_server_starts(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world)
    rec, row = await create(ctx, start=True)

    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / BALM.filename).read_bytes() == BALM.content and (directory / "mods" / FTB.filename).read_bytes() == FTB.content
    assert (directory / "mods" / "missingmodschecker.jar").read_bytes() == CHECKER, "the mod itself stays, as the pack has it"
    assert not (directory / "config" / "missing_mods_checker.json").exists(), "its list is not put on the server"
    assert not list(directory.rglob("Stay_True*")), "a resource pack is nothing for a server"
    assert ctx.pm.runtime(rec.id).state == "running", "the checker finds nothing to ask about, so nothing ends the server"

    lock = ctx.servers.jvm.lock_of(rec)
    assert {BALM.path, FTB.path, LITHIUM.path} == set(lock["files"]) and "config/missing_mods_checker.json" not in lock["overrides"]
    assert all(lock["files"][p].startswith("sha1:") for p in (BALM.path, FTB.path)), "found through CurseForge, checked by its checksum"
    steps = steps_of(ctx, row)
    assert any("Missing Mods Checker lists 2 mods" in s and "2 are installed from CurseForge" in s for s in steps)
    view = ctx.servers.view(rec)["modpack"]
    assert view["manual"] == [] and files_under(ctx.settings.cache_dir / "mods" / "curseforge-staging") == []


async def test_a_mod_whose_author_does_not_allow_downloads_is_listed_to_add_by_hand(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    world.cf.add_mod(5003, 7003, SECRET, name="Secret Sauce", downloadable=False)
    add_version(world, entries=[*LIST, entry("Secret Sauce", SECRET.filename, PAGE.format("secret-sauce", 7003))])
    rec, row = await create(ctx, start=True)
    assert ctx.pm.runtime(rec.id).state != "running" and any("not started yet" in s for s in steps_of(ctx, row))
    manual = ctx.servers.view(rec)["modpack"]["manual"]
    assert [(m["path"], m["name"]) for m in manual] == [(SECRET.path, "Secret Sauce")]
    assert manual[0]["page"].startswith("https://www.curseforge.com/minecraft/mc-mods/secret-sauce")
    directory = ctx.servers.server_dir(rec.id)
    (directory / "mods" / SECRET.filename).write_bytes(SECRET.content)
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["manual"] == []


# ---------------------------------------------------------------------------------------------- without a key
async def test_without_a_key_the_mods_are_listed_to_add_by_hand_and_the_server_waits(checked):
    world, ctx = checked
    add_version(world)
    rec, row = await create(ctx, start=True)
    assert ctx.pm.runtime(rec.id).state != "running", "the checker would end the server, so it is not started"
    steps = steps_of(ctx, row)
    assert any("Missing Mods Checker lists 2 mods" in s and "2 have to be added by hand" in s for s in steps)
    assert any("Add a CurseForge API key in Settings" in s for s in steps)
    manual = ctx.servers.view(rec)["modpack"]["manual"]
    assert [(m["path"], m["name"]) for m in manual] == [(BALM.path, "Balm"), (FTB.path, "FTB Library")]
    assert [m["page"] for m in manual] == [PAGE.format("balm-fabric", 7001), PAGE.format("ftb-library-fabric", 7002)]
    assert not any("api.curseforge.com" in url for url in world.net.requests), "nothing is asked of CurseForge without a key"

    directory = ctx.servers.server_dir(rec.id)
    for mod in (BALM, FTB):
        (directory / "mods" / mod.filename).write_bytes(mod.content)  # what the person downloads from the pages
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["manual"] == []
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_the_list_put_back_by_hand_ends_the_server_and_the_reason_is_explained(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world)
    rec, _ = await create(ctx)
    directory = ctx.servers.server_dir(rec.id)
    (directory / "config").mkdir(exist_ok=True)
    (directory / "config" / "missing_mods_checker.json").write_text(json.dumps(LIST))
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "failed" and "HeadlessException" in row["message"] and "a server has no screen" in row["message"]
    assert "config/missing_mods_checker.json" in row["message"]


# ---------------------------------------------------------------------------------------------- what is left alone
async def test_mods_the_pack_already_has_are_not_looked_up_again(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world, mods=(LITHIUM, BALM, FTB))  # the pack lists both files itself
    rec, row = await create(ctx)
    assert "POST /v1/mods/files" not in world.cf.api_calls, "nothing to look up: both files are there"
    assert not any("Missing Mods Checker lists" in s for s in steps_of(ctx, row))
    assert not (ctx.servers.server_dir(rec.id) / "config" / "missing_mods_checker.json").exists(), "its list is still kept off the server"


async def test_an_entry_that_names_its_mod_by_a_pattern_is_left_out_with_a_note(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world, entries=[*LIST, entry("Anything", "lithium-.*\\.jar", "https://www.curseforge.com/minecraft/mc-mods/x/download/99")])
    rec, row = await create(ctx)
    assert any("1 of the entries name their mod by a pattern" in s for s in steps_of(ctx, row))
    assert (ctx.servers.server_dir(rec.id) / "mods" / BALM.filename).is_file()


async def test_a_curseforge_outage_makes_the_mods_to_add_by_hand_and_does_not_stop_the_creation(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    world.cf.key = "another key"  # CurseForge answers every call with a refusal
    add_version(world)
    rec, row = await create(ctx)
    assert any("CurseForge could not be asked for the mods" in s for s in steps_of(ctx, row))
    assert [m["path"] for m in ctx.servers.view(rec)["modpack"]["manual"]] == [BALM.path, FTB.path]


@pytest.mark.parametrize("config,message", [
    pytest.param(b"{ this is not json", "not valid JSON", id="broken"),
    pytest.param(b'{"a": 1}', "not a list of mods", id="not a list"),
    pytest.param(json.dumps([{"displayName": "x", "pattern": "x.jar", "url": "https://www.curseforge.com/a"}] * 501).encode(), "not a list of mods", id="too long"),
])
async def test_a_list_that_cannot_be_read_is_ignored_with_a_warning(checked, config, message):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    world.checked.add_version("1.0.0", mods=[LITHIUM], overrides={"mods/missingmodschecker.jar": CHECKER, "config/missing_mods_checker.json": config})
    rec, row = await create(ctx, start=True)
    assert any(message in s and "The list is ignored" in s for s in steps_of(ctx, row))
    assert not (ctx.servers.server_dir(rec.id) / "config" / "missing_mods_checker.json").exists()
    assert ctx.pm.runtime(rec.id).state == "running"


# -------------------------------------------------------------------------------------------------------- updates
async def test_an_update_follows_the_new_list(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world)
    rec, _ = await create(ctx, start=True)
    directory = ctx.servers.server_dir(rec.id)
    balm2 = FakeMod("mods/balm-fabric-1.20.1-7.3.39.jar", b"balm, second edition" * 40)
    world.cf.add_mod(5001, 7011, balm2, name="Balm")
    add_version(world, "1.1.0", entries=[entry("Balm", balm2.filename, PAGE.format("balm-fabric", 7011)), LIST[1]])
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "success"
    assert not (directory / "mods" / BALM.filename).exists() and (directory / "mods" / balm2.filename).read_bytes() == balm2.content
    assert (directory / "mods" / FTB.filename).is_file() and not (directory / "config" / "missing_mods_checker.json").exists()
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_a_list_that_an_earlier_install_left_on_the_server_is_removed_by_the_next_update(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world)
    rec, _ = await create(ctx)
    directory = ctx.servers.server_dir(rec.id)
    old = json.dumps(LIST).encode()  # what a manager from before this knew would have unpacked
    (directory / "config").mkdir(exist_ok=True)
    (directory / "config" / "missing_mods_checker.json").write_bytes(old)
    lock = ctx.servers.jvm.lock_of(rec)
    lock["overrides"]["config/missing_mods_checker.json"] = hashlib.sha256(old).hexdigest()
    (directory / ".manager" / "pack.json").write_text(json.dumps(lock))
    add_version(world, "1.1.0")
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert not (directory / "config" / "missing_mods_checker.json").exists()

    (directory / "config" / "missing_mods_checker.json").write_text("[]")  # a person's own file is not touched
    lock = ctx.servers.jvm.lock_of(ctx.servers.get(rec.id))
    lock["overrides"]["config/missing_mods_checker.json"] = hashlib.sha256(b"something else").hexdigest()
    (directory / ".manager" / "pack.json").write_text(json.dumps(lock))
    add_version(world, "1.2.0")
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=120)
    assert row["status"] == "success", row["message"]
    assert (directory / "config" / "missing_mods_checker.json").read_text() == "[]"


# ----------------------------------------------------------------------------------------------- reading the list
def pack_with(tmp_path, entries, name="config/missing_mods_checker.json"):
    return zip_at(tmp_path / "pack.zip", {f"overrides/{name}": json.dumps(entries).encode()})


def test_the_list_is_read_and_each_entry_is_understood(tmp_path):
    odd = [
        entry("Balm", "balm.jar", PAGE.format("balm-fabric", 7420614)),
        entry("From elsewhere", "other.jar", "https://example.com/minecraft/mc-mods/x/download/5"),
        entry("Plain page", "plain.jar", "https://www.curseforge.com/minecraft/mc-mods/plain"),
        entry("A pattern", "mod-.*\\.jar", PAGE.format("pattern", 8)),
        entry("No folder named", "nofolder.jar", PAGE.format("nofolder", 9)) | {"destination": "  /Mods/ "},
        {"displayName": 3, "pattern": "bad.jar", "url": "https://www.curseforge.com/x"},
        "not an entry",
    ]
    wanted = missingmods.read_wanted(pack_with(tmp_path, odd), ("overrides/", "server-overrides/"))
    assert [(w.name, w.literal, w.file_id, w.destination) for w in wanted] == [
        ("Balm", "balm.jar", 7420614, "mods"), ("From elsewhere", "other.jar", None, "mods"), ("Plain page", "plain.jar", None, "mods"),
        ("A pattern", "", 8, "mods"), ("No folder named", "nofolder.jar", 9, "mods"),
    ]


def test_a_pack_without_a_list_has_none_and_a_later_folder_wins(tmp_path):
    assert missingmods.read_wanted(zip_at(tmp_path / "a.zip", {"overrides/config/other.json": b"[]"}), ("overrides/",)) is None
    assert missingmods.read_wanted(tmp_path / "missing.zip", ("overrides/",)) is None
    both = zip_at(tmp_path / "b.zip", {
        "overrides/config/missing_mods_checker.json": json.dumps([entry("Old", "old.jar", PAGE.format("old", 1))]).encode(),
        "server-overrides/config/missing_mods_checker.json": json.dumps([entry("New", "new.jar", PAGE.format("new", 2))]).encode(),
    })
    assert [w.name for w in missingmods.read_wanted(both, ("overrides/", "server-overrides/"))] == ["New"]


@pytest.mark.parametrize("pattern", ["../../evil.jar", "mods/sub.jar", "C:evil.jar", "a\\b.jar", "x.zip", ""])
def test_a_pattern_that_is_no_file_name_names_no_file(tmp_path, pattern):
    wanted = missingmods.read_wanted(pack_with(tmp_path, [entry("Evil", pattern, PAGE.format("evil", 5))]), ("overrides/",))
    assert wanted[0].literal == "", "nothing that could leave the mods folder is ever used as a path"


def test_an_oversized_list_is_refused(tmp_path):
    big = zip_at(tmp_path / "big.zip", {"overrides/config/missing_mods_checker.json": b"[" + b" " * (missingmods.MAX_CONFIG_BYTES + 10) + b"]"})
    with pytest.raises(PackError, match="unreasonably large"):
        missingmods.read_wanted(big, ("overrides/",))


# ------------------------------------------------------------------------------------------------ the headless flag
async def test_servers_are_started_without_a_screen_in_mind(checked):
    world, ctx = checked
    give_key(ctx, world.cf.key)
    add_version(world)
    rec, _ = await create(ctx, start=True)
    argv = json.loads((ctx.servers.server_dir(rec.id) / "fake-java.json").read_text(encoding="utf-8"))["argv"]
    assert "-Djava.awt.headless=true" in argv
