"""'Clean up unused' on the Versions page: it removes every downloaded build that no server needs, and nothing else."""

from __future__ import annotations

import asyncio

from conftest import wait_until


async def download(api, channel: str) -> None:
    row = await api.job(await api.call("POST", "/api/versions/download", json={"channel": channel}))
    assert row["status"] == "success", row["message"]


def entry(overview: dict, build_id: str) -> dict:
    return next(b for b in overview["builds"] if b["id"] == build_id)


async def test_clean_up_removes_every_build_no_server_needs(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Alpha")
    in_use = ctx.repo.get_server(server["id"]).build_id
    ctx.release_source.publish("nightly", "n1")
    await api.call("POST", "/api/versions/check")
    await download(api, "nightly")

    before = (await api.call("GET", "/api/versions")).json()
    nightly = next(b for b in before["builds"] if b["channel"] == "nightly")
    assert nightly["available"] and nightly["used_by"] == []
    assert before["channels"]["nightly"]["downloaded"], "the channel card must notice the download"

    # The newest build of a channel is not special: when nobody uses it, the button removes it.
    result = (await api.call("POST", "/api/versions/prune")).json()
    assert result["removed"] == [nightly["id"]] and result["freed_bytes"] == nightly["size"]

    after = (await api.call("GET", "/api/versions")).json()
    assert not entry(after, nightly["id"])["available"] and entry(after, in_use)["available"]
    assert not after["channels"]["nightly"]["downloaded"], "the channel card must notice that it is gone again"
    assert after["disk_bytes"] < before["disk_bytes"]
    assert (await api.call("POST", "/api/versions/prune")).json() == {"removed": [], "freed_bytes": 0}


async def test_clean_up_keeps_what_servers_run_can_roll_back_to_or_are_about_to_update_to(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Alpha")
    sid = server["id"]
    first = ctx.repo.get_server(sid).build_id
    ctx.release_source.publish("stable", "0.2.0")
    await api.call("POST", "/api/versions/check")
    waiting = ctx.repo.get_server(sid).pending_build_id
    assert waiting
    await ctx.builds.ensure(ctx.repo.get_build(waiting))  # fetched ahead of the update

    overview = (await api.call("GET", "/api/versions")).json()
    assert entry(overview, waiting)["used_by"] == [] and entry(overview, waiting)["wanted_by"] == [sid]
    assert (await api.call("POST", "/api/versions/prune")).json()["removed"] == []
    assert (await api.call("DELETE", f"/api/versions/{waiting}")).status_code == 409

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/update", json={}))
    assert row["status"] == "success", row["message"]
    overview = (await api.call("GET", "/api/versions")).json()
    assert entry(overview, first)["used_by"] == [sid], "the old version is what the server can roll back to"
    assert (await api.call("POST", "/api/versions/prune")).json()["removed"] == []
    assert all(entry(overview, b)["available"] for b in (first, waiting))


async def test_the_clean_up_after_an_update_keeps_spares_but_the_button_does_not(ctx, source):
    for version in ("n1", "n2"):
        await ctx.builds.ensure(ctx.builds.register(source.publish("nightly", version)))
    newer, older = (b for b in ctx.repo.list_builds() if b.channel == "nightly")

    assert ctx.updater.prune_builds() == [older.id], "automatic clean-up keeps the newest build per channel as a spare"
    assert ctx.builds.is_available(newer) and not ctx.builds.is_available(older)
    assert ctx.updater.prune_builds(keep_spares=False) == [newer.id]
    assert not ctx.builds.binary_path(newer.id).exists()


async def test_clean_up_also_removes_a_build_whose_record_forgot_that_it_is_on_disk(ctx, source):
    build = await ctx.builds.ensure(ctx.builds.register(source.publish("nightly", "n1")))
    ctx.repo.mark_not_downloaded(build.id)  # the file is still there, and the Versions page lists it
    assert ctx.builds.is_available(ctx.repo.get_build(build.id))

    assert ctx.updater.prune_builds(keep_spares=False) == [build.id]
    assert not ctx.builds.binary_path(build.id).exists()


async def test_clean_up_and_delete_wait_while_a_server_is_being_created_or_updated(live):
    api, ctx = live
    await api.login()
    ctx.release_source.publish("nightly", "n1")
    await download(api, "nightly")
    nightly = next(b for b in (await api.call("GET", "/api/versions")).json()["builds"] if b["channel"] == "nightly")

    # A build that was fetched a moment ago for a server that is not recorded yet must not disappear underneath it.
    release = asyncio.Event()

    async def hold(job) -> None:
        await release.wait()

    ctx.jobs.submit("update", "Pretend update", None, hold)
    assert (await api.call("POST", "/api/versions/prune")).status_code == 409
    assert (await api.call("DELETE", f"/api/versions/{nightly['id']}")).status_code == 409
    assert ctx.builds.is_available(ctx.repo.get_build(nightly["id"]))

    release.set()
    await wait_until(lambda: not ctx.jobs.is_running("update"), what="the pretend update to finish")
    assert (await api.call("POST", "/api/versions/prune")).json()["removed"] == [nightly["id"]]
