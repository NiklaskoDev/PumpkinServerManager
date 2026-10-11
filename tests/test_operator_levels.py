"""Operator levels: servers start their operators at level 1, and one operator's level can be set from the Players tab.

Pumpkin's /op takes no level (it uses ``op_permission_level``) and reads data/ops.json only when it starts, so a level
change on a running server is a stop, an edit of the file and a start. The fake Pumpkin behaves the same way.
"""

from __future__ import annotations

import json
import tomllib

import pytest
from conftest import create_server, finish, wait_until

from pumpkin_manager import pumpkin_config as pc
from pumpkin_manager.app import AppContext
from pumpkin_manager.servers import ServerError


def ops(ctx: AppContext, server_id: str) -> list[dict]:
    return ctx.servers.read_player_lists(ctx.repo.get_server(server_id))["ops"]


def level_of(ctx: AppContext, server_id: str, name: str) -> int | None:
    return next((e["level"] for e in ops(ctx, server_id) if e["name"] == name), None)


async def make_op(ctx: AppContext, server, name: str) -> None:
    await ctx.servers.player_action(ctx.repo.get_server(server.id), "op", name)
    await wait_until(lambda: level_of(ctx, server.id, name) is not None, 10, what=f"{name} in ops.json")


async def set_level(ctx: AppContext, server, name: str, level: int) -> dict:
    return await finish(ctx, ctx.servers.submit_operator_level(ctx.repo.get_server(server.id), name, level))


# ---------------------------------------------------------------------------------------------------- the default
async def test_new_servers_start_their_operators_at_level_1(ctx):
    server = await create_server(ctx, "Fresh")
    config = tomllib.loads((ctx.settings.servers_dir / server.id / "pumpkin.toml").read_text())
    assert config["op_permission_level"] == 1, "Pumpkin's own default would be 4"
    assert ctx.servers.read_config(server)["values"]["op_permission_level"] == 1
    assert ctx.servers.op_default_level(server) == 1
    await make_op(ctx, server, "Alex")
    assert level_of(ctx, server.id, "Alex") == 1


async def test_the_default_can_be_chosen_when_the_server_is_created(ctx):
    server = await create_server(ctx, "Strict", settings={"op_permission_level": 3})
    assert ctx.servers.op_default_level(server) == 3
    await make_op(ctx, server, "Alex")
    assert level_of(ctx, server.id, "Alex") == 3


def test_the_settings_form_offers_levels_1_to_4_with_1_as_default():
    field = pc.FIELD_BY_KEY["op_permission_level"]
    assert (field.default, field.min, field.max) == (1, 1, 4) and pc.OP_LEVELS == (1, 2, 3, 4)
    with pytest.raises(pc.ConfigError):
        pc.validate_values({"op_permission_level": 5})


# ----------------------------------------------------------------------------------------------- setting a level
async def test_a_new_operator_with_another_level_needs_a_restart_and_gets_it(ctx):
    server = await create_server(ctx, "Promote")
    pid = ctx.pm.runtime(server.id).pid

    row = await set_level(ctx, server, "Alex", 3)
    assert row["status"] == "success", row["message"]
    assert level_of(ctx, server.id, "Alex") == 3
    rt = ctx.pm.runtime(server.id)
    assert rt.state == "running" and rt.pid != pid, "the server was restarted to apply the level"
    assert any("restarts now" in step["text"] for step in row["steps"])
    assert ctx.repo.get_server(server.id).desired_running is True


async def test_an_existing_operator_can_be_changed_and_keeps_the_rest_of_the_entry(ctx):
    server = await create_server(ctx, "Reassign")
    await make_op(ctx, server, "Alex")
    path = ctx.settings.servers_dir / server.id / "data" / "ops.json"
    entries = json.loads(path.read_text())
    entries[0]["bypasses_player_limit"] = True
    path.write_text(json.dumps(entries))
    uuid_before = entries[0]["uuid"]

    row = await set_level(ctx, server, "alex", 2)  # the capitalisation need not match
    assert row["status"] == "success", row["message"]
    entry = json.loads(path.read_text())[0]
    assert (entry["name"], entry["level"], entry["uuid"], entry["bypasses_player_limit"]) == ("Alex", 2, uuid_before, True)


async def test_asking_for_the_level_an_operator_already_has_changes_nothing(ctx):
    server = await create_server(ctx, "Same")
    await make_op(ctx, server, "Alex")
    pid = ctx.pm.runtime(server.id).pid

    row = await set_level(ctx, server, "Alex", 1)
    assert row["status"] == "success" and any("already operator level 1" in step["text"] for step in row["steps"])
    assert ctx.pm.runtime(server.id).pid == pid, "no pointless restart"


async def test_a_stopped_server_gets_the_new_level_without_being_started(ctx):
    server = await create_server(ctx, "Quiet")
    await make_op(ctx, server, "Alex")
    await finish(ctx, ctx.servers.submit_stop(ctx.repo.get_server(server.id)))

    row = await set_level(ctx, server, "Alex", 4)
    assert row["status"] == "success", row["message"]
    assert level_of(ctx, server.id, "Alex") == 4
    assert ctx.pm.runtime(server.id).state == "stopped"
    assert ctx.repo.get_server(server.id).desired_running is False


async def test_a_player_who_is_not_an_operator_yet_needs_the_server_running(ctx):
    server = await create_server(ctx, "Offline")
    await finish(ctx, ctx.servers.submit_stop(ctx.repo.get_server(server.id)))

    row = await set_level(ctx, server, "Newcomer", 2)
    assert row["status"] == "failed" and "Start the server first" in row["message"]


async def test_other_operators_keep_their_levels_when_one_changes(ctx):
    server = await create_server(ctx, "Crew")
    await make_op(ctx, server, "Alex")
    await set_level(ctx, server, "Alex", 3)
    await make_op(ctx, server, "Steve")  # gets the server default
    assert (level_of(ctx, server.id, "Alex"), level_of(ctx, server.id, "Steve")) == (3, 1)
    await set_level(ctx, server, "Steve", 2)
    assert (level_of(ctx, server.id, "Alex"), level_of(ctx, server.id, "Steve")) == (3, 2)


# ------------------------------------------------------------------------------------------------------ guards
async def test_bad_requests_are_refused_before_anything_happens(ctx):
    server = await create_server(ctx, "Guarded")
    for name, level in (("Alex", 0), ("Alex", 5), ("a b", 2), ("x; stop", 2), ("", 2)):
        with pytest.raises(ServerError):
            ctx.servers.submit_operator_level(server, name, level)
    assert ctx.jobs.busy_operation(server.id) is None


async def test_an_unreadable_operator_list_is_left_alone(ctx):
    server = await create_server(ctx, "Damaged")
    await finish(ctx, ctx.servers.submit_stop(ctx.repo.get_server(server.id)))
    path = ctx.settings.servers_dir / server.id / "data" / "ops.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ServerError, match="left untouched"):
        ctx.servers._write_op_level(server, "Alex", 2)
    assert path.read_text(encoding="utf-8") == "{ this is not json"
