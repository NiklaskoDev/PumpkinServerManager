"""CurseForge over HTTP: the API key in the settings, finding packs, creating and updating servers, and who sees what."""

from __future__ import annotations

import pytest
from cftools import JADE, PACK, SECRET, add_update, give_key, standard_pack
from conftest import Api
from jvmfakes import PNG_HEADER
from test_access import new_friend

from pumpkin_manager import security

MODPACK = {"source": "curseforge", "project": str(PACK)}


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)


async def create(api: Api, name: str = "Alpha", **body) -> dict:
    row = await api.job(await api.call("POST", "/api/servers", json={"name": name, "engine": "java", "eula": True, "modpack": MODPACK, "start": False, **body}))
    assert row["status"] == "success", row["message"]
    return next(s for s in (await api.call("GET", "/api/servers")).json() if s["name"] == name)


async def test_the_curseforge_key_is_checked_saved_and_never_sent_back(jvm_live):
    world, api, ctx = jvm_live
    await api.login()
    assert (await api.call("GET", "/api/modpacks/sources")).json() == {"modrinth": {"enabled": True}, "curseforge": {"enabled": False, "search": None}}
    settings = (await api.call("GET", "/api/settings")).json()
    assert settings["has_curseforge_key"] is False and "curseforge_api_key" not in settings

    wrong = await api.call("PUT", "/api/settings", json={"curseforge_api_key": "00000000-0000-0000-0000-000000000000"})
    assert wrong.status_code == 422 and "did not accept the API key" in wrong.json()["detail"]
    assert not ctx.app_settings().curseforge_api_key, "a key that was refused is not saved"

    saved = await api.call("PUT", "/api/settings", json={"curseforge_api_key": f"  {world.cf.key}\n"})
    assert saved.status_code == 200 and saved.json()["has_curseforge_key"] is True
    assert "curseforge_api_key" not in saved.json() and world.cf.key not in saved.text
    assert ctx.app_settings().curseforge_api_key == world.cf.key, "stored without the spaces around it"
    assert world.cf.key not in (await api.call("GET", "/api/settings")).text
    assert (await api.call("GET", "/api/modpacks/sources")).json()["curseforge"]["enabled"] is True

    other = await api.call("PUT", "/api/settings", json={"update_check_minutes": 120})
    assert other.json()["has_curseforge_key"] is True, "saving other settings leaves the key alone"

    cleared = await api.call("PUT", "/api/settings", json={"curseforge_api_key": "  "})
    assert cleared.json()["has_curseforge_key"] is False and ctx.app_settings().curseforge_api_key is None
    assert (await api.call("GET", "/api/modpacks/sources")).json()["curseforge"]["enabled"] is False

    object.__setattr__(ctx.settings, "curseforge_api_key", world.cf.key)  # what CURSEFORGE_API_KEY does at startup (the settings are frozen)
    from_environment = (await api.call("GET", "/api/settings")).json()
    assert from_environment["has_curseforge_key"] is True and from_environment["curseforge_key_from_environment"] is True
    assert world.cf.key not in str(from_environment)


async def test_curseforge_packs_can_be_found_and_inspected(jvm_live):
    world, api, ctx = jvm_live
    standard_pack(world)
    assert (await api.call("GET", "/api/modpacks/search", params={"source": "curseforge"})).status_code == 401, "sign in first"
    await api.login()
    keyless = await api.call("GET", "/api/modpacks/search", params={"source": "curseforge", "q": "alpha"})
    assert keyless.status_code == 400 and "API key" in keyless.json()["detail"]

    give_key(ctx, world.cf.key)
    found = (await api.call("GET", "/api/modpacks/search", params={"source": "curseforge", "q": "alpha"})).json()
    assert found["search"] is True and found["total"] == 1
    hit = found["hits"][0]
    assert (hit["id"], hit["title"], hit["source"], hit["slug"]) == ("1000", "Alpha Pack", "curseforge", "alpha-pack")
    assert hit["minecraft"] == ["1.21.1"] and hit["loaders"] == ["neoforge"]

    world.cf.search_enabled = False
    limited = (await api.call("GET", "/api/modpacks/search", params={"source": "curseforge"})).json()
    assert limited["search"] is False and "Project ID" in limited["note"] and [h["id"] for h in limited["hits"]] == ["1000"]
    assert (await api.call("GET", "/api/modpacks/sources")).json()["curseforge"] == {"enabled": True, "search": False}

    project = (await api.call("GET", "/api/modpacks/project", params={"ref": "1000", "source": "curseforge"})).json()
    assert project["title"] == "Alpha Pack" and project["source"] == "curseforge" and project["page"].endswith("/modpacks/alpha-pack")
    version = project["versions"][0]
    assert (version["id"], version["number"], version["type"], version["minecraft"], version["loaders"], version["server_pack"]) == (
        "5000", "1.0.0", "release", ["1.21.1"], ["neoforge"], False,
    )
    assert (await api.call("GET", "/api/modpacks/project", params={"ref": "1000"})).json()["source"] == "curseforge", "a plain number is a CurseForge project"
    unknown = await api.call("GET", "/api/modpacks/project", params={"ref": "999999", "source": "curseforge"})
    assert unknown.status_code == 400 and "does not know" in unknown.json()["detail"]
    mod = await api.call("GET", "/api/modpacks/project", params={"ref": "2001", "source": "curseforge"})
    assert mod.status_code == 400 and "is not a modpack" in mod.json()["detail"]
    assert (await api.call("GET", "/api/modpacks/search", params={"source": "elsewhere"})).status_code == 422

    logo = world.cf.mods[PACK]["logo"]["thumbnailUrl"]
    icon = await api.call("GET", "/api/modpacks/icon", params={"url": logo})
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png" and icon.content.startswith(PNG_HEADER)
    assert (await api.call("GET", "/api/modpacks/icon", params={"url": "https://evil.example/logo.png"})).status_code == 400


