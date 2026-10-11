"""Whitelist/op/ban lists and the difficulty setting, against a fake Pumpkin that behaves like the real 0.2.0 build.

Two facts about the real server drove these tests (both found by running it): it keeps the lists in a ``data``
folder, and it ignores ``default_difficulty`` in pumpkin.toml (every new world starts on Normal).
"""

from __future__ import annotations

import json
import shutil

from conftest import create_server, finish, wait_until

from pumpkin_manager.app import AppContext
from pumpkin_manager.servers import PLAYER_LIST_FILES


def console(ctx: AppContext, server_id: str, since: int = 0) -> list[tuple[str, str]]:
    return [(line["s"], line["m"]) for line in ctx.pm.backlog(server_id, 3000) if line["n"] > since]


def mark(ctx: AppContext, server_id: str) -> int:
    return max((line["n"] for line in ctx.pm.backlog(server_id, 3000)), default=0)


def commands_sent(ctx: AppContext, server_id: str, since: int = 0) -> list[str]:
    return [text for stream, text in console(ctx, server_id, since) if stream == "in"]


async def current_difficulty(ctx: AppContext, server_id: str) -> str:
    """Ask the running server (the way an op would, with the bare /difficulty command) and return the answer."""
    await ctx.pm.send_command(server_id, "difficulty")
    echo = max(line["n"] for line in ctx.pm.backlog(server_id, 3000) if line["s"] == "in" and line["m"] == "difficulty")

    def answer() -> str | None:
        for line in ctx.pm.backlog(server_id, 3000):
            if line["n"] > echo and line["m"].startswith("The difficulty is "):
                return line["m"].removeprefix("The difficulty is ")
        return None

    return await wait_until(answer, 10, what="the difficulty query to be answered")


def world_difficulty(ctx: AppContext, server_id: str) -> str | None:
    level = ctx.settings.servers_dir / server_id / "world" / "level.dat"
    return json.loads(level.read_text()).get("difficulty") if level.is_file() else None


# ------------------------------------------------------------------------------------------------ player lists
async def test_player_lists_show_what_pumpkin_wrote_to_its_data_folder(ctx):
    server = await create_server(ctx, "Lists")
    data = ctx.settings.servers_dir / server.id / "data"
    # Real Pumpkin creates all four files (empty arrays) when it starts.
    assert sorted(p.name for p in data.glob("*.json")) == sorted(name for _, name in PLAYER_LIST_FILES)
    assert all(not rows for rows in ctx.servers.read_player_lists(server).values())

    for action, target, reason in (
        ("whitelist_add", "Steve_01", None), ("op", "Alex", None), ("ban", "Griefer", "broke the spawn"), ("ban_ip", "203.0.113.9", "spam"),
    ):
        await ctx.servers.player_action(server, action, target, reason)

    def lists():
        return ctx.servers.read_player_lists(ctx.repo.get_server(server.id))

    await wait_until(lambda: all(lists()[key] for key in ("whitelist", "ops", "banned_players", "banned_ips")), 10, what="all four lists")
    seen = lists()
    assert [e["name"] for e in seen["whitelist"]] == ["Steve_01"] and seen["whitelist"][0]["uuid"]
    assert seen["ops"][0]["name"] == "Alex" and seen["ops"][0]["level"] == 1, "new servers start their operators at level 1"
    assert seen["banned_players"][0]["name"] == "Griefer" and seen["banned_players"][0]["reason"] == "broke the spawn"
    assert seen["banned_players"][0]["expires"] == "forever" and seen["banned_players"][0]["source"] == "Server"
    assert seen["banned_ips"][0]["ip"] == "203.0.113.9" and seen["banned_ips"][0]["reason"] == "spam"

    for action, target in (("whitelist_remove", "Steve_01"), ("deop", "Alex"), ("pardon", "Griefer"), ("pardon_ip", "203.0.113.9")):
        await ctx.servers.player_action(server, action, target)
    await wait_until(lambda: not any(lists().values()), 10, what="all four lists to be empty again")


async def test_only_pumpkins_own_data_folder_is_read(ctx):
    """Vanilla keeps whitelist.json next to the config; Pumpkin does not read it, so it must not be shown either."""
    server = await create_server(ctx, "Strays")
    (ctx.settings.servers_dir / server.id / "whitelist.json").write_text(json.dumps([{"uuid": "x", "name": "Ghost"}]), encoding="utf-8")
    assert ctx.servers.read_player_lists(server)["whitelist"] == []


# ------------------------------------------------------------------------------------------------- difficulty
async def test_the_difficulty_chosen_at_creation_reaches_the_new_world(ctx):
    server = await create_server(ctx, "Hard mode", settings={"default_difficulty": "Hard"})
    assert "difficulty hard" in commands_sent(ctx, server.id), "the manager applies it, because Pumpkin ignores the config key"
    assert await current_difficulty(ctx, server.id) == "Hard"
    assert ctx.repo.get_kv(f"difficulty:{server.id}") == "Hard"

    await finish(ctx, ctx.servers.submit_stop(server))
    assert world_difficulty(ctx, server.id) == "hard", "saved with the world on shutdown"

    # The next start finds the world already on Hard and does not send anything.
    before = mark(ctx, server.id)
    await finish(ctx, ctx.servers.submit_start(ctx.repo.get_server(server.id)))
    assert not any(c.startswith("difficulty") for c in commands_sent(ctx, server.id, before))
    assert await current_difficulty(ctx, server.id) == "Hard"


