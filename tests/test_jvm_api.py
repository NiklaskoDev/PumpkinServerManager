"""Modpack servers over HTTP: finding and uploading modpacks, creating servers, who may change what, Java runtimes."""

from __future__ import annotations

import pytest
from conftest import Api, wait_until
from jvmfakes import PNG_HEADER, FakeMod
from test_access import new_friend

from pumpkin_manager import security

MODPACK = {"source": "modrinth", "project": "cool-pack"}


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)


async def create(api: Api, name: str = "Modded", **body) -> dict:
    row = await api.job(await api.call("POST", "/api/servers", json={"name": name, "engine": "java", "eula": True, "modpack": MODPACK, **body}))
    assert row["status"] == "success", row["message"]
    return next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == name)


async def test_modpacks_can_be_found_and_inspected(jvm_live):
    world, api, ctx = jvm_live
    assert (await api.call("GET", "/api/modpacks/search?q=cool")).status_code == 401, "sign in first"
    await api.login()
    found = (await api.call("GET", "/api/modpacks/search?q=cool")).json()
    assert found["total"] == 1 and found["hits"][0]["slug"] == "cool-pack" and found["hits"][0]["id"] == "AbCdEf12"
    assert (await api.call("GET", "/api/modpacks/search")).json()["total"] == 1, "no search text lists the popular ones"

    project = (await api.call("GET", "/api/modpacks/project", params={"ref": "https://modrinth.com/modpack/cool-pack"})).json()
    assert project["title"] == "Cool Pack" and project["versions"][0]["number"] == "1.0.0"
    assert project["versions"][0]["minecraft"] == ["1.21.1"] and project["versions"][0]["loaders"] == ["neoforge"]
    unknown = await api.call("GET", "/api/modpacks/project", params={"ref": "no-such-pack"})
    assert unknown.status_code == 400 and "does not know" in unknown.json()["detail"]
    assert (await api.call("GET", "/api/modpacks/project", params={"ref": "https://example.com/x"})).status_code == 400


async def test_a_modpack_server_is_created_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    created = await create(api, "Modded", memory_mb=3072, settings={"motd": "Hello"}, policy={"backup_keep": 3})
    sid = created["id"]
    assert created["engine"] == "java" and created["build"] is None and created["policy"]["backup_keep"] == 3
    assert created["modpack"]["title"] == "Cool Pack" and created["java"]["memory_mb"] == 3072 and created["java"]["major"] == 21

    detail = (await api.call("GET", f"/api/servers/{sid}")).json()
    await wait_until(lambda: ctx.pm.runtime(sid).state == "running", what="the server to run")
    config = (await api.call("GET", f"/api/servers/{sid}/config")).json()
    assert config["values"]["motd"] == "Hello" and any(f["key"] == "max-players" for f in config["schema"]) and detail["ports"]["bedrock"] is None
    saved = await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"max-players": 12}})
    assert saved.status_code == 200 and saved.json()["restart_required"] is True, "a running server has to be restarted"
    bad = await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"max-players": 0}})
    assert bad.status_code == 422 and "max-players" in bad.json()["errors"]
    raw = (await api.call("GET", f"/api/servers/{sid}/config/raw")).json()
    assert raw["file"] == "server.properties" and "max-players=12" in raw["content"]

    players = (await api.call("GET", f"/api/servers/{sid}/players")).json()
    assert players["running"] is True and players["op_level"] == 1
    console = (await api.call("GET", f"/api/servers/{sid}/console")).json()["lines"]
    assert any("Done (" in line["m"] for line in console)
    assert (await api.call("POST", f"/api/servers/{sid}/command", json={"command": "list"})).status_code == 200


