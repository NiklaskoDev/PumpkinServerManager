"""Spigot and BungeeCord over HTTP: what can be installed, creating them, the settings page of a proxy, and the development kits."""

from __future__ import annotations

import asyncio

import pytest
from conftest import Api, wait_until
from test_access import new_friend

from pumpkin_manager import security

yaml = pytest.importorskip("yaml")


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)


async def create(api: Api, name: str, kind: str, software: dict | None = None, **body) -> dict:
    row = await api.job(await api.call("POST", "/api/servers", json={
        "name": name, "engine": "java", "eula": True, "software": {"kind": kind, **(software or {})}, "start": False, **body,
    }))
    assert row["status"] == "success", row["message"]
    return next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == name)


# ------------------------------------------------------------------------------------------------------------------------ the catalog
async def test_the_versions_and_builds_of_spigot_can_be_listed(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    versions = (await api.call("GET", "/api/software/spigot/versions")).json()
    assert [v["id"] for v in versions["versions"]] == ["1.21.4", "1.21.1", "1.20.4", "1.16.5"] and versions["recommended"] == "1.21.4"
    builds = (await api.call("GET", "/api/software/spigot/builds", params={"minecraft": "1.21.1"})).json()
    assert [b["build"] for b in builds["builds"]] == ["4205"] and builds["default"] == "4205" and builds["java_minimum"] == 21 and builds["stable_only"] is True
    assert builds["has_experimental"] is False and builds["support"] is None and builds["recommended_flags"] == []
    missing = await api.call("GET", "/api/software/spigot/builds", params={"minecraft": "1.19"})
    assert missing.status_code == 404 and "Spigot has no build for Minecraft 1.19" in missing.json()["detail"]
    assert (await api.call("GET", "/api/software/spigot/builds")).status_code == 400, "a server needs a Minecraft version"


async def test_bungeecord_has_builds_and_no_minecraft_versions(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    versions = (await api.call("GET", "/api/software/bungeecord/versions")).json()
    assert versions == {"kind": "bungeecord", "versions": [], "recommended": ""}
    for params in ({}, {"minecraft": "1.21.4"}):
        builds = (await api.call("GET", "/api/software/bungeecord/builds", params=params)).json()
        assert [b["build"] for b in builds["builds"]] == ["2104", "2103", "2101", "2100"] and builds["default"] == "2104"
        assert builds["builds"][0]["label"] == "build 2104" and builds["builds"][0]["published"]
    world.bungee.api_status = 503
    ctx.software.invalidate()
    assert (await api.call("GET", "/api/software/bungeecord/builds")).status_code == 502


async def test_the_unknown_software_message_names_them_all(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    refused = await api.call("GET", "/api/software/forge/versions")
    assert refused.status_code == 400 and "Vanilla, Paper, Fabric, Spigot, BungeeCord" in refused.json()["detail"]


# ------------------------------------------------------------------------------------------------------------------------------ creating
async def test_a_spigot_server_is_created_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    created = await create(api, "Plugins", "spigot", {"minecraft": "1.21.1"}, memory_mb=3072, settings={"motd": "Hello"})
    assert created["software"]["label"] == "Spigot 1.21.1 (build 4205)" and created["software"]["kind"] == "spigot" and created["software"]["proxy"] is False
    assert created["proxy"] is False and created["java"]["memory_mb"] == 3072 and created["policy"]["update_mode"] == "notify"
    sid = created["id"]
    assert (await api.call("GET", f"/api/servers/{sid}/config")).json()["values"]["motd"] == "Hello"
    detail = (await api.call("GET", f"/api/servers/{sid}/software")).json()
    assert detail["software"]["name"] == "Spigot" and detail["software"]["build"] == "4205" and detail["java"]["major"] == 21


async def test_a_proxy_is_created_through_the_api_without_a_eula(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    row = await api.job(await api.call("POST", "/api/servers", json={
        "name": "Network", "engine": "java", "software": {"kind": "bungeecord"}, "settings": {"motd": "&aHello", "max_players": 20}, "start": True,
    }))
    assert row["status"] == "success", row["message"]
    created = next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == "Network")
    sid = created["id"]
    assert created["proxy"] is True and created["software"]["proxy"] is True and created["software"]["label"] == "BungeeCord (build 2104)"
    assert created["java"]["memory_mb"] == 512 and created["software"]["minecraft"] is None
    await wait_until(lambda: ctx.pm.runtime(sid).state == "running", what="the proxy to run")


async def test_a_proxy_settings_page_works_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    created = await create(api, "Network", "bungeecord", settings={"motd": "&aHello", "max_players": 20})
    sid = created["id"]
    config = (await api.call("GET", f"/api/servers/{sid}/config")).json()
    assert config["title"] == "Proxy settings" and config["file"] == "config.yml" and config["values"]["max_players"] == 20
    assert [f["key"] for f in config["schema"]] == ["motd", "max_players", "online_mode", "ip_forward"]
    saved = await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"motd": "&bNew", "online_mode": False}})
    assert saved.status_code == 200, saved.text
    assert (await api.call("GET", f"/api/servers/{sid}/config")).json()["values"]["motd"] == "&bNew"
    refused = await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"difficulty": "hard"}})
    assert refused.status_code == 422 and "unknown setting" in str(refused.json()), "a setting a proxy does not have: it is not server.properties"

    raw = (await api.call("GET", f"/api/servers/{sid}/config/raw")).json()
    assert raw["file"] == "config.yml" and "online_mode: false" in raw["content"]
    port = created["ports"]["java"]
    written = await api.call("PUT", f"/api/servers/{sid}/config/raw", json={"content": raw["content"].replace(f"0.0.0.0:{port}", "0.0.0.0:25999")})
    assert written.status_code == 200 and written.json()["reset_managed_keys"] == ["port"]
    config_yml = yaml.safe_load((ctx.servers.server_dir(sid) / "config.yml").read_text(encoding="utf-8"))
    assert config_yml["listeners"][0]["host"] == f"0.0.0.0:{port}"


