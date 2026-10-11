"""Modrinth modpacks: finding them, reading .mrpack files as untrusted input, and installing them into a server folder."""

from __future__ import annotations

import io
import stat
import zipfile

import pytest
from jvmfakes import FakeInternet, FakeMod, FakeModrinth, mrpack_bytes

from pumpkin_manager.jvm.modrinth import ModrinthClient, parse_project_reference
from pumpkin_manager.jvm.mrpack import ModCache, PackError, PackInstaller, read_pack, safe_relative_path
from pumpkin_manager.jvm.net import BadDownloadError, DownloadError

LITHIUM = FakeMod("mods/lithium.jar", b"lithium" * 100)
CREATE = FakeMod("mods/create.jar", b"create" * 300)
JEI = FakeMod("mods/jei.jar", b"jei" * 50, server="unsupported")  # a client-side mod: not for the server
OPTIONAL = FakeMod("mods/spark.jar", b"spark" * 20, server="optional")


def write_pack(tmp_path, **options) -> tuple:
    pack = tmp_path / "pack.mrpack"
    pack.write_bytes(mrpack_bytes(**options))
    return pack


# ------------------------------------------------------------------------- finding packs
def test_a_pasted_reference_becomes_a_project_id_or_slug():
    assert parse_project_reference("cool-pack") == "cool-pack"
    assert parse_project_reference("  AbCdEf12 ") == "AbCdEf12"
    assert parse_project_reference("https://modrinth.com/modpack/cool-pack") == "cool-pack"
    assert parse_project_reference("modrinth.com/modpack/cool-pack/version/1.2.3") == "cool-pack"
    for bad in ("", "a", "../../etc/passwd", "cool pack", "https://example.com/modpack/x", "https://modrinth.com/mod/sodium", "https://modrinth.com/"):
        with pytest.raises(DownloadError):
            parse_project_reference(bad)


async def test_search_finds_modpacks_that_can_run_on_a_server():
    net = FakeInternet()
    FakeModrinth(net, slug="create-pack", title="Create Pack", project_id="CrEaTe001")
    FakeModrinth(net, slug="client-only", title="Create Client Only", project_id="ClIeNt001", server_side="unsupported")
    FakeModrinth(net, slug="a-mod", title="Create Mod", project_id="MoDmOd001", project_type="mod")
    client = ModrinthClient(net.client(), retry_delay=0)
    found = await client.search("create")
    assert [h["slug"] for h in found["hits"]] == ["create-pack"] and found["total"] == 1
    assert found["hits"][0]["title"] == "Create Pack" and found["hits"][0]["id"] == "CrEaTe001"


async def test_a_project_must_be_a_modpack_that_works_on_servers():
    net = FakeInternet()
    FakeModrinth(net, slug="cool-pack")
    FakeModrinth(net, slug="client-only", project_id="ClIeNt001", server_side="unsupported")
    FakeModrinth(net, slug="a-mod", project_id="MoDmOd001", project_type="mod")
    client = ModrinthClient(net.client(), retry_delay=0)
    assert (await client.project("https://modrinth.com/modpack/cool-pack"))["slug"] == "cool-pack"
    with pytest.raises(DownloadError, match="client-only"):
        await client.project("client-only")
    with pytest.raises(DownloadError, match="not a modpack"):
        await client.project("a-mod")
    with pytest.raises(DownloadError, match="does not know"):
        await client.project("no-such-pack")


async def test_versions_come_newest_first_and_only_those_with_a_pack_file():
    net = FakeInternet()
    pack = FakeModrinth(net)
    pack.add_version("1.0.0", published="2026-01-01T00:00:00Z")
    pack.add_version("1.1.0", published="2026-03-01T00:00:00Z", version_type="beta")
    pack.versions.append({"id": "NoPack001", "version_number": "0.9", "date_published": "2026-04-01T00:00:00Z", "files": [{"filename": "x.jar"}]})
    client = ModrinthClient(net.client(), retry_delay=0)
    versions = await client.versions("cool-pack")
    assert [v["version_number"] for v in versions] == ["1.1.0", "1.0.0"]


