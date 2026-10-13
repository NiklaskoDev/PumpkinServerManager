"""Forge up to Minecraft 1.12 stops at startup when the world has blocks that the mods no longer have, and waits for "/fml confirm".
What the manager does about it: answers in the restart of an update from a pack's own server files, tells the person otherwise."""

from __future__ import annotations

import asyncio

from conftest import create_java_server, finish, wait_until
from test_jvm_gtnh_servers import create_gtnh, steps_of

from pumpkin_manager.jvm.service import FML_PROMPT_RE

PROMPT_FILE = "mods/prompt.jar"  # the fake server asks its question when this file is in the server folder (the new version brings it)


def asked(ctx, rec) -> bool:
    return any("Run the command /fml confirm" in line["m"] for line in ctx.pm.backlog(rec.id, 600))


PATIENCE = {"start_timeout": 15}  # the least a server may be given to come up


def warnings(ctx, rec) -> list[str]:
    return [a["message"] for a in ctx.repo.list_activity(50, rec.id) if a["level"] == "warning"]


async def test_the_blocks_an_update_removes_are_confirmed_for_a_pack_from_its_own_server_files(jvm):
    world, ctx = jvm
    world.gtnh.add_release("2.8.5", first=True, changes={PROMPT_FILE: b"the new version"})
    rec, _ = await create_gtnh(ctx, "Removes", modpack={"version": "2.8.4"}, start=True)
    world.behavior.update({"fml_prompt": 2, "fml_prompt_if_present": PROMPT_FILE})
    await ctx.pack_updates.check()

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "success", ctx.repo.list_updates(rec.id)[0]["message"]
    assert ctx.servers.get(rec.id).spec["pack"]["version_id"] == "2.8.5" and ctx.pm.runtime(rec.id).state == "running"
    assert asked(ctx, rec) and any("answered it with '/fml confirm'" in line["m"] for line in ctx.pm.backlog(rec.id, 600))
    told = [s for s in steps_of(ctx, row) if "removed 2 block(s) or item(s)" in s]
    assert told and "witchery:block0, witchery:block1" in told[0] and "the backup made before the update is the way back" in told[0]
    assert not warnings(ctx, rec), "nobody had to be asked"
    assert ctx.servers.jvm.confirm_missing == set(), "the permission was for that one restart"


async def test_so_many_missing_blocks_that_something_must_be_wrong_are_not_confirmed(jvm):
    world, ctx = jvm
    world.gtnh.add_release("2.8.5", first=True, changes={PROMPT_FILE: b"the new version"})
    rec, _ = await create_gtnh(ctx, "Too many", modpack={"version": "2.8.4"}, start=True, policy=PATIENCE)
    world.behavior.update({"fml_prompt": 5000, "fml_prompt_if_present": PROMPT_FILE})
    await ctx.pack_updates.check()

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", "the job itself ends normally: the problem was handled"
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "rolled_back" and "did not become ready within 15 seconds" in update["message"]
    assert "stopped to ask a question on its console that nobody answered" in update["message"], "the history says why, not only that it took long"
    assert ctx.servers.get(rec.id).spec["pack"]["version_id"] == "2.8.4" and ctx.pm.runtime(rec.id).state == "running", "the old version runs again"
    said = warnings(ctx, rec)
    assert any("5000 blocks or items" in m and "more than an update removes" in m and "type '/fml confirm'" in m for m in said), said


async def test_a_pack_installed_from_a_list_of_mods_is_not_answered_for(jvm):
    """The mods of such a pack were picked by the manager (and what it left out may be what the world needs): a person decides."""
    from jvmfakes import FakeMod

    world, ctx = jvm
    rec = await create_java_server(ctx, "Listed", policy=PATIENCE)
    world.pack.add_version("1.1.0", mods=[FakeMod(PROMPT_FILE, b"the new version")])
    world.behavior.update({"fml_prompt": 3, "fml_prompt_if_present": PROMPT_FILE})
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success"
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "rolled_back"
    said = warnings(ctx, rec)
    assert any("3 blocks or items" in m and "does not answer such questions by itself" in m for m in said), said


async def test_a_server_that_waits_for_an_answer_at_a_normal_start_says_so_and_goes_on_when_it_gets_one(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Waits", modpack={"version": "2.8.4"})
    (ctx.servers.server_dir(rec.id) / PROMPT_FILE).parent.mkdir(exist_ok=True)
    (ctx.servers.server_dir(rec.id) / PROMPT_FILE).write_bytes(b"added by hand")
    world.behavior.update({"fml_prompt": 4, "fml_prompt_if_present": PROMPT_FILE})

    job = ctx.servers.submit_start(ctx.servers.get(rec.id))
    await wait_until(lambda: asked(ctx, rec) and warnings(ctx, rec), 30, what="the question and the warning")
    said = " ".join(warnings(ctx, rec))
    assert "4 blocks or items" in said and "(witchery:block0, witchery:block1, witchery:block2)" in said and "type '/fml confirm'" in said
    assert ctx.pm.runtime(rec.id).state == "starting" and not any("the manager answered" in line["m"] for line in ctx.pm.backlog(rec.id, 600))
    await ctx.pm.send_command(rec.id, "fml confirm")  # Forge does not take that for an answer: the slash is part of the command
    await asyncio.sleep(1.5)
    assert ctx.pm.runtime(rec.id).state == "starting"
    await ctx.pm.send_command(rec.id, "/fml confirm")
    row = await finish(ctx, job, timeout=60)
    assert row["status"] == "success", row["message"]
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_the_count_and_names_are_read_from_what_forge_printed(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Reads", modpack={"version": "2.8.4"})
    lines = [
        "[19:22:53] [Server thread/WARN] [FML]: Forge Mod Loader detected missing blocks/items.", "", "There are 7 missing blocks and items in this save.", "",
        "Missing Blocks/Items:", "witchery:silvervat", "witchery:silvervat", "gregtech:gt.oldthing ", "", "", "Run the command /fml confirm or or /fml cancel to proceed.",
    ]
    jvm_service = ctx.servers.jvm
    assert await jvm_service._answer_prompt(ctx.servers.get(rec.id), lines) is None
    assert jvm_service.prompts[rec.id] == {"count": 7, "names": ["witchery:silvervat", "gregtech:gt.oldthing"], "answered": False}
    jvm_service.confirm_missing.add(rec.id)
    assert await jvm_service._answer_prompt(ctx.servers.get(rec.id), lines) == "/fml confirm"
    assert jvm_service.prompts[rec.id]["answered"] is True
    assert await jvm_service._answer_prompt(ctx.servers.get(rec.id), ["Run the command /fml confirm or or /fml cancel to proceed."]) is None, \
        "a question that does not say how much it would remove is not answered"
    assert FML_PROMPT_RE.search(lines[-1]) and not FML_PROMPT_RE.search("Forge Mod Loader detected missing blocks/items.")
