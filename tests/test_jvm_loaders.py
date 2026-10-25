"""Mod loaders: what gets downloaded, what the installers are asked to do, and how the launch command is found."""

from __future__ import annotations

import httpx
import pytest
from jvmfakes import FakeInternet, FakeJava

from pumpkin_manager.jvm.loaders import LoaderError, LoaderInstaller, LoaderSpec, find_launch, launch_arguments
from pumpkin_manager.jvm.net import BadDownloadError, DownloadError

JAVA = "C:/java/bin/java"


def installer_for(net: FakeInternet, fake: FakeJava | None = None) -> tuple[LoaderInstaller, FakeJava, list[str]]:
    fake = fake or FakeJava()
    said: list[str] = []
    return LoaderInstaller(net.client(), fake, retry_delay=0), fake, said


def test_a_loader_spec_only_accepts_what_can_safely_become_part_of_a_download_address():
    assert LoaderSpec("neoforge", "26.3.0.71-beta", "26.3").label == "NeoForge 26.3.0.71-beta"
    for kind, version in (("magic", "1.0"), ("forge", "../../etc"), ("forge", "1/2"), ("fabric", ""), ("fabric", "1..2"), ("fabric", "a b")):
        with pytest.raises(LoaderError):
            LoaderSpec(kind, version, "1.20.1")
    with pytest.raises(LoaderError):
        LoaderSpec("fabric", "0.16.9", "1.20.1/../x")


async def test_fabric_needs_only_the_launcher_jar(tmp_path):
    net = FakeInternet()
    net.fabric("1.21.1", "0.16.9", "1.0.1")
    installer, fake, said = installer_for(net)
    launch = await installer.install(LoaderSpec("fabric", "0.16.9", "1.21.1"), tmp_path, JAVA, said.append)
    assert launch == {"jar": "fabric-server-launch.jar"} and (tmp_path / "fabric-server-launch.jar").is_file()
    assert fake.calls == [], "Fabric's launcher downloads the rest when the server first starts: no installer is run"
    assert launch_arguments(launch) == ["-jar", "fabric-server-launch.jar"]


async def test_fabric_refuses_an_answer_that_is_not_a_jar(tmp_path):
    net = FakeInternet()
    net.fabric("1.21.1", "0.16.9", "1.0.1")
    net.blob("https://meta.fabricmc.net/v2/versions/loader/1.21.1/0.16.9/1.0.1/server/jar", b"<html>sorry</html>")
    installer, _, said = installer_for(net)
    with pytest.raises(BadDownloadError, match="did not send a launcher"):
        await installer.install(LoaderSpec("fabric", "0.16.9", "1.21.1"), tmp_path, JAVA, said.append)
    assert not (tmp_path / "fabric-server-launch.jar").exists()


async def test_neoforge_is_installed_with_its_installer_and_started_through_the_argument_file(tmp_path):
    net = FakeInternet()
    net.maven_installer("neoforge", "1.21.1", "21.1.77")
    installer, fake, said = installer_for(net)
    launch = await installer.install(LoaderSpec("neoforge", "21.1.77", "1.21.1"), tmp_path, JAVA, said.append)

    argv, cwd = fake.calls[0]
    assert argv == [JAVA, "-jar", "neoforge-installer.jar", "--installServer"] and cwd == str(tmp_path)
    assert launch["argfile"].startswith("libraries/net/neoforged/neoforge/21.1.77/") and launch["argfile"].endswith("_args.txt")
    assert launch_arguments(launch) == [f"@{launch['argfile']}"]
    assert not (tmp_path / "neoforge-installer.jar").exists(), "the installer is removed afterwards"
    assert any("installer" in message for message in said)


async def test_neoforge_for_minecraft_1_20_1_still_lives_under_its_old_name(tmp_path):
    net = FakeInternet()
    net.maven_installer("neoforge", "1.20.1", "47.1.106")
    installer, fake, said = installer_for(net)
    # The fake installer writes the 1.21 layout; what matters here is that the right download was found and run.
    await installer.install(LoaderSpec("neoforge", "47.1.106", "1.20.1"), tmp_path, JAVA, said.append)
    assert fake.calls and any("net/neoforged/forge/1.20.1-47.1.106" in url for url in net.requests)