async def test_the_modpack_file_is_checked_against_its_checksum_and_its_host(tmp_path):
    net = FakeInternet()
    pack = FakeModrinth(net)
    version = pack.add_version("1.0.0", mods=[LITHIUM])
    client = ModrinthClient(net.client(), retry_delay=0)
    await client.download_pack(version, tmp_path / "ok.mrpack")
    assert read_pack(tmp_path / "ok.mrpack").name == "Cool Pack"

    net.blob(version["files"][0]["url"], b"not the file that was promised")
    with pytest.raises(BadDownloadError):
        await client.download_pack(version, tmp_path / "bad.mrpack")
    assert not (tmp_path / "bad.mrpack").exists()

    evil = {**version, "files": [{**version["files"][0], "url": "https://evil.example.com/pack.mrpack"}]}
    with pytest.raises(BadDownloadError, match="allowed hosts"):
        await client.download_pack(evil, tmp_path / "evil.mrpack")
    plain = {**version, "files": [{**version["files"][0], "url": "http://cdn.modrinth.com/pack.mrpack"}]}
    with pytest.raises(BadDownloadError, match="https"):
        await client.download_pack(plain, tmp_path / "plain.mrpack")


# ------------------------------------------------------------------------- reading packs
def test_a_pack_is_read_into_what_the_server_needs(tmp_path):
    index = read_pack(write_pack(tmp_path, name="Cool Pack", version_id="2.0", mods=[LITHIUM, CREATE, JEI, OPTIONAL], loader=("fabric", "0.16.9")))
    assert (index.name, index.version_id, index.minecraft) == ("Cool Pack", "2.0", "1.21.1")
    assert (index.loader.kind, index.loader.version) == ("fabric", "0.16.9")
    assert [f.path for f in index.server_files] == ["mods/lithium.jar", "mods/create.jar", "mods/spark.jar"], "client-only mods are not installed"
    assert index.summary_public()["mods"] == 3 and index.summary_public()["loader"]["label"] == "Fabric 0.16.9"


@pytest.mark.parametrize("path", ["../outside.jar", "mods/../../outside.jar", "/etc/passwd", "C:/Windows/x.dll", "C:\\x.dll", ".manager/pack.json",
                                  ".MANAGER/x", "config/eula.txt", "", "mods\\..\\x"])
def test_paths_that_could_leave_the_server_folder_or_replace_managed_files_are_refused(path):
    with pytest.raises(PackError):
        safe_relative_path(path)
    assert safe_relative_path("mods\\sub/./a.jar") == "mods/sub/a.jar"


def test_a_pack_that_lists_such_a_path_is_refused_as_a_whole(tmp_path):
    with pytest.raises(PackError, match="outside the server folder"):
        read_pack(write_pack(tmp_path, mods=[FakeMod("../../evil.jar", b"x")]))


@pytest.mark.parametrize("options,message", [
    ({"index_patch": {"formatVersion": 2}}, "format"),
    ({"index_patch": {"game": "hytale"}}, "format"),
    ({"index_patch": {"dependencies": {"minecraft": "1.21.1"}}}, "mod loader"),
    ({"index_patch": {"dependencies": {"minecraft": "1.21.1", "forge": "1", "fabric-loader": "2"}}}, "exactly one mod loader"),
    ({"index_patch": {"dependencies": {"fabric-loader": "0.16.9"}}}, "Minecraft version"),
    ({"mods": [LITHIUM, LITHIUM]}, "twice"),
    ({"mods": [LITHIUM], "file_patch": lambda e: e["hashes"].pop("sha512")}, "checksums"),
    ({"mods": [LITHIUM], "file_patch": lambda e: e.update(fileSize=-5)}, "size"),
    ({"mods": [LITHIUM], "file_patch": lambda e: e.update(downloads=["https://evil.example.com/x.jar"])}, "allowed host"),
    ({"mods": [LITHIUM], "file_patch": lambda e: e.update(downloads=["http://cdn.modrinth.com/x.jar"])}, "allowed host"),
])
def test_a_pack_with_bad_metadata_is_refused(tmp_path, options, message):
    with pytest.raises(PackError, match=message):
        read_pack(write_pack(tmp_path, **options))


def test_files_that_are_not_modpacks_are_refused(tmp_path):
    (tmp_path / "x.mrpack").write_bytes(b"this is not a zip file")
    with pytest.raises(PackError, match="not a valid zip"):
        read_pack(tmp_path / "x.mrpack")
    empty = io.BytesIO()
    zipfile.ZipFile(empty, "w").close()
    (tmp_path / "y.mrpack").write_bytes(empty.getvalue())
    with pytest.raises(PackError, match="no modrinth.index.json"):
        read_pack(tmp_path / "y.mrpack")


