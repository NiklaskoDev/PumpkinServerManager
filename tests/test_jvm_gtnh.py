"""GT New Horizons from its own site: reading the version history, choosing the build and Java, how the server files start, what the
zips hold and how an update treats them."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cftools import zip_at
from jvmfakes import SERVERUTILITIES_CFG, FakeGtnh, FakeInternet, gtnh_server_files

from pumpkin_manager.jvm import archive, gtnh
from pumpkin_manager.jvm.archive import ServerPack
from pumpkin_manager.jvm.gtnh import GtnhCatalog, Release, parse_history
from pumpkin_manager.jvm.loaders import launch_arguments
from pumpkin_manager.jvm.mrpack import PackError
from pumpkin_manager.jvm.net import BadDownloadError, DownloadError, get_text

FIXTURE = Path(__file__).parent / "fixtures" / "gtnh-version-history.html"
SERVERS = "https://downloads.gtnewhorizons.com/ServerPacks/"


def release(version: str = "2.8.4", **options) -> Release:
    values = {"kind": "release", "published": "2025-12-23", "modern": f"{SERVERS}GT_New_Horizons_{version}_Server_Java_17-25.zip", "java_max": 25,
              "legacy": f"{SERVERS}GT_New_Horizons_{version}_Server_Java_8.zip", **options}
    return Release(version=version, **values)


def world(*versions: str) -> tuple[FakeInternet, FakeGtnh, GtnhCatalog]:
    net = FakeInternet()
    site = FakeGtnh(net)
    for version in versions:
        site.add_release(version)
    return net, site, GtnhCatalog(net.client(), retry_delay=0)


# ------------------------------------------------------------------------------------------------ the version history
def test_the_real_version_history_page_is_read():
    """The fixture is a cut-down copy of the page as it is published (2026-10-13): six of its folding blocks, one of them an April Fools edition."""
    releases = parse_history(FIXTURE.read_text(encoding="utf-8"))
    assert [r.version for r in releases] == ["2.9.0-RC-2", "2.9.0-beta-2", "2.8.4", "2.8.0-beta-4", "2.4.0"], "newest first; the April Fools edition has no version"
    rc, beta, stable, old_beta, oldest = releases
    assert (rc.kind, rc.published, rc.java_max) == ("beta", "2026-10-04", 26)
    assert rc.modern == f"{SERVERS}betas/GT_New_Horizons_2.9.0-RC-2_Server_Java_17-26.zip"
    assert rc.legacy == f"{SERVERS}betas/GT_New_Horizons_2.9.0-RC-2_Server_Java_8.zip"
    assert (beta.kind, beta.java_max) == ("beta", 25)
    assert (stable.kind, stable.published, stable.java_max) == ("release", "2025-12-23", 25)
    assert stable.modern == f"{SERVERS}GT_New_Horizons_2.8.4_Server_Java_17-25.zip" and stable.legacy == f"{SERVERS}GT_New_Horizons_2.8.4_Server_Java_8.zip"
    assert (old_beta.kind, old_beta.java_max, oldest.kind, oldest.java_max, oldest.published) == ("beta", 21, "release", 20, "2023-09-03")


def test_a_version_is_shown_to_the_dashboard_like_the_versions_of_other_packs():
    assert release().public() == {
        "id": "2.8.4", "number": "2.8.4", "type": "release", "published": "2025-12-23", "minecraft": ["1.7.10"], "loaders": ["forge"],
        "server_pack": True, "size": 0, "builds": [{"variant": "modern", "name": "Java 17 and newer", "runs_on": 25}, {"variant": "legacy", "name": "Java 8", "runs_on": 8}],
    }
    only_old = release("2.1.0", modern=None, java_max=None, published="")
    assert only_old.public()["builds"] == [{"variant": "legacy", "name": "Java 8", "runs_on": 8}] and only_old.public()["published"] is None
    assert release(java_max=16).public()["builds"][0]["runs_on"] is None, "a build made for a Java that cannot be installed says so"


def test_only_the_packs_own_download_addresses_count():
    page = "".join(f'<a href="{href}">x</a>' for href in (
        "https://evil.example/ServerPacks/GT_New_Horizons_9.9.1_Server_Java_8.zip",  # another host
        "http://downloads.gtnewhorizons.com/ServerPacks/GT_New_Horizons_9.9.2_Server_Java_8.zip",  # not https
        "https://downloads.gtnewhorizons.com/ServerPacks/GT_New_Horizons_9.9.3_Server_Java_8.zip.exe",  # not a zip
        "https://downloads.gtnewhorizons.com/ServerPacks/GT_New_Horizons_../../x_Server_Java_8.zip",  # a way out
        "https://downloads.gtnewhorizons.com/ServerPacks/GT_New_Horizons_9.9.4_Client_Java_8.zip",  # not a server
        "https://downloads.gtnewhorizons.com/Multi_mc_downloads/GT_New_Horizons_9.9.5_Java_8.zip",  # not a server
        "https://downloads.gtnewhorizons.com/ServerPacks/GT_New_Horizons_9.9.6_Server_Java_8.zip",  # the only one
    ))
    assert [(r.version, r.legacy) for r in parse_history(page)] == [("9.9.6", f"{SERVERS}GT_New_Horizons_9.9.6_Server_Java_8.zip")]


def test_a_page_built_differently_still_gives_the_versions_without_dates():
    page = (
        f'<ul><li><a href="{SERVERS}betas/GT_New_Horizons_3.0.0-beta-1_Server_Java_17-27.zip">a</a></li>'
        f'<li><a href="{SERVERS}GT_New_Horizons_2.9.0_Server_Java_17-26.zip">b</a><a href="{SERVERS}GT_New_Horizons_2.9.0_Server_Java_8.zip">c</a></li></ul>'
    )
    found = parse_history(page)
    assert [(r.version, r.kind, r.published, r.java_max, r.legacy is not None) for r in found] == [
        ("3.0.0-beta-1", "beta", "", 27, False), ("2.9.0", "release", "", 26, True),
    ]
    assert parse_history("<html>nothing to see</html>") == []


def test_a_block_with_two_versions_gets_no_date_it_cannot_vouch_for():
    block = (
        f'<details><summary><span>x</span><span>2025/01/02</span></summary><a href="{SERVERS}GT_New_Horizons_1.0.0_Server_Java_8.zip">a</a>'
        f'<a href="{SERVERS}GT_New_Horizons_1.0.1_Server_Java_8.zip">b</a></details>'
    )
    assert [(r.version, r.published) for r in parse_history(block)] == [("1.0.0", ""), ("1.0.1", "")]


# ------------------------------------------------------------------------------------------------------ the catalog
async def test_the_newest_stable_version_is_the_default_and_any_listed_one_can_be_named():
    net, site, catalog = world()
    for version, beta in (("2.9.0-RC-2", True), ("2.8.4", False), ("2.8.3", False)):
        site.add_release(version, beta=beta)
    assert (await catalog.release()).version == "2.8.4", "a release candidate is not what a new server gets"
    assert (await catalog.release("2.9.0-RC-2")).kind == "beta" and (await catalog.release("2.8.3")).version == "2.8.3"
    with pytest.raises(PackError, match="does not exist"):
        await catalog.release("1.2.3")
    project = await catalog.project()
    assert project["id"] == "gtnh" and project["title"] == "GT New Horizons" and project["source"] == "gtnh"
    assert [v["id"] for v in project["versions"]] == ["2.9.0-RC-2", "2.8.4", "2.8.3"]


async def test_only_beta_versions_still_give_a_default():
    net, site, catalog = world("2.0.0")
    site.releases[0]["beta"] = True
    assert (await catalog.release()).version == "2.0.0"


async def test_the_page_is_read_once_an_hour_unless_somebody_asks_again():
    net, site, _ = world("2.8.4")
    now = [1000.0]
    catalog = GtnhCatalog(net.client(), retry_delay=0, clock=lambda: now[0])
    await catalog.releases()
    await catalog.releases()
    assert net.requests.count(FakeGtnh.PAGE) == 1
    site.add_release("2.8.5", first=True)
    assert [r.version for r in await catalog.releases()] == ["2.8.4"], "still the page of a moment ago"
    now[0] += gtnh.CACHE_SECONDS + 1
    assert [r.version for r in await catalog.releases()] == ["2.8.5", "2.8.4"]
    site.add_release("2.8.6", first=True)
    catalog.invalidate()
    assert (await catalog.releases())[0].version == "2.8.6" and net.requests.count(FakeGtnh.PAGE) == 3


async def test_a_version_the_list_does_not_know_is_looked_for_on_a_fresh_page_once_the_list_is_a_minute_old():
    net, site, _ = world("2.8.4")
    now = [1000.0]
    catalog = GtnhCatalog(net.client(), retry_delay=0, clock=lambda: now[0])
    await catalog.releases()
    site.add_release("2.8.5", first=True)
    with pytest.raises(PackError, match="does not exist"):
        await catalog.release("2.8.5")  # the list is a moment old: asking the website again for every typo would be rude
    assert net.requests.count(FakeGtnh.PAGE) == 1
    now[0] += gtnh.RECHECK_SECONDS + 1
    assert (await catalog.release("2.8.5")).version == "2.8.5" and net.requests.count(FakeGtnh.PAGE) == 2
    with pytest.raises(PackError, match="does not exist"):
        await catalog.release("9.9.9")  # the page was just read
    assert net.requests.count(FakeGtnh.PAGE) == 2


async def test_an_unreachable_or_unreadable_website_is_explained():
    net, site, catalog = world("2.8.4")
    site.page_status = 503
    with pytest.raises(DownloadError, match="(?i)the GT New Horizons website answered HTTP 503"):
        await catalog.releases()
    site.page_status = 200
    site.releases.clear()
    with pytest.raises(DownloadError, match="lists no server downloads"):
        await catalog.releases()
    net.offline = True
    with pytest.raises(DownloadError, match="(?i)could not reach the GT New Horizons website"):
        await catalog.releases()


async def test_a_build_is_downloaded_only_from_the_packs_own_host(tmp_path):
    net, site, catalog = world()
    site.add_release("2.8.4")
    site.add_release("9.9.9", redirect_to="https://evil.example/server.zip")
    seen: list[tuple[int, int]] = []
    ok = tmp_path / "ok.zip"
    await catalog.download(await catalog.release("2.8.4"), "modern", ok, lambda done, total: seen.append((done, total)))
    assert zip_names(ok) >= {"mods/gregtech-5.09.jar", "lwjgl3ify-forgePatches.jar"} and seen and seen[-1][0] == ok.stat().st_size

    bad = tmp_path / "bad.zip"
    with pytest.raises(BadDownloadError, match="evil.example: it is not one of the allowed hosts"):
        await catalog.download(await catalog.release("9.9.9"), "legacy", bad)
    assert not bad.exists() and not any("evil.example" in url for url in net.requests), "the redirect was never followed"


def zip_names(path: Path) -> set[str]:
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


# ----------------------------------------------------------------------------------------------- builds and their Java
def test_the_build_for_java_17_and_newer_is_the_default():
    both, old = release(), release("2.1.0", modern=None, java_max=None)
    assert gtnh.pick_variant(both, None) == "modern" and gtnh.pick_variant(both, "") == "modern" and gtnh.pick_variant(both, "legacy") == "legacy"
    assert gtnh.pick_variant(old, None) == "legacy", "a version that only has the Java 8 build gets that"
    with pytest.raises(PackError, match="has no server files for Java 17 and newer"):
        gtnh.pick_variant(old, "modern")
    with pytest.raises(PackError, match="has no server files for Java 8"):
        gtnh.pick_variant(release(legacy=None), "legacy")
    with pytest.raises(PackError, match="Choose the server files"):
        gtnh.pick_variant(both, "java 11")


@pytest.mark.parametrize(("variant", "newest", "expected"), [
    ("legacy", 25, 8), ("modern", 25, 25), ("modern", 26, 25), ("modern", 24, 21), ("modern", 21, 21), ("modern", 20, 17), ("modern", 17, 17),
    ("modern", None, 21),
])
def test_java_is_the_newest_one_the_build_is_made_for_that_can_be_installed(variant, newest, expected):
    assert gtnh.java_for(variant, newest) == expected


def test_a_build_made_for_a_java_nobody_can_install_is_refused():
    with pytest.raises(PackError, match="cannot install"):
        gtnh.java_for("modern", 16)


# ------------------------------------------------------------------------------------------- how the files are started
def pack_of(tmp_path: Path, version: str = "2.8.4", *, modern: bool = True, changes=None) -> ServerPack:
    return ServerPack(zip_at(tmp_path / f"{version}-{modern}.zip", gtnh_server_files(version, modern=modern, changes=changes)), whole=True)


def test_the_build_for_java_17_starts_like_the_packs_own_script_and_the_one_for_java_8_too(tmp_path):
    modern = gtnh.launch_of(pack_of(tmp_path), "modern")
    assert modern == {"jar": "lwjgl3ify-forgePatches.jar", "before": ["-Dfml.readTimeout=180", "@java9args.txt"]}
    assert launch_arguments(modern) == ["-Dfml.readTimeout=180", "@java9args.txt", "-jar", "lwjgl3ify-forgePatches.jar"]
    legacy = gtnh.launch_of(pack_of(tmp_path, modern=False), "legacy")
    assert legacy["jar"] == "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar" and legacy["before"][0] == "-Dfml.readTimeout=180"
    assert "-XX:+UseStringDeduplication" in legacy["before"] and "-XX:+UseCodeCacheFlushing" in legacy["before"]
    assert launch_arguments({"jar": "x.jar"}) == ["-jar", "x.jar"] and launch_arguments({"argfile": "a.txt", "before": ["-Dx=1"]}) == ["-Dx=1", "@a.txt"]


def test_server_files_that_cannot_be_started_are_refused_before_anything_is_installed(tmp_path):
    with pytest.raises(PackError, match="lwjgl3ify-forgePatches.jar and java9args.txt"):
        gtnh.launch_of(pack_of(tmp_path, modern=False), "modern")
    with pytest.raises(PackError, match="no Forge jar"):
        gtnh.launch_of(pack_of(tmp_path, changes={"forge-1.7.10-10.13.4.1614-1.7.10-universal.jar": None}), "legacy")


def test_the_index_names_the_forge_the_files_bring(tmp_path):
    index = gtnh.build_index(release(), pack_of(tmp_path))
    assert (index.name, index.version_id, index.minecraft, index.source) == ("GT New Horizons", "2.8.4", "1.7.10", "gtnh")
    assert (index.loader.kind, index.loader.version) == ("forge", "10.13.4.1614") and index.archive_replace is gtnh.pack_wins
    other = gtnh.build_index(release(), pack_of(tmp_path, "9.9.9", changes={
        "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar": None, "forge-1.7.10-10.13.4.1700-1.7.10-universal.jar": b"newer forge"}))
    assert other.loader.version == "10.13.4.1700"
    assert gtnh.build_index(release(), pack_of(tmp_path, "9.9.8", changes={"forge-1.7.10-10.13.4.1614-1.7.10-universal.jar": None})).loader.version == gtnh.FORGE


def test_what_the_server_remembers_about_its_pack():
    assert gtnh.pack_info(release("2.9.0-RC-2", kind="beta", java_max=26), "legacy") == {
        "source": "gtnh", "project": "gtnh", "slug": "gt-new-horizons", "title": "GT New Horizons", "icon": None, "version_id": "2.9.0-RC-2",
        "version_number": "2.9.0-RC-2", "published": "2025-12-23", "version_type": "beta", "server_pack": True, "variant": "legacy", "java_max": 26,
    }


# ----------------------------------------------------------------------------------------------- what the zips hold
def test_a_whole_server_zip_keeps_its_loader_and_leaves_out_only_what_the_manager_looks_after(tmp_path):
    path = zip_at(tmp_path / "server.zip", gtnh_server_files("2.8.4"))
    whole, plain = ServerPack(path, whole=True), ServerPack(path)
    names = {rel for rel, _ in whole.entries}
    assert names >= {
        "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar", "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar", "minecraft_server.1.7.10.jar",
        "lwjgl3ify-forgePatches.jar", "java9args.txt", "server.properties", "server-icon.png", "serverutilities/serverutilities.cfg",
        "mods/gregtech-5.09.jar", "config/hodgepodge.cfg",
    }
    assert not names & {"eula.txt", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json", "usercache.json",
                        "startserver-java9.sh", "startserver-java9.bat"}
    assert whole.skipped == {"the manager looks after it": 6, "a start script: the manager starts the server itself": 2}
    assert whole.mod_count == 4 and whole.usable
    plain_names = {rel for rel, _ in plain.entries}
    assert "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar" not in plain_names and not any(n.startswith("libraries/") for n in plain_names), \
        "a pack's own server files that are not whole still get their loader from the manager"


def test_a_whole_server_zip_is_unpacked_as_it_is(tmp_path):
    pack = pack_of(tmp_path)
    server = tmp_path / "server"
    mods, others = archive.install(pack, server, lambda fraction, message: None)
    assert set(mods) == {"mods/gregtech-5.09.jar", "mods/NewHorizonsCoreMod.jar", "mods/hodgepodge.jar", "mods/gtnhlib.jar"}
    assert all(value.startswith("sha256:") for value in mods.values()) and "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar" in others
    assert (server / "java9args.txt").is_file() and (server / "lwjgl3ify-forgePatches.jar").read_bytes() == b"patches 2.8.4"
    assert not (server / "eula.txt").exists() and not (server / "startserver-java9.sh").exists()


# ------------------------------------------------------------------------------------------- what an update replaces
def test_an_update_replaces_what_the_pack_wants_replaced_and_keeps_what_is_somebodys(tmp_path):
    old = pack_of(tmp_path, "2.8.3")
    new = pack_of(tmp_path, "2.8.4", changes={
        "config/JourneyMapServer/world.cfg": b"uuid=the pack changed it\n", "serverutilities/serverutilities.cfg": SERVERUTILITIES_CFG + b"# new\n",
        "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar": b"asm, newer",
    })
    server = tmp_path / "server"
    mods, others = archive.install(old, server, lambda fraction, message: None)
    lock = {"files": mods, "overrides": others}
    (server / "mods" / "mine.jar").write_bytes(b"added by hand")  # not the pack's
    (server / "mods" / "hodgepodge.jar").write_bytes(b"replaced by hand")  # somebody replaced a mod
    (server / "config" / "hodgepodge.cfg").write_text("I:fixes=0\n")  # a mod rewrote it, or a person changed it: the pack is not changing it
    (server / "config" / "JourneyMapServer" / "world.cfg").write_text("uuid=the maps of the players\n")
    (server / "serverutilities" / "serverutilities.cfg").write_bytes(SERVERUTILITIES_CFG.replace(b"0.5", b"3"))  # somebody's setting
    (server / "lwjgl3ify-forgePatches.jar").write_bytes(b"hacked")
    (server / "server.properties").write_text("motd=mine\n")

    mods, others, report = archive.update(new, server, lock, frozenset({"server.properties"}), lambda f, m: None, gtnh.pack_wins)

    assert (server / "mods" / "gregtech-5.09.jar").read_bytes() == b"gregtech 2.8.4" * 30 and "mods/gregtech-5.09.jar" in report["updated"]
    assert (server / "mods" / "mine.jar").read_bytes() == b"added by hand" and (server / "mods" / "hodgepodge.jar").read_bytes() == b"replaced by hand"
    assert report["kept_mods"] == ["mods/hodgepodge.jar"]
    assert (server / "config" / "hodgepodge.cfg").read_text() == "I:fixes=1\n", "the pack's configuration wins, as its update guide says"
    assert (server / "lwjgl3ify-forgePatches.jar").read_bytes() == b"patches 2.8.4" and (server / "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar").read_bytes() == b"asm, newer"
    assert {"config/hodgepodge.cfg", "lwjgl3ify-forgePatches.jar"} <= set(report["replaced"]) and "config/GregTech/GregTech.cfg" in report["updated"]
    assert (server / "config" / "JourneyMapServer" / "world.cfg").read_text() == "uuid=the maps of the players\n", "what keeps the players' maps stays"
    assert (server / "config" / "JourneyMapServer" / "world.cfg.pack-new").read_text() == "uuid=the pack changed it\n"
    assert b"S:backup_timer=3" in (server / "serverutilities" / "serverutilities.cfg").read_bytes()
    assert (server / "serverutilities" / "serverutilities.cfg.pack-new").read_bytes().endswith(b"# new\n")
    assert sorted(report["kept_configs"]) == ["config/JourneyMapServer/world.cfg", "serverutilities/serverutilities.cfg"]
    assert (server / "server.properties").read_text() == "motd=mine\n", "server.properties is never replaced"
    assert not (server / "changelog to 2.8.3.md").exists() and (server / "changelog to 2.8.4.md").is_file(), "last version's changelog goes, the new one comes"
    assert "mods/gregtech-5.09.jar" in mods and "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar" in others


@pytest.mark.parametrize(("path", "wins"), [
    ("config/GregTech/GregTech.cfg", True), ("config/hodgepodge.cfg", True), ("config/JourneyMapServer/world.cfg", False),
    ("libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar", True), ("lwjgl3ify-forgePatches.jar", True),
    ("forge-1.7.10-10.13.4.1614-1.7.10-universal.jar", True), ("java9args.txt", True), ("serverutilities/serverutilities.cfg", False),
    ("serverutilities/server/ranks.txt", False), ("journeymap/config/5.2/journeymap.core.config", False), ("server.properties", False),
    ("server-icon.png", False), ("mods/gregtech.jar", False), ("World/level.dat", False),
])
def test_what_an_update_replaces_even_when_it_was_changed(path, wins):
    assert gtnh.pack_wins(path) is wins


# --------------------------------------------------------------------------- the pack's own backups, and updating in steps
def lock_for(directory: Path, digest: str) -> Path:
    path = directory / ".manager" / "pack.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"overrides": {gtnh.SERVERUTILITIES_CONFIG: digest, "config/a.cfg": "x"}}), encoding="utf-8")
    return path


def test_the_backups_of_serverutilities_are_switched_off_and_nothing_else_changes(tmp_path):
    import hashlib

    config = tmp_path / "serverutilities" / "serverutilities.cfg"
    config.parent.mkdir(parents=True)
    config.write_bytes(SERVERUTILITIES_CFG)
    lock = lock_for(tmp_path, hashlib.sha256(SERVERUTILITIES_CFG).hexdigest())

    assert gtnh.switch_off_own_backups(tmp_path) is True
    text = config.read_bytes()
    assert b"B:enable_backups=false" in text and b"B:enabled=true" in text and b"B:need_online_players=true" in text
    assert text == SERVERUTILITIES_CFG.replace(b"B:enable_backups=true", b"B:enable_backups=false"), "only that one line"
    recorded = json.loads(lock.read_text(encoding="utf-8"))["overrides"]
    assert recorded[gtnh.SERVERUTILITIES_CONFIG] == hashlib.sha256(text).hexdigest() and recorded["config/a.cfg"] == "x", "the lock knows the new content"
    assert gtnh.switch_off_own_backups(tmp_path) is False, "already off: nothing to do"


def test_windows_line_endings_survive_and_files_without_the_setting_are_left_alone(tmp_path):
    config = tmp_path / "serverutilities" / "serverutilities.cfg"
    config.parent.mkdir(parents=True)
    config.write_bytes(SERVERUTILITIES_CFG.replace(b"\n", b"\r\n"))
    assert gtnh.switch_off_own_backups(tmp_path) is True
    assert config.read_bytes() == SERVERUTILITIES_CFG.replace(b"\n", b"\r\n").replace(b"enable_backups=true", b"enable_backups=false")

    for content in (b"afk {\n    B:enabled=true\n}\n", b"backups {\n    B:other=true\n}\n", b"\xff\xfe not text"):
        config.write_bytes(content)
        assert gtnh.switch_off_own_backups(tmp_path) is False and config.read_bytes() == content
    config.unlink()
    assert gtnh.switch_off_own_backups(tmp_path) is False


@pytest.mark.parametrize(("old", "new", "warns"), [
    ("2.7.4", "2.8.0", False), ("2.8.0", "2.8.4", False), ("2.8.4", "2.8.4", False), ("2.7.4", "2.8.4-RC1", False),
    ("2.6.1", "2.8.0", True), ("2.5.0", "2.8.4", True), ("2.8.4", "3.0.0", True), ("2.8.4", "2.7.4", True), ("", "2.8.0", False), ("2.8.4", "weird", False),
])
def test_updating_over_more_than_one_major_version_or_backwards_comes_with_a_warning(old, new, warns):
    warning = gtnh.jump_warning(old, new)
    assert (warning is not None) is warns
    if warns:
        assert "backup" in warning


# ------------------------------------------------------------------------------------------------------ the text helper
async def test_a_page_is_read_with_a_ceiling_and_retries():
    net = FakeInternet()
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="hello")

    net.route("https://example.org/page", flaky)
    net.route("https://example.org/big", lambda request: httpx.Response(200, content=b"x" * 5000))
    net.route("https://example.org/teapot", lambda request: httpx.Response(418))
    client = net.client()
    assert await get_text(client, "https://example.org/page", what="the page", delay=0) == "hello" and len(calls) == 3
    with pytest.raises(BadDownloadError, match="much larger than expected"):
        await get_text(client, "https://example.org/big", what="the page", max_bytes=1000)
    with pytest.raises(DownloadError, match="the page does not know that"):
        await get_text(client, "https://example.org/missing", what="the page")
    with pytest.raises(DownloadError, match="answered HTTP 418"):
        await get_text(client, "https://example.org/teapot", what="the page")
    with pytest.raises(BadDownloadError, match="only https"):
        await get_text(client, "http://example.org/page")
    with pytest.raises(BadDownloadError, match="not one of the allowed hosts"):
        await get_text(client, "https://example.org/page", allowed_hosts=frozenset({"other.example"}))
