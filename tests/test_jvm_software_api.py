"""Vanilla, Paper and Fabric servers over HTTP: what can be installed, creating them, updating them, and who may do what."""

from __future__ import annotations

import pytest
from conftest import Api, wait_until
from test_access import new_friend

from pumpkin_manager import security


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)


async def create(api: Api, name: str = "Paper", kind: str = "paper", software: dict | None = None, **body) -> dict:
    row = await api.job(await api.call("POST", "/api/servers", json={
        "name": name, "engine": "java", "eula": True, "software": {"kind": kind, **(software or {})}, "start": False, **body,
    }))
    assert row["status"] == "success", row["message"]
    return next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == name)


# ------------------------------------------------------------------------------------------------ the catalog
async def test_the_versions_and_builds_of_each_software_can_be_listed(jvm_live):
    world, api, ctx = jvm_live
    assert (await api.call("GET", "/api/software/paper/versions")).status_code == 401, "sign in first"
    await api.login()
    paper = (await api.call("GET", "/api/software/paper/versions")).json()
    assert [v["id"] for v in paper["versions"]] == ["1.21.4", "1.21.1", "1.20.4", "1.16.5"] and paper["recommended"] == "1.21.4"
    snapshots = (await api.call("GET", "/api/software/vanilla/versions?snapshots=true")).json()
    assert snapshots["versions"][0] == {"id": "1.21.5-snapshot-1", "type": "snapshot", "published": snapshots["versions"][0]["published"]}
    assert [v["id"] for v in (await api.call("GET", "/api/software/fabric/versions")).json()["versions"]][:2] == ["1.21.4", "1.21.1"]

    builds = (await api.call("GET", "/api/software/paper/builds", params={"minecraft": "1.21.4"})).json()
    assert [b["build"] for b in builds["builds"]] == ["11", "10"] and builds["default"] == "11" and builds["java_minimum"] == 21
    assert builds["has_experimental"] is True and builds["support"] == {"status": "SUPPORTED"} and "-XX:+UseG1GC" in builds["recommended_flags"]
    everything = (await api.call("GET", "/api/software/paper/builds", params={"minecraft": "1.21.4", "experimental": "true"})).json()
    assert [b["build"] for b in everything["builds"]] == ["12", "11", "10"]

    assert (await api.call("GET", "/api/software/forge/versions")).status_code == 400
    missing = await api.call("GET", "/api/software/paper/builds", params={"minecraft": "1.7.10"})
    assert missing.status_code == 404 and "no builds for Minecraft 1.7.10" in missing.json()["detail"]
    world.net.offline = True
    ctx.software.invalidate()
    assert (await api.call("GET", "/api/software/vanilla/versions")).status_code == 502


async def test_the_catalog_is_for_those_who_manage_a_server(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Managed"))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    nina, _, _ = await new_friend(api, "nina", {})
    try:
        for friend, status in ((ada, 200), (pia, 403), (nina, 403)):
            assert (await friend.call("GET", "/api/software/paper/versions")).status_code == status
            assert (await friend.call("GET", "/api/software/paper/builds", params={"minecraft": "1.21.4"})).status_code == status
        for friend in (ada, pia):
            refused = await friend.call("POST", "/api/servers", json={"name": "Mine", "engine": "java", "eula": True, "software": {"kind": "paper"}})
            assert refused.status_code == 403, "creating servers is the administrator's"
    finally:
        for friend in (ada, pia, nina):
            await friend.http.aclose()


# ------------------------------------------------------------------------------------------------- creating
async def test_a_paper_server_is_created_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    row = await api.job(await api.call("POST", "/api/servers", json={
        "name": "Hosted", "engine": "java", "eula": True, "software": {"kind": "paper", "minecraft": "1.21.1"}, "memory_mb": 3072,
        "settings": {"motd": "Hello"}, "policy": {"backup_keep": 3, "update_mode": "notify"},
    }))
    assert row["status"] == "success", row["message"]
    created = next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == "Hosted")
    sid = created["id"]
    assert created["engine"] == "java" and created["modpack"] is None and created["build"] is None
    assert created["software"]["label"] == "Paper 1.21.1 (build 101)" and created["software"]["kind"] == "paper"
    assert created["java"]["memory_mb"] == 3072 and created["java"]["major"] == 21
    assert created["policy"]["update_mode"] == "notify" and created["policy"]["backup_keep"] == 3, "what was asked for wins over the defaults"

    await wait_until(lambda: ctx.pm.runtime(sid).state == "running", what="the server to run")
    config = (await api.call("GET", f"/api/servers/{sid}/config")).json()
    assert config["values"]["motd"] == "Hello"
    detail = (await api.call("GET", f"/api/servers/{sid}/software")).json()
    assert detail["software"]["label"] == "Paper 1.21.1 (build 101)" and detail["java"]["major"] == 21
    assert (await api.call("GET", "/api/system")).json()["summary"]["updates"] == 0


