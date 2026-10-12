"""Server software: what Mojang, PaperMC and Fabric offer, how the right build is chosen, and how it is downloaded and checked."""

from __future__ import annotations

import hashlib

import httpx
import pytest
from jvmfakes import FakeFabricApi, FakeInternet, FakeJava, server_jar
from jvmworld import JavaWorld

from pumpkin_manager.jvm.loaders import LoaderInstaller
from pumpkin_manager.jvm.modrinth import ModrinthClient
from pumpkin_manager.jvm.mojang import MojangMeta
from pumpkin_manager.jvm.net import BadDownloadError
from pumpkin_manager.jvm.software import Build, SoftwareCatalog, SoftwareError, SoftwareInstaller, version_key
from pumpkin_manager.models import software_label, software_tag


def catalog_for(world: JavaWorld, cache_seconds: float = 300.0) -> SoftwareCatalog:
    client = world.net.client()
    return SoftwareCatalog(client, MojangMeta(client, retry_delay=0), retry_delay=0, cache_seconds=cache_seconds)


def installer_for(world: JavaWorld) -> SoftwareInstaller:
    client = world.net.client()
    return SoftwareInstaller(client, LoaderInstaller(client, FakeJava(), retry_delay=0), ModrinthClient(client, retry_delay=0), retry_delay=0)


def ids(versions: list[dict]) -> list[str]:
    return [v["id"] for v in versions]


# ----------------------------------------------------------------------------------------------------- naming
def test_a_server_software_is_named_in_a_few_words():
    assert software_label({"kind": "paper", "minecraft": "1.21.8", "build": "60"}) == "Paper 1.21.8 (build 60)"
    assert software_label({"kind": "fabric", "minecraft": "1.21.8", "build": "0.19.5"}) == "Fabric 1.21.8 (loader 0.19.5)"
    assert software_label({"kind": "vanilla", "minecraft": "1.21.8", "build": ""}) == "Vanilla 1.21.8"
    assert software_label({"kind": "mystery", "minecraft": "1.0"}) == "Mystery 1.0"
    assert software_tag({"kind": "paper", "minecraft": "1.21.8", "build": "60"}) == "1.21.8 (build 60)"
    assert software_tag({"kind": "vanilla", "minecraft": "1.21.8"}) == "1.21.8"


def test_minecraft_versions_sort_by_their_numbers_not_their_letters():
    ordered = sorted(["1.21.10", "1.21", "26.3", "1.9", "1.21.1", "1.8.8"], key=version_key)
    assert ordered == ["1.8.8", "1.9", "1.21", "1.21.1", "1.21.10", "26.3"]


# ------------------------------------------------------------------------------------------------- what exists
async def test_each_software_lists_the_minecraft_versions_it_runs_newest_first():
    catalog = catalog_for(JavaWorld())
    assert ids(await catalog.versions("vanilla")) == ["1.21.4", "1.21.1", "1.20.4", "1.20.1", "1.16.5"]
    assert ids(await catalog.versions("paper")) == ["1.21.4", "1.21.1", "1.20.4", "1.16.5"], "release candidates are not versions to run"
    assert ids(await catalog.versions("fabric")) == ["1.21.4", "1.21.1", "1.20.4", "1.20.1"]


async def test_snapshots_are_only_listed_when_asked_for_and_only_the_ones_newer_than_the_newest_release():
    catalog = catalog_for(JavaWorld())
    vanilla = await catalog.versions("vanilla", snapshots=True)
    assert ids(vanilla)[0] == "1.21.5-snapshot-1" and vanilla[0]["type"] == "snapshot" and vanilla[1]["type"] == "release"
    assert ids(await catalog.versions("fabric", snapshots=True))[0] == "1.21.5-snapshot-1"
    assert "1.21.5-snapshot-1" not in ids(await catalog.versions("paper", snapshots=True)), "PaperMC builds releases only"


async def test_paper_prefers_stable_builds_and_says_what_it_knows_about_the_version():
    world = JavaWorld()
    options = await catalog_for(world).options("paper", "1.21.4")
    assert [b.build for b in options.builds] == ["12", "11", "10"], "newest first"
    assert options.default().build == "11", "build 12 is experimental, so the newest stable one is the default"
    assert options.java_minimum == 21 and options.support == {"status": "SUPPORTED"} and "-XX:+UseG1GC" in options.flags

    shown = options.public()
    assert [b["build"] for b in shown["builds"]] == ["11", "10"] and shown["default"] == "11" and shown["stable_only"] is True
    assert shown["has_experimental"] is True
    assert [b["build"] for b in options.public(experimental=True)["builds"]] == ["12", "11", "10"]
    assert (await catalog_for(world).options("paper", "1.16.5")).support == {"status": "UNSUPPORTED", "end": "2025-11-14"}