def test_a_client_only_mod_may_come_from_anywhere_because_it_is_never_downloaded(tmp_path):
    index = read_pack(write_pack(tmp_path, mods=[FakeMod("mods/hud.jar", b"x", server="unsupported", host="evil.example.com"), LITHIUM]))
    assert [f.path for f in index.server_files] == ["mods/lithium.jar"]


# ------------------------------------------------------------------------ installing packs
def installer_for(tmp_path, net: FakeInternet, **kwargs) -> PackInstaller:
    return PackInstaller(net.client(), ModCache(tmp_path / "cache"), retry_delay=0, **kwargs)


def serve(net: FakeInternet, *mods: FakeMod) -> None:
    """Make the mods downloadable (without the project and version pages around them)."""
    pack = FakeModrinth(net)
    for mod in mods:
        pack._serve_mod(mod)


async def test_a_pack_is_installed_with_its_mods_and_its_configuration(tmp_path):
    net = FakeInternet()
    serve(net, LITHIUM, CREATE, JEI, OPTIONAL)
    pack = write_pack(
        tmp_path, mods=[LITHIUM, CREATE, JEI, OPTIONAL], overrides={"config/a.toml": b"from overrides", "config/b.toml": b"base"},
        server_overrides={"config/b.toml": b"server wins", "server.properties": b"allow-flight=true\n"},
    )
    server = tmp_path / "server"
    server.mkdir()
    steps: list[tuple[float, str]] = []
    lock = await installer_for(tmp_path, net).install(read_pack(pack), pack, server, lambda f, m: steps.append((f, m)))

    assert (server / "mods" / "lithium.jar").read_bytes() == LITHIUM.content and (server / "mods" / "create.jar").read_bytes() == CREATE.content
    assert (server / "mods" / "spark.jar").exists(), "an optional mod is installed"
    assert not (server / "mods" / "jei.jar").exists(), "a client-only mod is not"
    assert (server / "config" / "a.toml").read_bytes() == b"from overrides"
    assert (server / "config" / "b.toml").read_bytes() == b"server wins", "server-overrides are applied last"
    assert (server / "server.properties").read_bytes() == b"allow-flight=true\n"
    assert set(lock["files"]) == {"mods/lithium.jar", "mods/create.jar", "mods/spark.jar"}
    assert set(lock["overrides"]) == {"config/a.toml", "config/b.toml", "server.properties"}
    assert (server / ".manager" / "pack.json").is_file()
    assert steps[0][0] == 0.0 and max(f for f, _ in steps) >= 0.9 and any("3 of 3" in m for _, m in steps)


async def test_a_mod_that_is_already_in_the_cache_is_not_downloaded_again(tmp_path):
    net = FakeInternet()
    serve(net, LITHIUM, CREATE)
    pack = write_pack(tmp_path, mods=[LITHIUM, CREATE])
    installer = installer_for(tmp_path, net)
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    await installer.install(read_pack(pack), pack, first, lambda f, m: None)
    await installer.install(read_pack(pack), pack, second, lambda f, m: None)
    assert net.mod_downloads == {LITHIUM.url: 1, CREATE.url: 1}
    assert (second / "mods" / "create.jar").read_bytes() == CREATE.content
    assert (second / "mods" / "create.jar").stat().st_size == len(CREATE.content)


async def test_a_pack_that_declares_a_file_one_byte_too_small_still_installs(tmp_path):
    """Real packs do this (Better MC lists one of its mods one byte short): the checksums are the proof, not the declared size."""
    net = FakeInternet()
    serve(net, LITHIUM)
    pack = write_pack(tmp_path, mods=[LITHIUM], file_patch=lambda e: e.update(fileSize=e["fileSize"] - 1))
    installer = installer_for(tmp_path, net)
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    await installer.install(read_pack(pack), pack, first, lambda f, m: None)
    assert (first / "mods" / "lithium.jar").read_bytes() == LITHIUM.content
    await installer.install(read_pack(pack), pack, second, lambda f, m: None)
    assert net.mod_downloads == {LITHIUM.url: 1}, "the cached copy is recognised although its size differs from the declared one"