async def test_a_curseforge_pack_file_can_be_uploaded_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    file = standard_pack(world)
    await api.login()
    uploaded = await api.call("PUT", "/api/modpacks/upload", content=world.cf.blobs[file["id"]])
    assert uploaded.status_code == 200, uploaded.text
    info = uploaded.json()
    assert info["pack"]["name"] == "Alpha Pack" and info["pack"]["source"] == "curseforge" and info["pack"]["mods"] == 3
    assert info["pack"]["loader"]["label"] == "NeoForge 21.1.77"
    refused = await api.call("PUT", "/api/modpacks/upload", content=b"definitely not a modpack")
    assert refused.status_code == 400


async def test_a_curseforge_server_is_created_checked_and_updated_through_the_api(jvm_live):
    world, api, ctx = jvm_live
    standard_pack(world)
    await api.login()
    assert (await api.call("PUT", "/api/settings", json={"curseforge_api_key": world.cf.key})).status_code == 200

    server = await create(api, "Alpha")
    sid = server["id"]
    assert server["modpack"]["source"] == "curseforge" and server["modpack"]["title"] == "Alpha Pack" and server["modpack"]["manual"] == []
    assert server["modpack"]["icon"].startswith("https://media.forgecdn.net/") and server["modpack"]["version_number"] == "1.0.0"
    detail = (await api.call("GET", f"/api/servers/{sid}/modpack")).json()
    assert detail["mods"] == ["mods/create.jar", "mods/lithium.jar"] and detail["mod_count"] == 2 and detail["config_files"] == 1

    add_update(world)
    checked = (await api.call("POST", f"/api/servers/{sid}/modpack/check")).json()
    assert checked["servers_with_updates"] == 1 and checked["update"]["available"] is True
    versions = (await api.call("GET", f"/api/servers/{sid}/modpack/versions")).json()
    assert versions["current"] == "5000" and [v["id"] for v in versions["versions"]] == ["5100", "5000"]
    assert versions["pending"]["id"] == "5100"

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/modpack/update", json={}), timeout=120)
    assert row["status"] == "success", row["message"]
    after = (await api.call("GET", f"/api/servers/{sid}")).json()
    assert after["modpack"]["version_number"] == "1.1.0" and after["modpack"]["can_roll_back"] is True

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/modpack/rollback"), timeout=120)
    assert row["status"] == "success", row["message"]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["modpack"]["version_number"] == "1.0.0"


async def test_the_files_to_add_by_hand_are_for_those_who_manage_the_server(jvm_live):
    world, api, ctx = jvm_live
    cf = world.cf
    cf.add_mod(2001, 3001, JADE, name="Jade")
    cf.add_mod(2004, 3004, SECRET, name="Secret Sauce", downloadable=False)
    cf.add_project(PACK, "Alpha Pack", class_id=4471)
    cf.add_pack_version(PACK, 5000, "1.0.0", mods=((2001, 3001), (2004, 3004)))
    await api.login()
    give_key(ctx, cf.key)
    sid = (await create(api, "Waiting"))["id"]
    ada, _, _ = await new_friend(api, "ada", {sid: "admin"})
    pia, _, _ = await new_friend(api, "pia", {sid: "player"})
    try:
        owner_view = (await api.call("GET", f"/api/servers/{sid}")).json()["modpack"]
        assert [m["path"] for m in owner_view["manual"]] == ["mods/secret.jar"] and owner_view["manual"][0]["name"] == "Secret Sauce"
        assert owner_view["manual"][0]["page"].endswith("/mc-mods/secret-sauce/files/3004")
        assert [m["path"] for m in (await ada.call("GET", f"/api/servers/{sid}")).json()["modpack"]["manual"]] == ["mods/secret.jar"]
        seen = (await pia.call("GET", f"/api/servers/{sid}")).json()["modpack"]
        assert seen["title"] == "Alpha Pack" and "manual" not in seen and "pending" not in seen, "which pack it is is for everybody, the rest for those who manage it"
        for friend in (ada, pia):
            assert (await friend.call("GET", "/api/modpacks/sources")).status_code == 403, "finding packs is the administrator's"
            assert (await friend.call("GET", "/api/modpacks/search", params={"source": "curseforge"})).status_code == 403
            assert (await friend.call("PUT", "/api/settings", json={"curseforge_api_key": cf.key})).status_code == 403
            assert cf.key not in (await friend.call("GET", "/api/settings")).text
    finally:
        await ada.http.aclose()
        await pia.http.aclose()