async def test_the_update_routes_of_a_proxy_work_without_a_minecraft_version(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    created = await create(api, "Network", "bungeecord", {"build": "2101"})
    sid = created["id"]
    versions = (await api.call("GET", f"/api/servers/{sid}/software/versions")).json()
    assert versions["versions"] == [] and versions["kind"] == "bungeecord"
    builds = (await api.call("GET", f"/api/servers/{sid}/software/builds")).json()
    assert builds["current_build"] == "2101" and [b["build"] for b in builds["builds"]][0] == "2104"
    checked = (await api.call("POST", f"/api/servers/{sid}/software/check")).json()
    assert checked["servers_with_updates"] == 1 and checked["software"]["pending"]["label"] == "BungeeCord (build 2104)"
    row = await api.job(await api.call("POST", f"/api/servers/{sid}/software/update", json={}))
    assert row["status"] == "success", row["message"]
    after = (await api.call("GET", f"/api/servers/{sid}/software")).json()["software"]
    assert after["build"] == "2104" and after["pending"] is None and after["can_roll_back"] is True
    rolled = await api.job(await api.call("POST", f"/api/servers/{sid}/software/rollback"))
    assert rolled["status"] == "success", rolled["message"]
    assert (await api.call("GET", f"/api/servers/{sid}/software")).json()["software"]["build"] == "2101"


async def test_a_friend_sees_that_a_server_is_a_proxy_but_not_what_is_for_administrators(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Network", "bungeecord"))["id"]
    mia, _, _ = await new_friend(api, "mia", {sid: "moderator"})
    try:
        view = next(iter((await mia.call("GET", "/api/servers")).json()))
        assert view["proxy"] is True and view["software"]["proxy"] is True and view["software"]["label"] == "BungeeCord (build 2104)"
        assert "pending" not in view["software"] and "can_roll_back" not in view["software"]
    finally:
        await mia.http.aclose()


async def test_the_player_lists_of_a_proxy_are_empty_not_an_error(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Network", "bungeecord"))["id"]
    lists = await api.call("GET", f"/api/servers/{sid}/players")
    assert lists.status_code == 200 and all(entries == [] for entries in lists.json().values() if isinstance(entries, list))


# ------------------------------------------------------------------------------------------------------------- the development kits
async def test_the_development_kits_are_listed_and_can_be_removed(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    assert (await api.call("GET", "/api/java/runtimes")).json()["jdks"] == []
    await create(api, "Plugins", "spigot")
    listing = (await api.call("GET", "/api/java/runtimes")).json()
    assert [j["major"] for j in listing["jdks"]] == [21] and listing["jdks"][0]["size"] > 0 and listing["jdk_disk_bytes"] > 0
    assert [j["major"] for j in listing["installed"]] == [21], "the runtime the server runs on is a separate download from the kit that builds it"
    assert listing["disk_bytes"] > 0 and listing["installed"][0]["used_by"] == [(await api.call("GET", "/api/servers")).json()[0]["id"]]
    assert (await api.call("DELETE", "/api/java/jdks/21")).json() == {"ok": True}
    assert (await api.call("DELETE", "/api/java/jdks/21")).status_code == 404
    assert (await api.call("GET", "/api/java/runtimes")).json()["jdks"] == []


async def test_a_development_kit_is_not_removed_while_a_build_uses_it(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    await create(api, "First", "spigot")
    world.spigot.delay = 0.3
    job = await api.call("POST", "/api/servers", json={"name": "Second", "engine": "java", "eula": True, "software": {"kind": "spigot"}, "start": False})
    await wait_until(lambda: ctx.spigot_builder.busy, 20, what="the build to start")
    refused = await api.call("DELETE", "/api/java/jdks/21")
    assert refused.status_code == 409 and "build is running" in refused.json()["detail"]
    row = await api.job(job, timeout=120)
    assert row["status"] == "success", row["message"]
    assert (await api.call("DELETE", "/api/java/jdks/21")).status_code == 200


async def test_only_the_administrator_removes_a_development_kit(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Plugins", "spigot"))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    try:
        assert (await ada.call("DELETE", "/api/java/jdks/21")).status_code == 403
        assert (await ada.call("GET", "/api/java/runtimes")).status_code == 403
    finally:
        await ada.http.aclose()
    await asyncio.sleep(0)