async def test_forge_since_1_17_is_started_through_the_argument_file(tmp_path):
    net = FakeInternet()
    net.maven_installer("forge", "1.20.1", "47.2.0")
    installer, fake, said = installer_for(net)
    launch = await installer.install(LoaderSpec("forge", "47.2.0", "1.20.1"), tmp_path, JAVA, said.append)
    assert launch["argfile"].startswith("libraries/net/minecraftforge/forge/1.20.1-47.2.0/")
    assert fake.calls[0][0][2:] == ["forge-installer.jar", "--installServer"]


async def test_old_forge_versions_leave_a_plain_jar_and_the_odd_1_7_10_name_is_found(tmp_path):
    net = FakeInternet()
    net.maven_installer("forge", "1.12.2", "14.23.5.2860")
    (tmp_path / ".legacy").write_text("")
    installer, _, said = installer_for(net)
    assert await installer.install(LoaderSpec("forge", "14.23.5.2860", "1.12.2"), tmp_path, JAVA, said.append) == {"jar": "forge-1.12.2-14.23.5.2860.jar"}

    other = FakeInternet()
    other.maven_installer("forge", "1.7.10", "10.13.4.1614", suffixed=True)
    installer, _, said = installer_for(other)
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / ".legacy").write_text("")
    (folder / "forge-1.12.2-14.23.5.2860.jar").write_bytes(b"PK")  # what the fake installer would have written
    await installer.install(LoaderSpec("forge", "10.13.4.1614", "1.7.10"), folder, JAVA, said.append)
    assert any("1.7.10-10.13.4.1614-1.7.10" in url for url in other.requests)


async def test_an_installer_with_a_wrong_checksum_is_never_run(tmp_path):
    net = FakeInternet()
    net.maven_installer("forge", "1.20.1", "47.2.0", wrong_checksum=True)
    installer, fake, said = installer_for(net)
    with pytest.raises(BadDownloadError, match="SHA256"):
        await installer.install(LoaderSpec("forge", "47.2.0", "1.20.1"), tmp_path, JAVA, said.append)
    assert fake.calls == [] and not (tmp_path / "forge-installer.jar").exists()


async def test_an_installer_without_any_published_checksum_is_not_even_downloaded(tmp_path):
    net = FakeInternet()
    net.maven_installer("forge", "1.20.1", "47.2.0", publish_checksum=False)
    installer, fake, said = installer_for(net)
    with pytest.raises(LoaderError, match="no published checksum"):
        await installer.install(LoaderSpec("forge", "47.2.0", "1.20.1"), tmp_path, JAVA, said.append)
    assert fake.calls == [] and not (tmp_path / "forge-installer.jar").exists()
    assert not any(url.endswith("-installer.jar") for url in net.requests), "a program that cannot be checked is not fetched"


async def test_an_older_installer_that_has_only_a_sha1_is_still_checked_and_installed(tmp_path):
    net = FakeInternet()
    net.maven_installer("forge", "1.12.2", "14.23.5.2860", sha1_only=True)
    (tmp_path / ".legacy").write_text("")
    installer, fake, said = installer_for(net)
    assert await installer.install(LoaderSpec("forge", "14.23.5.2860", "1.12.2"), tmp_path, JAVA, said.append) == {"jar": "forge-1.12.2-14.23.5.2860.jar"}
    assert fake.calls

    wrong = FakeInternet()
    wrong.maven_installer("forge", "1.12.2", "14.23.5.2860", sha1_only=True, wrong_checksum=True)
    installer, fake, said = installer_for(wrong)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(BadDownloadError, match="SHA1"):
        await installer.install(LoaderSpec("forge", "14.23.5.2860", "1.12.2"), other, JAVA, said.append)
    assert fake.calls == []


async def test_an_installer_whose_checksum_cannot_be_fetched_is_not_downloaded(tmp_path):
    net = FakeInternet()
    net.maven_installer("neoforge", "1.21.1", "21.1.77")
    url = "https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.77/neoforge-21.1.77-installer.jar"

    def broken(request):
        raise httpx.ReadTimeout("simulated", request=request)

    net.route(url + ".sha256", broken)
    net.route(url + ".sha1", broken)
    installer, fake, said = installer_for(net)
    with pytest.raises(LoaderError, match="could not be fetched"):
        await installer.install(LoaderSpec("neoforge", "21.1.77", "1.21.1"), tmp_path, JAVA, said.append)
    assert fake.calls == [] and url not in net.requests