async def test_a_version_without_a_stable_build_still_has_a_default_and_is_not_the_one_to_start_with():
    world = JavaWorld()
    world.paper.add_version("1.21.5")
    world.paper.add_build("1.21.5", 1, "ALPHA")
    world.paper.add_build("1.21.5", 2, "BETA")
    catalog = catalog_for(world)
    options = await catalog.options("paper", "1.21.5")
    assert options.default().build == "2" and not options.default().stable and options.public()["stable_only"] is False
    assert ids(await catalog.versions("paper"))[0] == "1.21.5"
    assert await catalog.recommended_minecraft("paper") == "1.21.4", "a version nobody can run safely yet is not suggested"
    assert await catalog.newer_minecraft("paper", "1.21.1") is not None and (await catalog.newer_minecraft("paper", "1.21.1"))["id"] == "1.21.4"
    assert await catalog.newest_build("paper", "1.21.5") is None


async def test_fabric_offers_its_loaders_and_prefers_a_stable_one():
    options = await catalog_for(JavaWorld()).options("fabric", "1.21.1")
    assert [b.build for b in options.builds] == ["0.17.0-beta.1", "0.16.10", "0.16.9"]
    assert options.default().build == "0.16.10" and [b["build"] for b in options.public()["builds"]] == ["0.16.10", "0.16.9"]


async def test_vanilla_has_exactly_one_build_per_version_with_the_checksum_mojang_published():
    options = await catalog_for(JavaWorld()).options("vanilla", "1.20.4")
    (build,) = options.builds
    assert build.build == "" and build.sha1 == hashlib.sha1(server_jar("1.20.4")).hexdigest() and build.size == len(server_jar("1.20.4"))
    assert build.url.startswith("https://piston-data.mojang.com/") and options.java_minimum == 17 and build.label == "Vanilla 1.20.4"


@pytest.mark.parametrize("kind,version,message", [
    ("paper", "1.7.10", "Paper has no builds for Minecraft 1.7.10"),
    ("fabric", "1.13.2", "Fabric does not support Minecraft 1.13.2"),
    ("vanilla", "9.9.9", "Mojang does not list Minecraft 9.9.9"),
    ("vanilla", "../etc", "not a valid Minecraft version"),
    ("paper", "1.21 4", "not a valid Minecraft version"),
])
async def test_a_version_that_does_not_exist_is_explained(kind, version, message):
    with pytest.raises(SoftwareError, match=message):
        await catalog_for(JavaWorld()).options(kind, version)


async def test_a_version_mojang_never_published_a_server_for_is_explained():
    world = JavaWorld()
    world.net.minecraft({"1.21.4": 21, "1.2.3": None}, no_server=("1.2.3",))
    with pytest.raises(SoftwareError, match="published no server for Minecraft 1.2.3"):
        await catalog_for(world).options("vanilla", "1.2.3")


async def test_an_unknown_kind_of_software_is_refused():
    with pytest.raises(SoftwareError, match="Unknown server software 'forge'"):
        await catalog_for(JavaWorld()).versions("forge")


async def test_an_unreachable_service_is_reported_as_such_and_answers_are_remembered_for_a_while():
    world = JavaWorld()
    catalog = catalog_for(world)
    await catalog.versions("paper")
    calls = len(world.paper.api_calls)
    await catalog.versions("paper")
    assert len(world.paper.api_calls) == calls, "asked once, remembered"
    catalog.invalidate()
    world.net.offline = True
    with pytest.raises(SoftwareError, match="Could not reach") as caught:
        await catalog.versions("paper")
    assert caught.value.status == 502