async def test_a_vanilla_and_a_fabric_server_are_created_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    vanilla = await create(api, "Pure", "vanilla", {"minecraft": "1.20.4"})
    assert vanilla["software"]["label"] == "Vanilla 1.20.4" and vanilla["policy"]["update_mode"] == "notify"
    fabric = await create(api, "Modded", "fabric", {"minecraft": "1.21.1", "fabric_api": True})
    assert fabric["software"]["label"] == "Fabric 1.21.1 (loader 0.16.10)" and fabric["software"]["fabric_api"] == {"number": "0.106.0+1.21.1"}


async def test_creating_software_that_does_not_exist_is_refused_with_a_reason(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    for software, message in (({"kind": "forge"}, "Unknown server software"), ({"kind": "paper", "minecraft": "9.9"}, "no builds for Minecraft 9.9")):
        row = await api.job(await api.call("POST", "/api/servers", json={"name": "Nope", "engine": "java", "eula": True, "software": software}))
        assert row["status"] == "failed" and message in row["message"]
    assert (await api.call("GET", "/api/servers")).json() == []


# ------------------------------------------------------------------------------------------------- updating
async def test_a_server_is_checked_updated_and_rolled_back_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Cycle", software={"minecraft": "1.21.4", "build": "10"}))["id"]

    versions = (await api.call("GET", f"/api/servers/{sid}/software/versions")).json()
    assert versions["current"] == "1.21.4" and [v["id"] for v in versions["versions"]][0] == "1.21.4"
    builds = (await api.call("GET", f"/api/servers/{sid}/software/builds", params={"minecraft": "1.21.4"})).json()
    assert builds["current_build"] == "10" and builds["current"] == "1.21.4" and builds["default"] == "11"

    nothing = await api.call("POST", f"/api/servers/{sid}/software/update", json={})
    assert nothing.status_code == 409 and "no newer version" in nothing.json()["detail"]
    checked = (await api.call("POST", f"/api/servers/{sid}/software/check")).json()
    assert checked["servers_with_updates"] == 1 and checked["update"]["available"] and checked["software"]["pending"]["label"] == "Paper 1.21.4 (build 11)"
    assert (await api.call("GET", "/api/system")).json()["summary"]["updates"] == 1, "the dashboard counts software updates too"

    for method, path in (("POST", f"/api/servers/{sid}/update"), ("POST", f"/api/servers/{sid}/rollback"), ("POST", f"/api/servers/{sid}/update/check")):
        refused = await api.call(method, path, json={})
        assert refused.status_code == 409 and "Version tab" in refused.json()["detail"], path

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/software/update", json={}))
    assert row["status"] == "success", row["message"]
    updated = (await api.call("GET", f"/api/servers/{sid}")).json()
    assert updated["software"]["build"] == "11" and updated["software"]["can_roll_back"] and not updated["update"]["available"]
    history = (await api.call("GET", f"/api/servers/{sid}/updates")).json()["updates"]
    assert (history[0]["status"], history[0]["from_tag"], history[0]["to_tag"]) == ("success", "1.21.4 (build 10)", "1.21.4 (build 11)")

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/software/rollback"))
    assert row["status"] == "success", row["message"]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["software"]["build"] == "10"
    again = await api.call("POST", f"/api/servers/{sid}/software/rollback")
    assert again.status_code == 409 and "no earlier version" in again.json()["detail"]


async def test_an_older_minecraft_version_needs_a_confirmation_over_the_api_as_well(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Older", software={"minecraft": "1.21.4"}))["id"]
    refused = await api.call("POST", f"/api/servers/{sid}/software/update", json={"minecraft": "1.20.4"})
    assert refused.status_code == 409 and refused.json()["code"] == "downgrade" and "older than the 1.21.4" in refused.json()["detail"]
    assert ctx.repo.list_updates(sid) == [], "nothing was started"
    row = await api.job(await api.call("POST", f"/api/servers/{sid}/software/update", json={"minecraft": "1.20.4", "confirm_downgrade": True}))
    assert row["status"] == "success", row["message"]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["java"]["major"] == 17


async def test_a_request_for_a_version_that_does_not_exist_or_is_installed_is_answered_at_once(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Exact", software={"minecraft": "1.21.4"}))["id"]
    same = await api.call("POST", f"/api/servers/{sid}/software/update", json={"minecraft": "1.21.4", "build": "11"})
    assert same.status_code == 409 and "already runs that version" in same.json()["detail"]
    unknown = await api.call("POST", f"/api/servers/{sid}/software/update", json={"build": "999"})
    assert unknown.status_code == 404 and "has no build 999" in unknown.json()["detail"]
    too_long = await api.call("POST", f"/api/servers/{sid}/software/update", json={"minecraft": "x" * 200})
    assert too_long.status_code == 422


async def test_other_servers_have_nothing_to_do_with_the_software_routes(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    pumpkin = await api.create_server("Plain")
    modded = (await api.call("POST", "/api/servers", json={"name": "Packed", "engine": "java", "eula": True, "start": False, "modpack": {"source": "modrinth", "project": "cool-pack"}}))
    assert (await api.job(modded))["status"] == "success"
    packed = next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == "Packed")
    for sid in (pumpkin["id"], packed["id"]):
        for method, path in (("GET", "software"), ("GET", "software/versions"), ("POST", "software/check"), ("POST", "software/update"), ("POST", "software/rollback")):
            resp = await api.call(method, f"/api/servers/{sid}/{path}", json={} if method == "POST" else None)
            assert resp.status_code == 404 and "does not run server software" in resp.json()["detail"], (sid, path)
    software = await create(api, "Software")
    resp = await api.call("GET", f"/api/servers/{software['id']}/modpack")
    assert resp.status_code == 404 and "does not run a modpack" in resp.json()["detail"]


# --------------------------------------------------------------------------------------------- who may do what
async def test_updating_the_software_is_for_those_who_manage_the_server(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Shared", software={"minecraft": "1.21.4", "build": "10"}))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    moe, _, _ = await new_friend(api, "moe", {sid: "moderator"})
    try:
        for friend, allowed in ((ada, True), (pia, False), (moe, False)):
            seen = [
                (await friend.call("GET", f"/api/servers/{sid}/software")).status_code,
                (await friend.call("GET", f"/api/servers/{sid}/software/versions")).status_code,
                (await friend.call("POST", f"/api/servers/{sid}/software/check")).status_code,
            ]
            assert seen == ([200, 200, 200] if allowed else [403, 403, 403]), (friend, seen)
        for friend in (pia, moe):
            assert (await friend.call("POST", f"/api/servers/{sid}/software/update", json={})).status_code == 403
            assert (await friend.call("POST", f"/api/servers/{sid}/software/rollback")).status_code == 403

        # every role sees which software and version it runs; what was found newer and the undo are for those who manage it
        checked = (await ada.call("POST", f"/api/servers/{sid}/software/check")).json()
        assert checked["software"]["pending"]
        for friend in (pia, moe):
            seen = (await friend.call("GET", f"/api/servers/{sid}")).json()
            assert seen["software"]["label"] == "Paper 1.21.4 (build 10)" and seen["java"] is None and seen["modpack"] is None
            assert not {"pending", "upgrade", "can_roll_back", "support", "fabric_api", "installed_at"} & set(seen["software"])
        assert (await ada.call("GET", f"/api/servers/{sid}")).json()["software"]["pending"]["build"] == "11"
        listed = [s for s in (await pia.call("GET", "/api/servers")).json()][0]
        assert listed["software"]["name"] == "Paper" and "pending" not in listed["software"]
    finally:
        for friend in (ada, pia, moe):
            await friend.http.aclose()