async def test_a_loader_version_that_does_not_exist_is_explained(tmp_path):
    installer, fake, said = installer_for(FakeInternet())
    with pytest.raises(LoaderError, match="could not be downloaded"):
        await installer.install(LoaderSpec("forge", "99.9.9", "1.20.1"), tmp_path, JAVA, said.append)
    assert fake.calls == []


async def test_a_failing_installer_reports_what_it_said_and_cleans_up(tmp_path):
    net = FakeInternet()
    net.maven_installer("neoforge", "1.21.1", "21.1.77")
    installer, _, said = installer_for(net, FakeJava(exit_code=1, output=("Downloading libraries...", "ERROR: no route to host")))
    with pytest.raises(LoaderError, match="exit code 1.*no route to host"):
        await installer.install(LoaderSpec("neoforge", "21.1.77", "1.21.1"), tmp_path, JAVA, said.append)
    assert not (tmp_path / "neoforge-installer.jar").exists()


async def test_quilt_is_installed_with_its_installer_into_the_server_folder(tmp_path):
    net = FakeInternet()
    net.quilt("0.15.1")
    installer, fake, said = installer_for(net)
    launch = await installer.install(LoaderSpec("quilt", "0.26.0", "1.21.1"), tmp_path, JAVA, said.append)
    assert launch == {"jar": "quilt-server-launch.jar"}
    argv = fake.calls[0][0]
    assert argv[:3] == [JAVA, "-jar", "quilt-installer-0.15.1.jar"] and argv[3:7] == ["install", "server", "1.21.1", "0.26.0"]
    assert "--download-server" in argv and f"--install-dir={tmp_path}" in argv


async def test_a_quilt_installer_that_does_not_match_the_repositorys_checksum_is_never_run(tmp_path):
    net = FakeInternet()
    net.quilt("0.15.1", wrong_checksum=True)
    installer, fake, said = installer_for(net)
    with pytest.raises(BadDownloadError, match="SHA256"):
        await installer.install(LoaderSpec("quilt", "0.26.0", "1.21.1"), tmp_path, JAVA, said.append)
    assert fake.calls == [] and not list(tmp_path.glob("quilt-installer-*.jar"))


async def test_a_quilt_installer_without_a_published_checksum_is_never_downloaded_or_run(tmp_path):
    net = FakeInternet()
    net.quilt("0.15.1", publish_checksum=False)
    installer, fake, said = installer_for(net)
    with pytest.raises(DownloadError, match="no published checksum"):
        await installer.install(LoaderSpec("quilt", "0.26.0", "1.21.1"), tmp_path, JAVA, said.append)
    assert fake.calls == [] and not list(tmp_path.glob("quilt-installer-*.jar"))
    assert not any(url.endswith("quilt-installer-0.15.1.jar") for url in net.requests)


def test_the_launch_command_is_found_from_what_is_on_disk(tmp_path):
    assert find_launch(tmp_path, "neoforge") is None
    args = tmp_path / "libraries" / "net" / "neoforged" / "neoforge" / "21.1.77"
    args.mkdir(parents=True)
    (args / "unix_args.txt").write_text("x")
    (args / "win_args.txt").write_text("x")
    found = find_launch(tmp_path, "neoforge")
    assert found and found["argfile"].startswith("libraries/net/neoforged/neoforge/21.1.77/")
    assert find_launch(tmp_path, "forge") is None, "another loader's files do not count"
    (tmp_path / "forge-1.12.2-14.23.5.2860-installer.jar").write_text("x")
    assert find_launch(tmp_path, "forge") is None, "an installer is not a launcher"
    (tmp_path / "forge-1.12.2-14.23.5.2860.jar").write_text("x")
    assert find_launch(tmp_path, "forge") == {"jar": "forge-1.12.2-14.23.5.2860.jar"}
    with pytest.raises(LoaderError):
        launch_arguments({})