# ------------------------------------------------------------------------------------------------- choosing
async def test_the_build_to_install_is_the_one_asked_for_or_the_newest_stable_one():
    catalog = catalog_for(JavaWorld())
    build, options = await catalog.resolve("paper", "1.21.4")
    assert (build.build, build.minecraft, build.stable) == ("11", "1.21.4", True) and options.java_minimum == 21
    assert (await catalog.resolve("paper", "1.21.4", "10"))[0].build == "10"
    assert (await catalog.resolve("paper", "1.21.4", "12"))[0].channel == "alpha", "an experimental build can be chosen on purpose"
    assert (await catalog.resolve("paper"))[0].minecraft == "1.21.4", "without a version: the newest one with a stable build"
    assert (await catalog.resolve("vanilla", "1.20.1", "ignored"))[0].label == "Vanilla 1.20.1"
    assert (await catalog.resolve("fabric", "1.21.1", "0.16.9"))[0].label == "Fabric 1.21.1 (loader 0.16.9)"
    with pytest.raises(SoftwareError, match="has no build 99 for Minecraft 1.21.4"):
        await catalog.resolve("paper", "1.21.4", "99")
    with pytest.raises(SoftwareError, match="no loader version 9.9 for Minecraft 1.21.1"):
        await catalog.resolve("fabric", "1.21.1", "9.9")


async def test_update_checks_look_at_stable_builds_of_the_same_version_and_at_newer_minecraft_versions():
    world = JavaWorld()
    catalog = catalog_for(world)
    assert (await catalog.newest_build("paper", "1.21.4")).build == "11"
    assert (await catalog.newest_build("fabric", "1.21.4")).build == "0.16.10"
    assert await catalog.newest_build("vanilla", "1.21.4") is None, "Vanilla has no builds: a newer version is its update"
    assert (await catalog.newer_minecraft("vanilla", "1.20.4"))["id"] == "1.21.4"
    assert await catalog.newer_minecraft("vanilla", "1.21.4") is None
    assert (await catalog.newer_minecraft("vanilla", "1.21.4", snapshots=True))["id"] == "1.21.5-snapshot-1"
    assert await catalog.newer_minecraft("paper", "1.21.4") is None


async def test_what_is_newer_is_decided_by_mojangs_release_dates():
    catalog = catalog_for(JavaWorld())
    assert await catalog.is_newer("1.21.4", "1.20.4") and not await catalog.is_newer("1.20.4", "1.21.4") and not await catalog.is_newer("1.21.4", "1.21.4")
    assert await catalog.is_newer("26.9", "1.21.4"), "a version Mojang does not list is compared by its numbers"


# ----------------------------------------------------------------------------------------------- installing
async def test_a_vanilla_jar_is_downloaded_and_checked(tmp_path):
    world = JavaWorld()
    build, _ = await catalog_for(world).resolve("vanilla", "1.21.1")
    launch = await installer_for(world).fetch(build, tmp_path, tmp_path / "java", lambda text: None)
    assert launch == {"jar": "server.jar"} and (tmp_path / "server.jar").read_bytes() == server_jar("1.21.1")


async def test_a_paper_jar_is_downloaded_and_checked(tmp_path):
    world = JavaWorld()
    build, _ = await catalog_for(world).resolve("paper", "1.21.4")
    progress: list[tuple[int, int]] = []
    launch = await installer_for(world).fetch(build, tmp_path, tmp_path / "java", lambda text: None, lambda done, total: progress.append((done, total)))
    assert launch == {"jar": "paper.jar"} and hashlib.sha256((tmp_path / "paper.jar").read_bytes()).hexdigest() == build.sha256
    assert progress and progress[-1][0] == build.size


async def test_a_fabric_launcher_is_fetched_through_the_loader_installer(tmp_path):
    world = JavaWorld()
    build, _ = await catalog_for(world).resolve("fabric", "1.21.4")
    launch = await installer_for(world).fetch(build, tmp_path, tmp_path / "java", lambda text: None)
    assert launch == {"jar": "fabric-server-launch.jar"} and (tmp_path / "fabric-server-launch.jar").read_bytes()[:2] == b"PK"
    assert world.fabric.downloads == 1


async def test_a_download_with_the_wrong_checksum_is_thrown_away(tmp_path):
    world = JavaWorld()
    build, _ = await catalog_for(world).resolve("paper", "1.21.4")
    world.paper.blobs[build.sha256] = b"PK\x03\x04 something else entirely"
    with pytest.raises(BadDownloadError, match="SHA256"):
        await installer_for(world).fetch(build, tmp_path, tmp_path / "java", lambda text: None)
    assert list(tmp_path.iterdir()) == []

    vanilla, _ = await catalog_for(world).resolve("vanilla", "1.21.4")
    world.net.routes[("piston-data.mojang.com", vanilla.url.split("piston-data.mojang.com", 1)[1])] = lambda request: httpx.Response(200, content=b"PK\x03\x04 nope")
    with pytest.raises(BadDownloadError, match="SHA1"):
        await installer_for(world).fetch(vanilla, tmp_path, tmp_path / "java", lambda text: None)
    assert list(tmp_path.iterdir()) == []


