"""GT New Horizons over HTTP: offering the pack, creating, updating and rolling back its servers, and who may do what."""

from __future__ import annotations

import pytest
from conftest import Api, wait_until
from test_access import new_friend

from pumpkin_manager import security


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)


async def create(api: Api, name: str = "GTNH", modpack: dict | None = None, **body) -> dict:
    row = await api.job(await api.call("POST", "/api/servers", json={
        "name": name, "engine": "java", "eula": True, "modpack": {"source": "gtnh", **(modpack or {})}, "start": False, **body,
    }))
    assert row["status"] == "success", row["message"]
    return next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == name)


async def test_the_pack_and_its_versions_are_offered_to_the_administrator_only(jvm_live):
    world, api, ctx = jvm_live
    query = {"ref": "gtnh", "source": "gtnh"}
    assert (await api.call("GET", "/api/modpacks/project", params=query)).status_code == 401, "sign in first"
    await api.login()
    project = (await api.call("GET", "/api/modpacks/project", params=query)).json()
    assert (project["id"], project["title"], project["source"]) == ("gtnh", "GT New Horizons", "gtnh") and "Minecraft 1.7.10" in project["description"]
    assert [(v["id"], v["type"]) for v in project["versions"]][:2] == [("2.9.0-RC-2", "beta"), ("2.8.4", "release")]
    assert project["versions"][1] == {
        "id": "2.8.4", "number": "2.8.4", "type": "release", "published": "2025-12-23", "minecraft": ["1.7.10"], "loaders": ["forge"],
        "server_pack": True, "size": 0, "builds": [{"variant": "modern", "name": "Java 17 and newer", "runs_on": 25}, {"variant": "legacy", "name": "Java 8", "runs_on": 8}],
    }
    assert (await api.call("GET", "/api/modpacks/project", params={"ref": "gtnh", "source": "nowhere"})).status_code == 422

    sid = (await create(api, "Managed"))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        for friend in (ada, pia):
            assert (await friend.call("GET", "/api/modpacks/project", params=query)).status_code == 403, "creating servers is the administrator's"
            refused = await friend.call("POST", "/api/servers", json={"name": "Mine", "engine": "java", "eula": True, "modpack": {"source": "gtnh"}})
            assert refused.status_code == 403
    finally:
        await ada.http.aclose()
        await pia.http.aclose()

    world.net.offline = True
    ctx.servers.jvm.gtnh.invalidate()
    down = await api.call("GET", "/api/modpacks/project", params=query)
    assert down.status_code == 400 and "GT New Horizons website" in down.json()["detail"]


async def test_a_server_is_created_updated_and_rolled_back_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    created = await create(api, "Hosted", {"version": "2.8.3"}, settings={"motd": "Hello"}, start=True)
    sid = created["id"]
    assert created["engine"] == "java" and created["software"] is None and created["build"] is None
    assert (created["modpack"]["title"], created["modpack"]["source"], created["modpack"]["variant"], created["modpack"]["version_number"]) == (
        "GT New Horizons", "gtnh", "modern", "2.8.3")
    assert created["java"]["memory_mb"] == 6144 and created["java"]["major"] == 25 and created["policy"]["start_timeout"] == 1800
    await wait_until(lambda: ctx.pm.runtime(sid).state == "running", what="the server to run")
    assert (await api.call("GET", f"/api/servers/{sid}/config")).json()["values"]["motd"] == "Hello"

    info = (await api.call("GET", f"/api/servers/{sid}/modpack")).json()
    assert info["modpack"]["loader"]["label"] == "Forge 10.13.4.1614" and info["mod_count"] == 4 and info["config_files"] > 10
    assert "mods/gregtech-5.09.jar" in info["mods"] and info["modpack"]["notice"] is None

    versions = (await api.call("GET", f"/api/servers/{sid}/modpack/versions")).json()
    assert versions["current"] == "2.8.3" and [v["id"] for v in versions["versions"]] == ["2.9.0-RC-2", "2.8.4", "2.8.3", "2.7.4"]
    nothing = await api.call("POST", f"/api/servers/{sid}/modpack/update", json={"version_id": "2.8.3"})
    assert nothing.status_code == 202 and (await api.job(nothing))["status"] == "failed"

    checked = (await api.call("POST", f"/api/servers/{sid}/modpack/check")).json()
    assert checked["servers_with_updates"] == 1 and checked["update"]["pending_pack"]["number"] == "2.8.4"
    assert (await api.call("GET", "/api/system")).json()["summary"]["updates"] == 1

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/modpack/update", json={}))
    assert row["status"] == "success", row["message"]
    updated = (await api.call("GET", f"/api/servers/{sid}")).json()
    assert updated["modpack"]["version_number"] == "2.8.4" and updated["modpack"]["can_roll_back"] and not updated["update"]["available"]
    history = (await api.call("GET", f"/api/servers/{sid}/updates")).json()["updates"]
    assert (history[0]["status"], history[0]["from_tag"], history[0]["to_tag"]) == ("success", "2.8.3", "2.8.4")

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/modpack/rollback"))
    assert row["status"] == "success", row["message"]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["modpack"]["version_number"] == "2.8.3"


async def test_creating_from_the_curseforge_listing_is_refused_with_the_way_forward(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    await api.call("PUT", "/api/settings", json={"curseforge_api_key": world.cf.key})
    world.cf.add_project(252507, "GT New Horizons", class_id=4471)
    world.cf.add_pack_version(252507, 7446117, "2.8.4", minecraft="1.7.10", loader="forge-10.13.4.1614")
    refused = await api.call("GET", "/api/modpacks/project", params={"ref": "252507", "source": "curseforge"})
    assert refused.status_code == 400 and "only a list for reference" in refused.json()["detail"] and "New server → GT New Horizons" in refused.json()["detail"]
    row = await api.job(await api.call("POST", "/api/servers", json={
        "name": "Stub", "engine": "java", "eula": True, "modpack": {"source": "curseforge", "project": "252507"}, "start": False,
    }))
    assert row["status"] == "failed" and "only a list for reference" in row["message"]
    assert (await api.call("GET", "/api/servers")).json() == []


async def test_players_see_which_pack_it_is_but_not_how_it_is_managed(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Shared", {"version": "2.8.3"}))["id"]
    await api.call("POST", f"/api/servers/{sid}/modpack/check")
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        seen = (await pia.call("GET", f"/api/servers/{sid}")).json()["modpack"]
        assert (seen["title"], seen["version_number"], seen["variant"], seen["loader"]["label"]) == ("GT New Horizons", "2.8.3", "modern", "Forge 10.13.4.1614")
        assert not {"pending", "can_roll_back", "notice", "manual", "client_only"} & set(seen), "updates and instructions are for those who manage the server"
        managed = (await ada.call("GET", f"/api/servers/{sid}")).json()["modpack"]
        assert managed["pending"]["number"] == "2.8.4" and managed["can_roll_back"] is False
        assert (await ada.call("GET", f"/api/servers/{sid}/modpack/versions")).status_code == 200
        assert (await pia.call("GET", f"/api/servers/{sid}/modpack/versions")).status_code == 403
        assert (await pia.call("POST", f"/api/servers/{sid}/modpack/update", json={})).status_code == 403
    finally:
        await ada.http.aclose()
        await pia.http.aclose()