async def test_a_normal_world_needs_no_command(ctx):
    server = await create_server(ctx, "Plain")
    assert not any(c.startswith("difficulty") for c in commands_sent(ctx, server.id))
    assert ctx.repo.get_kv(f"difficulty:{server.id}") == "Normal"
    assert await current_difficulty(ctx, server.id) == "Normal"


async def test_changing_the_difficulty_of_a_running_server_applies_at_once(ctx):
    server = await create_server(ctx, "Live")
    result = await ctx.servers.save_settings(server, {"default_difficulty": "Easy"})
    assert result["applied_live"] == ["default_difficulty"] and result["restart_required"] is False
    assert await current_difficulty(ctx, server.id) == "Easy"

    # Other settings in the same save still need a restart.
    result = await ctx.servers.save_settings(ctx.repo.get_server(server.id), {"default_difficulty": "Hard", "motd": "new text"})
    assert result["applied_live"] == ["default_difficulty"] and result["restart_required"] is True
    assert await current_difficulty(ctx, server.id) == "Hard"

    result = await ctx.servers.save_settings(ctx.repo.get_server(server.id), {"motd": "only text"})
    assert result["applied_live"] == [] and result["restart_required"] is True


async def test_a_difficulty_changed_while_the_server_is_stopped_is_applied_on_the_next_start(ctx):
    server = await create_server(ctx, "Later")
    await finish(ctx, ctx.servers.submit_stop(server))
    result = await ctx.servers.save_settings(ctx.repo.get_server(server.id), {"default_difficulty": "Peaceful"})
    assert result["applied_live"] == [] and result["restart_required"] is False

    await finish(ctx, ctx.servers.submit_start(ctx.repo.get_server(server.id)))
    assert "difficulty peaceful" in commands_sent(ctx, server.id)
    assert await current_difficulty(ctx, server.id) == "Peaceful"


async def test_a_difficulty_changed_in_game_survives_restarts(ctx):
    server = await create_server(ctx, "Operator-tweaked")  # configured Normal, and applied as such
    await ctx.pm.send_command(server.id, "difficulty hard")  # what an op typing /difficulty hard does
    await wait_until(lambda: any("Set game difficulty to Hard" in m for _, m in console(ctx, server.id)), 10, what="the change")

    before = mark(ctx, server.id)
    await finish(ctx, ctx.servers.submit_restart(ctx.repo.get_server(server.id)))
    assert not any(c.startswith("difficulty") for c in commands_sent(ctx, server.id, before)), "the manager must not undo it"
    assert await current_difficulty(ctx, server.id) == "Hard"


async def test_a_regenerated_world_gets_the_configured_difficulty_again(ctx):
    server = await create_server(ctx, "Reborn", settings={"default_difficulty": "Hard"})
    await finish(ctx, ctx.servers.submit_stop(server))
    shutil.rmtree(ctx.settings.servers_dir / server.id / "world")  # the owner wipes the world to start over

    before = mark(ctx, server.id)
    await finish(ctx, ctx.servers.submit_start(ctx.repo.get_server(server.id)))
    assert "difficulty hard" in commands_sent(ctx, server.id, before), "a new world starts on Normal again"
    assert await current_difficulty(ctx, server.id) == "Hard"


async def test_a_server_that_was_running_before_this_fix_gets_its_configured_difficulty(ctx):
    """Servers created by older versions have a world but no record of what was applied: the setting wins once."""
    server = await create_server(ctx, "Legacy", settings={"default_difficulty": "Hard"})
    ctx.repo.db.execute("DELETE FROM settings WHERE key=?", (f"difficulty:{server.id}",))
    await finish(ctx, ctx.servers.submit_stop(server))
    await ctx.servers.save_settings(ctx.repo.get_server(server.id), {"default_difficulty": "Easy"})

    await finish(ctx, ctx.servers.submit_start(ctx.repo.get_server(server.id)))
    assert await current_difficulty(ctx, server.id) == "Easy"


async def test_deleting_a_server_forgets_what_was_applied_to_it(ctx):
    server = await create_server(ctx, "Short-lived", settings={"default_difficulty": "Hard"})
    assert ctx.repo.get_kv(f"difficulty:{server.id}") == "Hard"
    ctx.repo.set_kv(f"skip:{server.id}", "some-build")
    await finish(ctx, ctx.servers.submit_delete(server, final_backup=False, delete_backups=True))
    assert ctx.repo.get_kv(f"difficulty:{server.id}") is None and ctx.repo.get_kv(f"skip:{server.id}") is None