async def test_a_file_that_is_not_a_jar_is_refused_even_with_a_matching_checksum(tmp_path):
    world = JavaWorld()
    junk = b"<html>an error page</html>"
    build = Build("paper", "1.21.4", "99", url="https://fill-data.papermc.io/v1/objects/x/paper.jar", sha256=hashlib.sha256(junk).hexdigest())
    world.net.routes[("fill-data.papermc.io", "/v1/objects/x/paper.jar")] = lambda request: httpx.Response(200, content=junk)
    with pytest.raises(BadDownloadError, match="not a jar"):
        await installer_for(world).fetch(build, tmp_path, tmp_path / "java", lambda text: None)
    assert list(tmp_path.iterdir()) == []


async def test_a_download_address_on_another_host_is_refused(tmp_path):
    world = JavaWorld()
    jar = server_jar("x")
    evil = Build("paper", "1.21.4", "98", url="https://evil.example/paper.jar", sha256=hashlib.sha256(jar).hexdigest())
    with pytest.raises(BadDownloadError, match="not one of the allowed hosts"):
        await installer_for(world).fetch(evil, tmp_path, tmp_path / "java", lambda text: None)
    plain = Build("vanilla", "1.21.4", "", url="http://piston-data.mojang.com/server.jar", sha1=hashlib.sha1(jar).hexdigest())
    with pytest.raises(BadDownloadError, match="only https"):
        await installer_for(world).fetch(plain, tmp_path, tmp_path / "java", lambda text: None)


# ------------------------------------------------------------------------------------------------ Fabric API
async def test_fabric_api_is_the_newest_release_for_the_minecraft_version(tmp_path):
    world = JavaWorld()
    world.fabric_api.add_version("0.107.0-beta+1.21.1", "1.21.1", version_type="beta")
    installer = installer_for(world)
    api = await installer.fabric_api("1.21.1")
    assert api.number == "0.106.0+1.21.1" and api.release_type == "release", "a newer beta does not win over a release"
    record = await installer.install_fabric_api(api, tmp_path)
    assert (tmp_path / "mods" / "fabric-api-0.106.0+1.21.1.jar").read_bytes() == world.fabric_api.files[api.version_id]
    assert record["file"] == "mods/fabric-api-0.106.0+1.21.1.jar" and record["sha1"] == hashlib.sha1(world.fabric_api.files[api.version_id]).hexdigest()
    assert await installer.fabric_api("1.20.4") is None, "no Fabric API for that version yet"


async def test_when_there_is_only_a_beta_fabric_api_that_is_used():
    world = JavaWorld()
    world.fabric_api.add_version("0.200.0-beta+1.22", "1.22", version_type="beta")
    assert (await installer_for(world).fabric_api("1.22")).release_type == "beta"


async def test_a_fabric_api_file_with_a_hostile_name_is_not_used(tmp_path):
    world = JavaWorld()
    for version in world.fabric_api.versions:
        version["files"][0]["filename"] = "../../evil.jar"
    assert await installer_for(world).fabric_api("1.21.1") is None


async def test_a_fabric_api_download_is_checked(tmp_path):
    world = JavaWorld()
    api = await installer_for(world).fabric_api("1.21.1")
    world.fabric_api.files[api.version_id] = b"PK\x03\x04 tampered"
    world.net.routes[("cdn.modrinth.com", api.file["url"].split("cdn.modrinth.com", 1)[1])] = lambda request: httpx.Response(200, content=b"PK\x03\x04 tampered")
    with pytest.raises(BadDownloadError):
        await installer_for(world).install_fabric_api(api, tmp_path)
    assert not list((tmp_path / "mods").iterdir())


def test_the_fake_world_serves_the_modrinth_filters_like_the_real_one():
    net = FakeInternet()
    api = FakeFabricApi(net)
    api.add_version("1", "1.21.1")
    assert api.versions[0]["game_versions"] == ["1.21.1"]
