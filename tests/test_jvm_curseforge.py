"""CurseForge modpacks: the API client, reading packs, and creating servers from them (list of files, server pack, files to add by hand)."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import pytest_asyncio
from cftools import (
    JADE,
    MINIMAP,
    NEW,
    NEWEST,
    OLD,
    PACK,
    SECRET,
    SERVER_FILES,
    create_cf,
    files_under,
    give_key,
    standard_pack,
    steps_of,
    try_create_cf,
    zip_at,
)
from conftest import create_java_server, finish
from jvmfakes import PNG_HEADER, FakeMod
from jvmworld import CREATE, LITHIUM

from pumpkin_manager.jvm import archive, curseforge
from pumpkin_manager.jvm.mrpack import ModCache, PackError
from pumpkin_manager.jvm.net import DownloadError
from pumpkin_manager.jvm.properties import parse as parse_properties
from pumpkin_manager.servers import ServerError
from pumpkin_manager.util import plain_environment


@pytest_asyncio.fixture
async def cfjvm(jvm):
    """The fake Java world with a CurseForge API key set. Yields (world, ctx)."""
    world, ctx = jvm
    give_key(ctx, world.cf.key)
    return world, ctx


def props_of(ctx, rec) -> dict[str, str]:
    return parse_properties((ctx.servers.server_dir(rec.id) / "server.properties").read_text(encoding="utf-8"))


async def stream(data: bytes):
    for start in range(0, len(data), 1000):  # arrives in pieces, like a request body
        yield data[start:start + 1000]


# ------------------------------------------------------------------------------------------- reading packs
def manifest_of(**changes):
    manifest = {
        "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.2.0", "primary": True}]}, "manifestType": "minecraftModpack",
        "manifestVersion": 1, "name": "Some Pack", "version": "2.0", "author": "someone", "overrides": "overrides",
        "files": [{"projectID": 238222, "fileID": 4000000, "required": True}],
    }
    manifest.update(changes)
    return manifest


def pack_with(tmp_path, manifest) -> object:
    return zip_at(tmp_path / "pack.zip", {"manifest.json": json.dumps(manifest).encode(), "overrides/config/a.toml": b"a"})


def test_a_curseforge_manifest_is_read(tmp_path):
    manifest = curseforge.read_manifest(pack_with(tmp_path, manifest_of()))
    assert (manifest.name, manifest.version, manifest.author, manifest.minecraft) == ("Some Pack", "2.0", "someone", "1.20.1")
    assert (manifest.loader.kind, manifest.loader.version) == ("forge", "47.2.0")
    assert manifest.files == [(238222, 4000000)] and manifest.overrides == "overrides"


def test_the_primary_mod_loader_is_the_one_used(tmp_path):
    loaders = [{"id": "fabric-0.16.9"}, {"id": "forge-47.2.0", "primary": True}]
    manifest = curseforge.read_manifest(pack_with(tmp_path, manifest_of(minecraft={"version": "1.20.1", "modLoaders": loaders})))
    assert manifest.loader.kind == "forge"
    only = curseforge.read_manifest(pack_with(tmp_path, manifest_of(minecraft={"version": "1.20.1", "modLoaders": [{"id": "neoforge-47.1.106"}]})))
    assert only.loader.kind == "neoforge", "a single loader is used even when it is not marked primary"


@pytest.mark.parametrize("changes,message", [
    ({"manifestType": "somethingElse"}, "not a Minecraft modpack"),
    ({"manifestVersion": 2}, "not a Minecraft modpack"),
    ({"minecraft": {"version": "1.20.1", "modLoaders": []}}, "one Minecraft version and one mod loader"),
    ({"minecraft": {"version": "", "modLoaders": [{"id": "forge-47.2.0"}]}}, "one Minecraft version and one mod loader"),
    ({"minecraft": {"version": "1.12.2", "modLoaders": [{"id": "liteloader-1.12"}]}}, "cannot install"),
    ({"minecraft": {"version": "1.12.2", "modLoaders": [{"id": "forge-"}]}}, "cannot install"),
    ({"files": "everything"}, "no files"),
    ({"files": [{"projectID": "238222", "fileID": 1}]}, "damaged"),
    ({"files": [{"projectID": True, "fileID": 1}]}, "damaged"),
    ({"files": [{"projectID": 1, "fileID": -4}]}, "damaged"),
    ({"files": [{"projectID": 1}]}, "damaged"),
    ({"files": [{"projectID": 1, "fileID": 1}] * 5001}, "no files or more than"),
    ({"overrides": "../../etc"}, "outside the server folder"),
    ({"overrides": "/etc"}, "absolute path"),
])
def test_a_manifest_that_is_wrong_or_hostile_is_refused(tmp_path, changes, message):
    with pytest.raises(PackError, match=message):
        curseforge.read_manifest(pack_with(tmp_path, manifest_of(**changes)))


@pytest.mark.parametrize("pack,display,expected", [
    ("RLCraft", "RLCraft 1.12.2 - Release v2.9.3.zip", "1.12.2 - Release v2.9.3"),
    ("Prominence™ II: Hasturian Era", "Prominence™ II Hasturian Era v4.1.3", "v4.1.3"),
    ("Alpha Pack", "Alpha Pack 1.0.0", "1.0.0"),
    ("Alpha Pack", "ALPHAPACK-2.0", "2.0"),
    ("All the Mods 10 - ATM10", "All the Mods 10-8.2", "All the Mods 10-8.2"),
    ("Alpha Pack", "Alpha Pack", "Alpha Pack"),
    ("Alpha Pack", "Beta 2.0.zip", "Beta 2.0"),
    ("Cobblemon Official Modpack [Fabric]", "Cobblemon Official Modpack [Fabric] 1.8", "1.8"),
    ("Pack (Forge)", "Pack (Forge) - v2", "v2"),
    ("Pack", "Pack (1.2)", "(1.2)"),
    ("Pack", "Pack – v3", "v3"),
    ("Alpha Pack", "", ""),
    ("", "Some File.zip", "Some File"),
    ("Alpha Pack", None, ""),
])
def test_a_version_is_named_without_the_packs_own_name_in_front(pack, display, expected):
    assert curseforge.version_name(pack, display) == expected


def test_files_that_are_not_curseforge_packs_are_refused(tmp_path):
    with pytest.raises(PackError, match="not a valid zip"):
        curseforge.read_manifest(tmp_path.joinpath("x.zip").write_bytes(b"nope") and tmp_path / "x.zip")
    with pytest.raises(PackError, match="no manifest.json"):
        curseforge.read_manifest(zip_at(tmp_path / "empty.zip", {"readme.txt": b"hi"}))
    with pytest.raises(PackError, match="cannot be read"):
        curseforge.read_manifest(zip_at(tmp_path / "bad.zip", {"manifest.json": b"{ not json"}))
    assert curseforge.is_curseforge_pack(pack_with(tmp_path, manifest_of()))
    assert not curseforge.is_curseforge_pack(zip_at(tmp_path / "mr.zip", {"manifest.json": b"{}", "modrinth.index.json": b"{}"}))
    assert not curseforge.is_curseforge_pack(zip_at(tmp_path / "other.zip", {"readme.txt": b"hi"}))
    assert not curseforge.is_curseforge_pack(tmp_path / "missing.zip")


@pytest.mark.parametrize("text,expected", [
    ("715572", ("id", "715572")),
    ("  715572\n", ("id", "715572")),
    ("https://www.curseforge.com/minecraft/modpacks/all-the-mods-9", ("slug", "all-the-mods-9")),
    ("https://www.curseforge.com/minecraft/modpacks/All-The-Mods-9/files/6107778", ("slug", "all-the-mods-9")),
    ("curseforge.com/minecraft/modpacks/rlcraft?x=1", ("slug", "rlcraft")),
])
def test_a_project_is_named_by_its_number_or_the_address_of_its_page(text, expected):
    assert curseforge.parse_project_reference(text) == expected


@pytest.mark.parametrize("text", ["", "all-the-mods-9", "https://www.curseforge.com/minecraft/mc-mods/jei", "https://example.com/minecraft/modpacks/x/..", "12 34"])
def test_other_references_are_explained(text):
    with pytest.raises(curseforge.CurseForgeError, match="project number"):
        curseforge.parse_project_reference(text)


@pytest.mark.parametrize("site,expected", [
    ("https://www.curseforge.com/minecraft/mc-mods/jei", "https://www.curseforge.com/minecraft/mc-mods/jei/files/42"),
    ("https://www.curseforge.com/minecraft/mc-mods/jei/", "https://www.curseforge.com/minecraft/mc-mods/jei/files/42"),
    ("https://CurseForge.com/minecraft/mc-mods/jei", "https://curseforge.com/minecraft/mc-mods/jei/files/42"),
    ("http://www.curseforge.com/minecraft/mc-mods/jei", ""),
    ("https://evil.example/minecraft/mc-mods/jei", ""),
    ("https://www.curseforge.com.evil.example/x", ""),
    ("https://user:pw@www.curseforge.com/x", ""),
    ("https://www.curseforge.com:8443/x", ""),
    ("https://www.curseforge.com:notaport/x", ""),
    ("javascript:alert(1)", ""),
    ("", ""),
    (None, ""),
    (42, ""),
])
def test_only_real_curseforge_pages_are_linked(site, expected):
    assert curseforge.mod_page(site, 42) == expected


def test_a_server_pack_is_read_before_anything_is_written(tmp_path):
    pack = archive.ServerPack(zip_at(tmp_path / "s.zip", {f"Server-Files-1.0/{name}": data for name, data in SERVER_FILES.items()}))
    assert pack.prefix == "Server-Files-1.0/" and pack.mod_count == 2 and pack.usable
    names = {rel for rel, _ in pack.entries}
    assert names == {
        "mods/lithium.jar", "mods/create.jar", "config/create.toml", "config/other.toml", "kubejs/server_scripts/main.js", "README.txt",
        "server.properties",
    }
    assert sum(pack.skipped.values()) == 8, "scripts, the installer, the loader's libraries, logs and the lists the manager keeps"
    flat = archive.ServerPack(zip_at(tmp_path / "flat.zip", {"mods/a.jar": b"a", "config/a.toml": b"a"}))
    assert flat.prefix == "" and flat.mod_count == 1
    configs_only = archive.ServerPack(zip_at(tmp_path / "config.zip", {"config/a.toml": b"a", "start.sh": b"x"}))
    assert not configs_only.usable, "server files without mods are no use here"


@pytest.mark.parametrize("entries,message", [
    ({"mods/../../evil.jar": b"x"}, "outside the server folder"),
    ({"/etc/cron.d/evil": b"x"}, "outside the server folder"),
    ({"../Server-Files/mods/a.jar": b"x"}, "outside the server folder"),
    ({"C:/evil.jar": b"x"}, "outside the server folder"),
    ({"Server-Files\\..\\..\\evil.jar": b"x"}, "outside the server folder"),
    ({"mods/a.jar": b"x", "MODS/A.JAR": b"y"}, "twice"),
    ({"config/eula.txt": b"eula=false"}, "looks after"),
])
def test_a_hostile_server_pack_is_refused(tmp_path, entries, message):
    with pytest.raises(PackError, match=message):
        archive.ServerPack(zip_at(tmp_path / "bad.zip", entries))


def test_links_and_the_managers_own_folder_are_never_unpacked(tmp_path):
    path = zip_at(
        tmp_path / "s.zip", {"mods/a.jar": b"mod", ".manager/pack.json": b"{}", "__MACOSX/mods/._a.jar": b"junk"}, symlinks={"config/link": "/etc/passwd"},
    )
    pack = archive.ServerPack(path)
    assert [rel for rel, _ in pack.entries] == ["mods/a.jar"]
    target = tmp_path / "server"
    target.mkdir()
    mods, others = archive.install(pack, target, lambda fraction, message: None)
    assert mods == {"mods/a.jar": "sha256:" + hashlib.sha256(b"mod").hexdigest()} and others == {}
    assert sorted(p.name for p in target.rglob("*") if p.is_file()) == ["a.jar"]
    with pytest.raises(PackError, match="not a usable zip"):
        archive.ServerPack(tmp_path / "missing.zip")


def test_leftovers_of_an_interrupted_curseforge_install_are_wiped_when_the_cache_opens(tmp_path):
    leftover = tmp_path / "mods" / "curseforge-staging" / "stage1" / "ab" / ("ab" + "0" * 38)
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"half an install")
    kept = tmp_path / "mods" / "cd" / ("cd" + "0" * 126)
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"a mod from Modrinth")
    ModCache(tmp_path / "mods")
    assert not leftover.exists() and kept.exists()


def test_the_managers_secrets_do_not_reach_the_programs_it_starts(monkeypatch):
    for name in ("ADMIN_PASSWORD", "GITHUB_TOKEN", "CURSEFORGE_API_KEY", "SOME_OTHER_VARIABLE"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = plain_environment()
    assert env["PATH"] == "/usr/bin" and not {"ADMIN_PASSWORD", "GITHUB_TOKEN", "CURSEFORGE_API_KEY", "SOME_OTHER_VARIABLE"} & set(env)
    assert plain_environment(["SOME_OTHER_VARIABLE"])["SOME_OTHER_VARIABLE"] == "secret", "what the administrator allows is passed on"


# ------------------------------------------------------------------------------------------- the API client
async def test_the_client_lists_the_files_of_a_pack_across_pages_without_its_server_packs(cfjvm):
    world, ctx = cfjvm
    world.cf.add_project(PACK, "Alpha Pack", class_id=4471)
    for n in range(120):
        world.cf._register(PACK, 7000 + n, f"alpha-{n}.zip", b"zip %d" % n, date=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}.000Z", release=1 if n % 3 else 2)
    world.cf._register(PACK, 7999, "Server-Files.zip", b"server", server_pack_of=7119, date="2026-12-31T00:00:00.000Z")
    files = await ctx.servers.jvm.curseforge.files(PACK)
    assert len(files) == 120 and files[0]["id"] == 7119 and files[-1]["id"] == 7000, "newest first, all pages, no server packs"
    versions = await ctx.servers.jvm.pack_versions("curseforge", str(PACK))
    assert versions[0]["id"] == "7119" and versions[0]["type"] == "release" and versions[2]["type"] == "beta"


async def test_many_files_are_looked_up_in_batches(cfjvm):
    world, ctx = cfjvm
    for n in range(450):
        world.cf._register(2000 + n, 100_000 + n, f"mod-{n}.jar", b"mod %d" % n)
    found = await ctx.servers.jvm.curseforge.resolve_files([100_000 + n for n in range(450)] + [999_999_999])
    assert len(found) == 450 and 999_999_999 not in found
    assert world.cf.api_calls.count("POST /v1/mods/files") == 3, "200 ids per request"


async def test_a_file_is_downloaded_only_with_a_checksum_and_an_address(cfjvm, tmp_path):
    world, ctx = cfjvm
    client = ctx.servers.jvm.curseforge
    good = world.cf._register(5, 50, "good.jar", b"good bytes")
    await client.download(good, tmp_path / "good.jar", what="good.jar")
    assert (tmp_path / "good.jar").read_bytes() == b"good bytes"
    assert world.net.keys_sent_to == {"edge.forgecdn.net"}, "CurseForge's download servers get the key: it tells them whose download it is"

    secret = world.cf._register(5, 51, "secret.jar", b"x", downloadable=False)
    with pytest.raises(curseforge.CurseForgeError, match="does not allow it to be downloaded automatically"):
        await client.download(secret, tmp_path / "secret.jar", what="Secret Sauce")
    nosum = {**good, "hashes": []}
    with pytest.raises(curseforge.CurseForgeError, match="no checksum"):
        await client.download(nosum, tmp_path / "nosum.jar", what="nosum.jar")

    world.net.blob(good["downloadUrl"], b"tampered bytes")
    with pytest.raises(DownloadError):
        await client.download(good, tmp_path / "bad.jar", what="good.jar")
    assert not (tmp_path / "bad.jar").exists(), "a file that does not match its checksum is not kept"
    elsewhere = {**good, "downloadUrl": "https://evil.example/good.jar"}
    with pytest.raises(DownloadError):
        await client.download(elsewhere, tmp_path / "evil.jar", what="good.jar")
    assert not any("evil.example" in url for url in world.net.requests), "only CurseForge's own hosts are contacted"


async def test_the_key_never_follows_a_redirect_away_from_curseforge(cfjvm, tmp_path):
    world, ctx = cfjvm
    client = ctx.servers.jvm.curseforge
    content = b"the real bytes"
    file = world.cf._register(5, 60, "moved.jar", content)
    moved = "https://mediafilez.forgecdn.net/files/0/60/moved.jar"

    # the way CurseForge's servers do it: edge.forgecdn.net sends the download on to mediafilez.forgecdn.net
    world.net.route(file["downloadUrl"], lambda request: httpx.Response(302, headers={"Location": moved}))
    world.net.blob(moved, content)
    await client.download(file, tmp_path / "moved.jar", what="moved.jar")
    assert (tmp_path / "moved.jar").read_bytes() == content
    assert world.net.keys_sent_to == {"edge.forgecdn.net", "mediafilez.forgecdn.net"}

    # anywhere else is refused, and nothing is sent there
    world.net.keys_sent_to.clear()
    world.net.route(file["downloadUrl"], lambda request: httpx.Response(302, headers={"Location": "https://evil.example/moved.jar"}))
    world.net.blob("https://evil.example/moved.jar", b"evil")
    with pytest.raises(DownloadError, match="not one of the allowed hosts"):
        await client.download(file, tmp_path / "evil.jar", what="moved.jar")
    world.net.route(file["downloadUrl"], lambda request: httpx.Response(302, headers={"Location": "http://mediafilez.forgecdn.net/files/0/60/moved.jar"}))
    with pytest.raises(DownloadError, match="only https"):
        await client.download(file, tmp_path / "plain.jar", what="moved.jar")
    assert not any("evil.example" in url for url in world.net.requests) and "evil.example" not in world.net.keys_sent_to
    assert not (tmp_path / "evil.jar").exists() and not (tmp_path / "plain.jar").exists()

    # an address that only leads to itself ends somewhere
    world.net.route(file["downloadUrl"], lambda request: httpx.Response(302, headers={"Location": file["downloadUrl"]}))
    with pytest.raises(DownloadError, match="redirects too many times"):
        await client.download(file, tmp_path / "loop.jar", what="moved.jar")

    # and a download server that does not accept the key says so, once
    attempts = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(401, json={"message": "A valid api-key is required."})

    world.net.route(file["downloadUrl"], refuse)
    with pytest.raises(curseforge.CurseForgeError, match="check the key in Settings"):
        await client.download(file, tmp_path / "refused.jar", what="moved.jar")
    assert len(attempts) == 1, "asking again with the same key cannot help"


async def test_mods_that_curseforge_serves_through_a_redirect_are_installed_too(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    moved = "https://mediafilez.forgecdn.net/files/3/1/lithium.jar"
    world.net.route(world.cf.files[3001]["downloadUrl"], lambda request: httpx.Response(302, headers={"Location": moved}))
    world.net.blob(moved, LITHIUM.content)
    rec, _ = await create_cf(ctx)
    assert (ctx.servers.server_dir(rec.id) / "mods" / "lithium.jar").read_bytes() == LITHIUM.content
    assert world.net.keys_sent_to == {"api.curseforge.com", "edge.forgecdn.net", "mediafilez.forgecdn.net"}


async def test_search_finds_modpacks_only(cfjvm):
    world, ctx = cfjvm
    world.cf.add_project(PACK, "Alpha Pack", class_id=4471)
    world.cf.add_project(1001, "Alpha Pack Reloaded", class_id=4471)
    world.cf.add_project(2001, "Alpha Mod", class_id=6)
    found = await ctx.servers.jvm.search_packs("alpha", source="curseforge")
    assert found["search"] is True and [h["title"] for h in found["hits"]] == ["Alpha Pack Reloaded", "Alpha Pack"], "most downloaded first"
    assert found["hits"][0]["source"] == "curseforge" and found["hits"][0]["icon_url"].startswith("https://media.forgecdn.net/")
    assert ctx.servers.jvm.sources() == {"modrinth": {"enabled": True}, "curseforge": {"enabled": True, "search": True}}


async def test_a_key_that_cannot_search_still_finds_packs_by_project_number(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    world.cf.search_enabled = False
    jvm = ctx.servers.jvm
    assert jvm.sources()["curseforge"] == {"enabled": True, "search": None}, "not known before the first search"

    found = await jvm.search_packs("alpha", source="curseforge")
    assert found["search"] is False and "Project ID" in found["note"] and [h["title"] for h in found["hits"]] == ["Alpha Pack"]
    assert (await jvm.search_packs("zzz", source="curseforge"))["hits"] == [], "the pack list is narrowed by what was typed"
    assert jvm.sources()["curseforge"] == {"enabled": True, "search": False}

    details = await jvm.pack_details("1000", "curseforge")
    assert details["title"] == "Alpha Pack" and details["source"] == "curseforge" and details["versions"][0]["number"] == "1.0.0"
    assert details["page"] == "https://www.curseforge.com/minecraft/modpacks/alpha-pack"
    assert (await jvm.pack_details("1000"))["source"] == "curseforge", "a plain number is a CurseForge project"
    with pytest.raises(ServerError, match="Project ID"):
        await jvm.pack_details("https://www.curseforge.com/minecraft/modpacks/alpha-pack", "curseforge")

    world.cf.search_enabled = True
    by_link = await jvm.pack_details("https://www.curseforge.com/minecraft/modpacks/alpha-pack", "curseforge")
    assert by_link["id"] == "1000"
    assert (await jvm.search_packs("alpha", source="curseforge"))["search"] is True
    assert jvm.sources()["curseforge"]["search"] is True


async def test_a_project_that_is_unknown_or_not_a_modpack_is_explained(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    with pytest.raises(ServerError, match="does not know"):
        await ctx.servers.jvm.pack_details("424242", "curseforge")
    with pytest.raises(ServerError, match="is not a modpack"):
        await ctx.servers.jvm.pack_details("2001", "curseforge")
    with pytest.raises(ServerError, match="no modpack at that address"):
        await ctx.servers.jvm.pack_details("https://www.curseforge.com/minecraft/modpacks/nothing-here", "curseforge")


async def test_curseforge_needs_a_key_and_says_so(jvm):
    world, ctx = jvm
    standard_pack(world)
    assert ctx.servers.jvm.sources()["curseforge"] == {"enabled": False, "search": None}
    row = await try_create_cf(ctx, "NoKey")
    assert row["status"] == "failed" and "API key" in row["message"] and ctx.repo.list_servers() == []
    with pytest.raises(ServerError, match="API key"):
        await ctx.servers.jvm.search_packs("alpha", source="curseforge")
    assert not any("curseforge.com" in url for url in world.net.requests), "nothing is sent anywhere without a key"

    give_key(ctx, "a-key-that-is-not-valid")
    row = await try_create_cf(ctx, "WrongKey")
    assert row["status"] == "failed" and "did not accept the API key" in row["message"] and "$2a$10$" in row["message"]
    assert ctx.repo.list_servers() == []


async def test_a_key_in_the_environment_is_used_when_none_was_saved(jvm, monkeypatch):
    world, ctx = jvm
    standard_pack(world)
    object.__setattr__(ctx.settings, "curseforge_api_key", world.cf.key)  # the settings object is frozen; this is what CURSEFORGE_API_KEY does at startup
    assert ctx.servers.jvm.sources()["curseforge"]["enabled"] is True
    rec, _ = await create_cf(ctx, "FromEnvironment")
    assert rec.spec["pack"]["source"] == "curseforge"
    give_key(ctx, "a-key-that-is-not-valid")
    assert (await try_create_cf(ctx, "SavedWins"))["status"] == "failed", "a key saved in the dashboard wins over the environment"


# ------------------------------------------------------------------------------- creating from the list of files
async def test_a_curseforge_modpack_is_installed_from_its_list_of_files(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, row = await create_cf(ctx, "Alpha", start=True)

    pack = rec.spec["pack"]
    assert (pack["source"], pack["project"], pack["slug"], pack["title"]) == ("curseforge", "1000", "alpha-pack", "Alpha Pack")
    assert (pack["version_id"], pack["version_number"], pack["server_pack"]) == ("5000", "1.0.0", False)
    assert pack["icon"].startswith("https://media.forgecdn.net/") and pack["version_type"] == "release"
    assert rec.spec["loader"] == {"kind": "neoforge", "version": "21.1.77"} and rec.spec["minecraft"] == "1.21.1" and rec.spec["java"] == 21

    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM.content and (directory / "mods" / "create.jar").read_bytes() == CREATE.content
    assert not (directory / "mods" / "minimap.jar").exists(), "CurseForge tags it as meant for clients"
    assert (directory / "config" / "create.toml").read_bytes() == b"speed = 1\n", "the pack's own configuration"

    lock = ctx.servers.jvm.lock_of(rec)
    assert (lock["source"], lock["mode"]) == ("curseforge", "files")
    assert set(lock["files"]) == {"mods/lithium.jar", "mods/create.jar"} and all(v.startswith("sha1:") for v in lock["files"].values())
    assert lock["client_only"] == ["mods/minimap.jar"] and set(lock["overrides"]) == {"config/create.toml"} and "manual" not in lock

    steps = steps_of(ctx, row)
    assert any("Leaving out 1 mod that only works on a client" in s for s in steps)
    assert ctx.pm.runtime(rec.id).state == "running"
    view = ctx.servers.view(rec)
    assert view["modpack"]["source"] == "curseforge" and view["modpack"]["manual"] == [] and view["modpack"]["client_only"] == ["mods/minimap.jar"]

    # CurseForge's terms ask for no cache of what its API hands out: nothing stays behind except the server itself.
    assert files_under(ctx.settings.cache_dir / "mods") == [] and files_under(ctx.settings.cache_dir / "uploads") == []
    assert world.net.keys_sent_to == {"api.curseforge.com", "edge.forgecdn.net"}, "the key goes to CurseForge's own servers and nowhere else"
    assert world.cf.key not in json.dumps(rec.spec) and world.cf.key not in str(steps)


async def test_curseforge_files_are_downloaded_again_for_the_next_server(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    await create_cf(ctx, "First")
    await create_cf(ctx, "Second")
    fetched = [url for url in world.net.requests if url.endswith("/lithium.jar")]
    assert len(fetched) == 2, "nothing from CurseForge is cached between installs"
    assert files_under(ctx.settings.cache_dir / "mods") == []


async def test_modrinth_is_asked_about_curseforge_files_as_well(cfjvm):
    world, ctx = cfjvm
    world.net.mod_sides((JADE, "unsupported"))  # CurseForge does not tag it as client-only, Modrinth knows better
    world.cf.add_mod(2005, 3005, JADE)
    standard_pack(world, mods=((2001, 3001), (2002, 3002), (2005, 3005)))
    rec, _ = await create_cf(ctx)
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and not (directory / "mods" / "jade.jar").exists()
    assert ctx.servers.view(rec)["modpack"]["client_only"] == ["mods/jade.jar"]


async def test_a_client_only_mod_that_another_curseforge_mod_needs_stays(cfjvm):
    world, ctx = cfjvm
    cf = world.cf
    cf.add_mod(2001, 3001, LITHIUM)
    cf.add_mod(2003, 3003, MINIMAP, client_only=True)  # tagged as meant for clients...
    cf.add_mod(2002, 3002, CREATE, requires=(2003,))  # ...but create lists it as a required dependency
    cf.add_project(PACK, "Alpha Pack", class_id=4471)
    cf.add_pack_version(PACK, 5000, "1.0.0", mods=((2001, 3001), (2002, 3002), (2003, 3003)), date=OLD)
    rec, row = await create_cf(ctx)
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "minimap.jar").is_file(), "create needs it"
    assert ctx.servers.view(rec)["modpack"]["client_only"] == []
    assert any("Keeping 1 mod that only works on a client, because other mods in the pack need it" in s for s in steps_of(ctx, row))


async def test_a_modrinth_outage_does_not_stop_a_curseforge_pack(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    world.net.lookup_status = 503
    rec, row = await create_cf(ctx)
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and not (directory / "mods" / "minimap.jar").exists(), "CurseForge's own tags still apply"
    assert any("Modrinth could not be asked" in s for s in steps_of(ctx, row))


async def test_leaving_client_only_mods_out_can_be_switched_off_for_curseforge_packs(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, leave_out_client_only=False)
    assert (ctx.servers.server_dir(rec.id) / "mods" / "minimap.jar").is_file()
    assert rec.spec["leave_out_client_only"] is False


async def test_the_newest_release_is_installed_unless_a_version_is_chosen(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    world.cf.add_mod(2001, 3011, FakeMod("mods/lithium.jar", b"lithium 2" * 50))
    world.cf.add_pack_version(PACK, 5100, "1.1.0", mods=((2001, 3011),), date=NEW)
    world.cf.add_pack_version(PACK, 5200, "1.2.0-beta", mods=((2001, 3001),), date=NEWEST, release=2)
    rec, _ = await create_cf(ctx, "Default")
    assert rec.spec["pack"]["version_id"] == "5100", "the newest release, not the newest file"
    chosen, _ = await create_cf(ctx, "Chosen", modpack={"version": "5000"})
    assert chosen.spec["pack"]["version_id"] == "5000" and chosen.spec["pack"]["version_number"] == "1.0.0"
    beta, _ = await create_cf(ctx, "Beta", modpack={"version": "5200"})
    assert beta.spec["pack"]["version_id"] == "5200" and beta.spec["pack"]["version_type"] == "beta"
    row = await try_create_cf(ctx, "Unknown", modpack={"version": "424242"})
    assert row["status"] == "failed" and "does not exist" in row["message"]


async def test_a_pack_can_be_named_by_the_link_to_its_page(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    rec, _ = await create_cf(ctx, modpack={"project": "https://www.curseforge.com/minecraft/modpacks/alpha-pack"})
    assert rec.spec["pack"]["project"] == "1000"
    row = await try_create_cf(ctx, "NotAPack", modpack={"project": "2001"})
    assert row["status"] == "failed" and "is not a modpack" in row["message"]
    row = await try_create_cf(ctx, "Nothing", modpack={"project": "hello"})
    assert row["status"] == "failed" and "project number" in row["message"]
    assert len(ctx.repo.list_servers()) == 1


async def test_a_forge_pack_gets_its_java_and_loader(cfjvm):
    world, ctx = cfjvm
    world.net.maven_installer("forge", "1.20.1", "47.2.0")
    world.cf.add_mod(2001, 3001, LITHIUM)
    world.cf.add_project(PACK, "Alpha Pack", class_id=4471)
    world.cf.add_pack_version(PACK, 5000, "1.0.0", mods=((2001, 3001),), minecraft="1.20.1", loader="forge-47.2.0", date=OLD)
    rec, _ = await create_cf(ctx, "Forge", start=True)
    assert (rec.spec["minecraft"], rec.spec["java"], rec.spec["loader"]) == ("1.20.1", 17, {"kind": "forge", "version": "47.2.0"})
    assert rec.spec["launch"]["argfile"].startswith("libraries/net/minecraftforge/forge/1.20.1-47.2.0/")
    assert ctx.pm.runtime(rec.id).state == "running"


# -------------------------------------------------------------------------------------- what goes wrong
async def test_a_file_that_does_not_match_its_checksum_stops_the_creation_and_leaves_nothing(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    world.net.blob(world.cf.files[3001]["downloadUrl"], b"not what CurseForge listed")
    row = await try_create_cf(ctx, "Tampered")
    assert row["status"] == "failed" and "lithium.jar" in row["message"]
    assert ctx.repo.list_servers() == [] and list(ctx.settings.servers_dir.iterdir()) == []
    assert files_under(ctx.settings.cache_dir / "mods") == [] and files_under(ctx.settings.cache_dir / "uploads") == []


async def test_a_pack_with_a_file_that_curseforge_no_longer_has_is_explained(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    del world.cf.files[3002]
    row = await try_create_cf(ctx, "Gone")
    assert row["status"] == "failed" and "no longer exist on CurseForge" in row["message"] and "3002" in row["message"]
    assert ctx.repo.list_servers() == [] and files_under(ctx.settings.cache_dir / "uploads") == []


async def test_files_that_are_not_mods_are_left_out_and_a_file_without_a_checksum_is_refused(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    world.cf.add_mod(2010, 3010, FakeMod("mods/shaders.zip", b"shaders"), name="Shaders", class_id=6552)
    world.cf.add_pack_version(PACK, 5001 + 100, "1.0.1", mods=((2001, 3001), (2010, 3010)), date=NEW)
    rec, row = await create_cf(ctx, "Shaders", modpack={"version": "5101"})
    assert not list((ctx.servers.server_dir(rec.id) / "mods").glob("shaders*")), "a shader pack is nothing for a server"
    assert any("1 files are not mods" in s for s in steps_of(ctx, row))

    world.cf.files[3001]["hashes"] = []
    row = await try_create_cf(ctx, "NoChecksum")
    assert row["status"] == "failed" and "no checksum" in row["message"]


async def test_a_file_name_that_could_escape_the_mods_folder_is_refused(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    for name in ("..\\..\\evil.jar", "../evil.jar", "C:evil.jar", "mods/sub/evil.jar"):
        world.cf.files[3001]["fileName"] = name
        row = await try_create_cf(ctx, "Escape")
        assert row["status"] == "failed" and "name that cannot be used" in row["message"], (name, row["message"])
    assert ctx.repo.list_servers() == [] and list(ctx.settings.servers_dir.iterdir()) == []
    assert not (ctx.settings.data_dir / "evil.jar").exists() and not (ctx.settings.servers_dir / "evil.jar").exists()


# ----------------------------------------------------------------------------------- mods to add by hand
async def test_mods_that_cannot_be_downloaded_automatically_are_listed_and_the_server_waits_for_them(cfjvm):
    world, ctx = cfjvm
    cf = world.cf
    cf.add_mod(2001, 3001, LITHIUM)
    cf.add_mod(2004, 3004, SECRET, name="Secret Sauce", downloadable=False)  # the author does not allow it
    cf.add_mod(2005, 3005, JADE, name="Jade")
    cf.files[3005]["downloadUrl"] = "https://evil.example/jade.jar"  # an address that is not CurseForge's: never contacted
    cf.add_project(PACK, "Alpha Pack", class_id=4471)
    cf.add_pack_version(PACK, 5000, "1.0.0", mods=((2001, 3001), (2004, 3004), (2005, 3005)), date=OLD)
    rec, row = await create_cf(ctx, "Waiting", start=True)

    steps = steps_of(ctx, row)
    assert any("2 mods cannot be downloaded automatically" in s for s in steps)
    assert any("not started yet" in s for s in steps)
    assert ctx.pm.runtime(rec.id).state != "running" and not rec.desired_running
    assert not any("secret.jar" in url or "evil.example" in url for url in world.net.requests), "nothing is fetched around the API"

    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and not (directory / "mods" / "secret.jar").exists()
    manual = ctx.servers.view(rec)["modpack"]["manual"]
    assert manual == [
        {"path": "mods/secret.jar", "name": "Secret Sauce", "page": "https://www.curseforge.com/minecraft/mc-mods/secret-sauce/files/3004"},
        {"path": "mods/jade.jar", "name": "Jade", "page": "https://www.curseforge.com/minecraft/mc-mods/jade/files/3005"},
    ]
    assert [m["path"] for m in ctx.servers.jvm.lock_of(rec)["manual"]] == ["mods/secret.jar", "mods/jade.jar"]

    (directory / "mods" / "secret.jar").write_bytes(SECRET.content)  # added by hand
    assert [m["path"] for m in ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["manual"]] == ["mods/jade.jar"]
    assert "mods/secret.jar" not in ctx.servers.jvm.lock_of(rec)["files"], "what somebody added is not the manager's business"


# ------------------------------------------------------------------------------- the author's server pack
async def test_the_authors_server_pack_is_used_when_there_is_one(cfjvm):
    world, ctx = cfjvm
    standard_pack(world, server_pack=SERVER_FILES, wrapper="Server-Files-1.0.0")
    rec, row = await create_cf(ctx, "Archive", start=True)

    assert rec.spec["pack"]["server_pack"] is True and rec.spec["pack"]["source"] == "curseforge"
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM.content and (directory / "mods" / "create.jar").read_bytes() == CREATE.content
    assert (directory / "config" / "create.toml").read_bytes() == b"speed = 5\n", "the server pack's configuration, not the launcher pack's"
    assert (directory / "kubejs" / "server_scripts" / "main.js").is_file() and (directory / "README.txt").read_bytes() == b"read me\n"
    values = props_of(ctx, rec)
    assert values["allow-flight"] == "true" and values["view-distance"] == "9" and values["server-port"] == str(rec.java_port)
    for left_out in ("start.sh", "start.bat", "forge-installer.jar", "libraries/foo/bar.jar", "logs/latest.log"):
        assert not (directory / left_out).exists(), left_out
    assert not (directory / "ops.json").exists() or "Cheater" not in (directory / "ops.json").read_text(), "the operators are the manager's to keep"
    assert (directory / "user_jvm_args.txt").read_text() == "# -Xmx4G\n", "the memory options are the manager's"
    assert "eula=true" in (directory / "eula.txt").read_text()

    lock = ctx.servers.jvm.lock_of(rec)
    assert lock["mode"] == "archive" and set(lock["files"]) == {"mods/lithium.jar", "mods/create.jar"}
    assert lock["files"]["mods/lithium.jar"] == "sha256:" + hashlib.sha256(LITHIUM.content).hexdigest()
    assert set(lock["overrides"]) == {"config/create.toml", "config/other.toml", "kubejs/server_scripts/main.js", "README.txt", "server.properties"}

    downloaded = sorted(url.rsplit("/", 1)[1] for url in world.net.requests if "forgecdn.net" in url)
    assert downloaded == ["Server-Files-1.0.0.zip", "alpha-pack-1.0.0.zip"], "the two zip files, not the mods one by one"
    assert not any("modrinth.com" in url for url in world.net.requests), "the author's own choice is not second-guessed"
    steps = steps_of(ctx, row)
    assert any("Using the pack's own server files (2 mods)" in s for s in steps)
    assert any("Installing 2 mods" in s for s in steps), "the count of mods is the server pack's, not the list's"
    assert any("Not taken over from the pack's server files" in s for s in steps)
    assert ctx.pm.runtime(rec.id).state == "running"
    assert files_under(ctx.settings.cache_dir / "uploads") == []


async def test_a_server_pack_without_a_wrapper_folder_works_too(cfjvm):
    world, ctx = cfjvm
    standard_pack(world, server_pack={"mods/lithium.jar": LITHIUM.content, "config/a.toml": b"a = 1\n", "run.sh": b"#!/bin/sh\n"})
    rec, _ = await create_cf(ctx)
    directory = ctx.servers.server_dir(rec.id)
    assert rec.spec["pack"]["server_pack"] is True
    assert (directory / "mods" / "lithium.jar").is_file() and (directory / "config" / "a.toml").is_file() and not (directory / "mods" / "create.jar").exists()


async def test_the_server_pack_can_be_switched_off(cfjvm):
    world, ctx = cfjvm
    standard_pack(world, server_pack=SERVER_FILES, wrapper="Server-Files-1.0.0")
    rec, _ = await create_cf(ctx, modpack={"server_pack": False})
    assert rec.spec["pack"]["server_pack"] is False
    assert (ctx.servers.server_dir(rec.id) / "config" / "create.toml").read_bytes() == b"speed = 1\n", "the list of files and the pack's own configuration"
    assert not any("Server-Files" in url for url in world.net.requests), "the server pack was never downloaded"


@pytest.mark.parametrize("how", ["no mods in it", "not downloadable"])
async def test_a_server_pack_that_is_no_use_falls_back_to_the_list_of_files(cfjvm, how):
    world, ctx = cfjvm
    if how == "no mods in it":
        standard_pack(world, server_pack={"config/a.toml": b"a = 1\n", "start.sh": b"#!/bin/sh\n"})
    else:
        standard_pack(world, server_pack=SERVER_FILES, wrapper="Server-Files-1.0.0", server_pack_downloadable=False)
    rec, row = await create_cf(ctx)
    assert rec.spec["pack"]["server_pack"] is False
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and (directory / "mods" / "create.jar").is_file()
    assert any("hold no mods" in s for s in steps_of(ctx, row)) is (how == "no mods in it")


async def test_a_hostile_server_pack_is_refused_and_leaves_nothing(cfjvm):
    world, ctx = cfjvm
    standard_pack(world, server_pack={"mods/a.jar": b"mod", "mods/../../escape.txt": b"gotcha"})
    row = await try_create_cf(ctx, "Hostile")
    assert row["status"] == "failed" and "outside the server folder" in row["message"]
    assert ctx.repo.list_servers() == [] and list(ctx.settings.servers_dir.iterdir()) == []
    assert files_under(ctx.settings.cache_dir / "uploads") == [] and not (ctx.settings.data_dir / "escape.txt").exists()


# ------------------------------------------------------------------------------------- uploading a pack file
async def test_a_curseforge_pack_file_can_be_uploaded_instead(cfjvm):
    world, ctx = cfjvm
    file = standard_pack(world)
    upload = await ctx.servers.jvm.accept_upload(stream(world.cf.blobs[file["id"]]))
    assert upload["pack"]["name"] == "Alpha Pack" and upload["pack"]["source"] == "curseforge" and upload["pack"]["mods"] == 3
    assert upload["pack"]["loader"]["label"] == "NeoForge 21.1.77" and upload["pack"]["minecraft"] == "1.21.1"

    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Uploaded", "engine": "java", "eula": True, "modpack": {"source": "upload", "upload": upload["upload"]}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    rec = next(s for s in ctx.repo.list_servers() if s.name == "Uploaded")
    assert {k: v for k, v in rec.spec["pack"].items() if k not in ("client_only", "client_only_count", "manual", "manual_count")} == {
        "source": "upload", "title": "Alpha Pack", "version_number": "1.0.0", "format": "curseforge",
    }
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and not (directory / "mods" / "minimap.jar").exists()
    assert (directory / "config" / "create.toml").is_file()
    assert files_under(ctx.settings.cache_dir / "uploads") == [], "a used upload is removed"
    assert ctx.servers.jvm.lock_of(rec)["source"] == "curseforge"


async def test_a_zip_that_is_no_modpack_is_not_accepted_as_an_upload(cfjvm, tmp_path):
    world, ctx = cfjvm
    junk = zip_at(tmp_path / "junk.zip", {"hello.txt": b"hi"}).read_bytes()
    with pytest.raises(Exception, match="not a Modrinth modpack"):
        await ctx.servers.jvm.accept_upload(stream(junk))
    assert files_under(ctx.settings.cache_dir / "uploads") == []


# ------------------------------------------------------------------------------------------ icons
async def test_pack_icons_come_from_curseforges_image_host_only(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    logo = world.cf.mods[PACK]["logo"]["thumbnailUrl"]
    data, kind = await ctx.servers.jvm.pack_icon(logo)
    assert kind == "image/png" and data.startswith(PNG_HEADER)
    assert "media.forgecdn.net" not in world.net.keys_sent_to, "the key does not go to the image host"
    for url in ("https://evil.example/logo.png", "http://media.forgecdn.net/logo.png", "https://edge.forgecdn.net/files/1/2/mod.jar"):
        with pytest.raises(ServerError):
            await ctx.servers.jvm.pack_icon(url)


async def test_modrinth_servers_still_work_next_to_curseforge_ones(cfjvm):
    world, ctx = cfjvm
    standard_pack(world)
    modrinth = await create_java_server(ctx, "FromModrinth", start=False)
    curse, _ = await create_cf(ctx, "FromCurseForge")
    assert modrinth.spec["pack"]["source"] == "modrinth" and curse.spec["pack"]["source"] == "curseforge"
    assert ctx.servers.jvm.lock_of(modrinth)["source"] == "modrinth" and ctx.servers.jvm.lock_of(modrinth)["mode"] == "files"
    assert all(len(v) == 128 for v in ctx.servers.jvm.lock_of(modrinth)["files"].values()), "Modrinth files are still recorded by SHA-512"
