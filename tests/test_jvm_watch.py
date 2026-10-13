"""The watch that decides whether an updated Java server is up: a server that says it is still starting is not a dead one."""

from __future__ import annotations

import pytest
from conftest import create_java_server

from pumpkin_manager.jvm import packupdates
from pumpkin_manager.mcnet import PingError, StillStartingError
from pumpkin_manager.servers import ServerError

STARTING = StillStartingError("the server sent a message instead of its status: Server is still starting! Please wait before reconnecting.")
SILENT = PingError("Connection refused")


class Listener:
    """A job context that only writes down what it is told."""

    def __init__(self) -> None:
        self.steps: list[str] = []

    def step(self, text: str, level: str = "info") -> None:
        self.steps.append(text)


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(packupdates, "PING_PAUSE", 0.01)


def script(monkeypatch, answers: list[Exception | None]) -> list[int]:
    """What the server answers to the watch, one entry per ping (the last one repeats); returns a list that counts the pings."""
    pings: list[int] = []

    async def answer(host: str, port: int, timeout: float = 3.0):
        pings.append(1)
        found = answers[min(len(pings), len(answers)) - 1]
        if found is not None:
            raise found
        return {"online": 0, "max": 20, "version": "1.7.10", "protocol": 5, "motd": "", "sample": []}

    monkeypatch.setattr(packupdates, "server_list_ping", answer)
    return pings


async def test_a_server_that_says_it_is_still_starting_gets_the_time_it_needs(jvm, quick, monkeypatch):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Slow")
    pings = script(monkeypatch, [STARTING] * 12 + [None])
    listener = Listener()
    await ctx.pack_updates._watch_stability(rec, listener)
    assert len(pings) == 13, "twice as many tries as a silent server would get"
    assert any("Watching the new version" in s for s in listener.steps)


async def test_a_server_that_goes_silent_is_given_up_on_after_a_few_tries(jvm, quick, monkeypatch):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Silent")
    pings = script(monkeypatch, [SILENT])
    with pytest.raises(ServerError, match="no longer answers on its port .*Connection refused"):
        await ctx.pack_updates._watch_stability(rec, Listener())
    assert len(pings) == packupdates.PING_ATTEMPTS


async def test_a_message_in_between_starts_the_count_of_silent_tries_again(jvm, quick, monkeypatch):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Mixed")
    silent = [SILENT] * (packupdates.PING_ATTEMPTS - 1)
    script(monkeypatch, [*silent, STARTING, *silent, STARTING, *silent, None])
    await ctx.pack_updates._watch_stability(rec, Listener())


async def test_a_server_that_never_finishes_starting_is_given_up_on_in_the_end(jvm, quick, monkeypatch):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Endless")
    monkeypatch.setattr(packupdates, "STARTING_PATIENCE", 0.3)
    pings = script(monkeypatch, [STARTING])
    with pytest.raises(ServerError, match="still says that it is starting, 0 minutes after it was ready"):
        await ctx.pack_updates._watch_stability(rec, Listener())
    assert len(pings) > packupdates.PING_ATTEMPTS


async def test_a_server_that_stopped_is_reported_whatever_it_said_before(jvm, quick, monkeypatch):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Stopped", start=False)
    script(monkeypatch, [STARTING])
    with pytest.raises(ServerError, match="stopped by itself"):
        await ctx.pack_updates._watch_stability(rec, Listener())