async def test_the_minecraft_eula_is_required_through_the_api_too(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    row = await api.job(await api.call("POST", "/api/servers", json={"name": "NoEula", "engine": "java", "modpack": MODPACK}))
    assert row["status"] == "failed" and "EULA" in row["message"]
    assert (await api.call("GET", "/api/servers")).json() == []


async def test_an_uploaded_modpack_file_can_create_a_server(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    pack = world.pack.packs[world.pack.versions[0]["id"]]
    uploaded = await api.call("PUT", "/api/modpacks/upload", content=pack)
    assert uploaded.status_code == 200, uploaded.text
    info = uploaded.json()
    assert info["pack"]["name"] == "Cool Pack" and info["pack"]["mods"] == 2 and info["pack"]["loader"]["label"] == "NeoForge 21.1.77"
    server = await create(api, "FromFile", modpack={"source": "upload", "upload": info["upload"]}, start=False)
    assert server["modpack"]["source"] == "upload"
    refused = await api.call("PUT", "/api/modpacks/upload", content=b"definitely not a modpack")
    assert refused.status_code == 400 and "not a Modrinth modpack" in refused.json()["detail"]


async def test_how_a_java_server_is_launched_is_for_the_administrator(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Shared", start=False))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        for friend in (ada, pia):
            assert (await friend.call("GET", "/api/modpacks/search")).status_code == 403, "creating servers is the administrator's"
            assert (await friend.call("GET", "/api/java/runtimes")).status_code == 403
        # an admin on the server may change its settings, but not how much memory it takes or how Java is started
        assert (await ada.call("PATCH", f"/api/servers/{sid}", json={"name": "Renamed"})).status_code == 200
        assert (await ada.call("PATCH", f"/api/servers/{sid}", json={"java": {"memory_mb": 2048}})).status_code == 403
        assert (await ada.call("PATCH", f"/api/servers/{sid}", json={"java": {"jvm_args": "-Xss4M"}})).status_code == 403
        assert ctx.repo.get_server(sid).spec["memory_mb"] == 4096
        changed = (await api.call("PATCH", f"/api/servers/{sid}", json={"java": {"memory_mb": 2048, "jvm_args": "-Xss4M"}})).json()
        assert changed["java"]["memory_mb"] == 2048 and changed["java"]["jvm_args"] == "-Xss4M"
        bad = await api.call("PATCH", f"/api/servers/{sid}", json={"java": {"jvm_args": "-jar evil.jar"}})
        assert bad.status_code == 422

        # every role sees which modpack it is; the launch settings stay with those who manage the server
        seen = (await pia.call("GET", f"/api/servers/{sid}")).json()
        assert seen["modpack"]["title"] == "Cool Pack" and seen["java"] is None
        assert "pending" not in seen["modpack"] and "can_roll_back" not in seen["modpack"], "updates are for those who manage the server"
        assert (await ada.call("GET", f"/api/servers/{sid}")).json()["java"]["memory_mb"] == 2048
    finally:
        await ada.http.aclose()
        await pia.http.aclose()


async def test_java_runtimes_can_be_listed_downloaded_and_removed(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    assert (await api.call("GET", "/api/java/runtimes")).json()["installed"] == []
    server = await create(api, "UsesJava", start=False)
    listing = (await api.call("GET", "/api/java/runtimes")).json()
    assert [j["major"] for j in listing["installed"]] == [21] and listing["installed"][0]["used_by"] == [server["id"]]
    assert listing["available"] == [8, 11, 17, 21, 25] and listing["disk_bytes"] > 0 and listing["mods_cache_bytes"] > 0
    in_use = await api.call("DELETE", "/api/java/runtimes/21")
    assert in_use.status_code == 409 and "UsesJava" in in_use.json()["detail"]

    row = await api.job(await api.call("POST", "/api/java/runtimes/17"))
    assert row["status"] == "success", row["message"]
    assert [j["major"] for j in (await api.call("GET", "/api/java/runtimes")).json()["installed"]] == [17, 21]
    assert (await api.call("DELETE", "/api/java/runtimes/17")).json() == {"ok": True}
    assert (await api.call("DELETE", "/api/java/runtimes/17")).status_code == 404
    assert (await api.call("POST", "/api/java/runtimes/12")).status_code == 400


async def test_the_system_info_tells_the_dashboard_about_memory_and_java(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    await create(api, "Info", start=False)
    system = (await api.call("GET", "/api/system")).json()
    assert system["memory"]["total"] > 0 and system["memory"]["available"] > 0
    assert system["memory"]["limit"] is None or system["memory"]["limit"] > 0
    assert system["java_installed"] == [21]


async def test_modpack_icons_come_through_the_manager_from_modrinths_image_host_only(jvm_live):
    world, api, ctx = jvm_live
    icon = "https://cdn.modrinth.com/data/AbCdEf12/icon.png"
    assert (await api.call("GET", "/api/modpacks/icon", params={"url": icon})).status_code == 401
    await api.login()
    shown = await api.call("GET", "/api/modpacks/icon", params={"url": icon})
    assert shown.status_code == 200 and shown.headers["content-type"] == "image/png" and shown.content.startswith(PNG_HEADER)
    world.net.blob("https://cdn.modrinth.com/data/AbCdEf12/icon.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")
    world.net.blob("https://cdn.modrinth.com/data/AbCdEf12/page.png", b"<html>not a picture</html>", "text/html")
    for bad, message in (
        ("https://evil.example.com/icon.png", "allowed hosts"), ("http://cdn.modrinth.com/data/AbCdEf12/icon.png", "https"),
        ("https://cdn.modrinth.com/data/AbCdEf12/icon.svg", "not a picture"), ("https://cdn.modrinth.com/data/AbCdEf12/page.png", "not a picture"),
        ("https://cdn.modrinth.com/data/missing/icon.png", "not available"),
    ):
        refused = await api.call("GET", "/api/modpacks/icon", params={"url": bad})
        assert refused.status_code == 400 and message in refused.json()["detail"], (bad, refused.text)
    # everybody who sees a modpack server also sees its icon
    sid = (await create(api, "Icons", start=False))["id"]
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        assert (await pia.call("GET", "/api/modpacks/icon", params={"url": icon})).status_code == 200
    finally:
        await pia.http.aclose()


async def test_a_modpack_server_lists_what_the_pack_installed(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Packed", start=False))["id"]
    info = (await api.call("GET", f"/api/servers/{sid}/modpack")).json()
    assert info["mods"] == ["mods/create.jar", "mods/lithium.jar"] and info["mod_count"] == 2 and info["config_files"] == 2
    assert info["modpack"]["title"] == "Cool Pack" and info["java"]["major"] == 21
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        assert (await ada.call("GET", f"/api/servers/{sid}/modpack")).status_code == 200
        assert (await pia.call("GET", f"/api/servers/{sid}/modpack")).status_code == 403, "what is installed is for those who manage the server"
    finally:
        await ada.http.aclose()
        await pia.http.aclose()
    pumpkin = await api.create_server("Plain")
    assert (await api.call("GET", f"/api/servers/{pumpkin['id']}/modpack")).status_code == 404


async def test_a_modpack_is_updated_and_rolled_back_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Api update", start=False))["id"]
    world.pack.add_version("1.1.0", mods=[FakeMod("mods/lithium.jar", b"lithium-second-edition" * 100)])

    versions = (await api.call("GET", f"/api/servers/{sid}/modpack/versions")).json()
    assert [v["number"] for v in versions["versions"]] == ["1.1.0", "1.0.0"] and versions["pending"] is None
    checked = (await api.call("POST", f"/api/servers/{sid}/modpack/check")).json()
    assert checked["servers_with_updates"] == 1 and checked["update"]["available"] and checked["update"]["pending_pack"]["number"] == "1.1.0"
    assert (await api.call("GET", "/api/system")).json()["summary"]["updates"] == 1, "the dashboard counts modpack updates too"

    for method, path in (("POST", f"/api/servers/{sid}/update"), ("POST", f"/api/servers/{sid}/rollback"), ("POST", f"/api/servers/{sid}/update/check")):
        refused = await api.call(method, path, json={})
        assert refused.status_code == 409 and "Modpack tab" in refused.json()["detail"], path

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/modpack/update", json={}))
    assert row["status"] == "success", row["message"]
    updated = (await api.call("GET", f"/api/servers/{sid}")).json()
    assert updated["modpack"]["version_number"] == "1.1.0" and updated["modpack"]["can_roll_back"] and not updated["update"]["available"]
    history = (await api.call("GET", f"/api/servers/{sid}/updates")).json()["updates"]
    assert (history[0]["status"], history[0]["from_tag"], history[0]["to_tag"]) == ("success", "1.0.0", "1.1.0")

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/modpack/rollback"))
    assert row["status"] == "success", row["message"]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["modpack"]["version_number"] == "1.0.0"
    again = await api.call("POST", f"/api/servers/{sid}/modpack/rollback")
    assert again.status_code == 409 and "no earlier modpack version" in again.json()["detail"]
    pumpkin = await api.create_server("Plain")
    for path in ("versions", "check", "update", "rollback"):
        wrong = await api.call("GET" if path == "versions" else "POST", f"/api/servers/{pumpkin['id']}/modpack/{path}", json={})
        assert wrong.status_code == 404, path


async def test_updating_a_modpack_is_for_those_who_manage_the_server(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Managed", start=False))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        for friend, allowed in ((ada, True), (pia, False)):
            seen = [
                (await friend.call("GET", f"/api/servers/{sid}/modpack/versions")).status_code,
                (await friend.call("POST", f"/api/servers/{sid}/modpack/check")).status_code,
            ]
            assert seen == ([200, 200] if allowed else [403, 403]), (friend, seen)
        assert (await pia.call("POST", f"/api/servers/{sid}/modpack/update", json={})).status_code == 403
        assert (await pia.call("POST", f"/api/servers/{sid}/modpack/rollback")).status_code == 403
    finally:
        await ada.http.aclose()
        await pia.http.aclose()


async def test_a_manual_check_says_when_modrinth_cannot_be_reached(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    sid = (await create(api, "Offline", start=False))["id"]
    world.net.offline = True
    result = await api.call("POST", f"/api/servers/{sid}/modpack/check")
    assert result.status_code == 502 and "Could not reach Modrinth" in result.json()["detail"]