async def test_a_download_far_larger_than_the_pack_says_is_stopped(tmp_path):
    net = FakeInternet()
    big = FakeMod("mods/big.jar", b"x" * (17 * 1024 * 1024))
    serve(net, big)
    pack = write_pack(tmp_path, mods=[big], file_patch=lambda e: e.update(fileSize=100))
    server = tmp_path / "server"
    server.mkdir()
    with pytest.raises(PackError, match="mods/big.jar could not be downloaded.*much larger"):
        await installer_for(tmp_path, net).install(read_pack(pack), pack, server, lambda f, m: None)
    assert not (server / "mods").exists()


async def test_two_paths_with_the_same_content_are_fetched_once(tmp_path):
    net = FakeInternet()
    twin = FakeMod("mods/twin-copy.jar", LITHIUM.content)
    serve(net, LITHIUM, twin)
    pack = write_pack(tmp_path, mods=[LITHIUM, twin])
    server = tmp_path / "s"
    server.mkdir()
    await installer_for(tmp_path, net).install(read_pack(pack), pack, server, lambda f, m: None)
    assert sum(net.mod_downloads.values()) == 1, "identical files are downloaded once"
    assert (server / "mods" / "lithium.jar").read_bytes() == (server / "mods" / "twin-copy.jar").read_bytes() == LITHIUM.content


async def test_a_mod_that_does_not_match_its_checksum_stops_the_installation(tmp_path):
    net = FakeInternet()
    serve(net, LITHIUM, CREATE)
    net.blob(CREATE.url, b"x" * len(CREATE.content))  # same size, other content
    pack = write_pack(tmp_path, mods=[LITHIUM, CREATE])
    server = tmp_path / "server"
    server.mkdir()
    with pytest.raises(PackError, match="mods/create.jar could not be downloaded.*checksum"):
        await installer_for(tmp_path, net).install(read_pack(pack), pack, server, lambda f, m: None)
    assert not (server / "mods").exists(), "nothing is placed in the server before every mod has been downloaded and checked"


async def test_a_mod_that_has_disappeared_is_reported_by_name(tmp_path):
    net = FakeInternet()
    pack = write_pack(tmp_path, mods=[LITHIUM])
    server = tmp_path / "server"
    server.mkdir()
    with pytest.raises(PackError, match="mods/lithium.jar could not be downloaded"):
        await installer_for(tmp_path, net).install(read_pack(pack), pack, server, lambda f, m: None)


async def test_it_will_not_start_a_pack_that_does_not_fit_on_the_disk(tmp_path):
    net = FakeInternet()
    serve(net, LITHIUM)
    pack = write_pack(tmp_path, mods=[LITHIUM])
    server = tmp_path / "server"
    server.mkdir()
    with pytest.raises(PackError, match="free disk space"):
        await installer_for(tmp_path, net, free_space=lambda path: 1000).install(read_pack(pack), pack, server, lambda f, m: None)
    assert net.mod_downloads == {}


async def test_configuration_files_cannot_escape_the_server_folder_or_replace_managed_files(tmp_path):
    net = FakeInternet()
    server = tmp_path / "server"
    server.mkdir()
    for evil in ("../../evil.txt", ".manager/pack.json", "eula.txt"):
        pack = write_pack(tmp_path, raw_entries={f"overrides/{evil}": b"gotcha"})
        with pytest.raises(PackError):
            await installer_for(tmp_path, net).install(read_pack(pack), pack, server, lambda f, m: None)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()


async def test_a_link_inside_a_pack_is_ignored(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("modrinth.index.json", mrpack_bytes_index())
        link = zipfile.ZipInfo("overrides/config/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
        zf.writestr("overrides/config/real.toml", "ok")
    pack = tmp_path / "linked.mrpack"
    pack.write_bytes(buf.getvalue())
    server = tmp_path / "server"
    server.mkdir()
    await installer_for(tmp_path, FakeInternet()).install(read_pack(pack), pack, server, lambda f, m: None)
    assert (server / "config" / "real.toml").exists() and not (server / "config" / "link").exists()


def mrpack_bytes_index() -> str:
    import json

    return json.dumps({
        "formatVersion": 1, "game": "minecraft", "versionId": "1", "name": "Linked", "files": [],
        "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.9"},
    })
