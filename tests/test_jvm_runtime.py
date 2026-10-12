"""Java runtimes: choosing the right download, checking it, installing it safely, and knowing which Java a Minecraft needs."""

from __future__ import annotations

import json

import pytest
from jvmfakes import FakeInternet

from pumpkin_manager.jvm.mojang import MojangMeta, available_java, guess_java_major
from pumpkin_manager.jvm.net import BadDownloadError, DownloadError
from pumpkin_manager.jvm.runtime import META_FILE, AdoptiumSource, JavaDownloads, JavaRuntimes, adoptium_platform


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_kv(self, key: str) -> str | None:
        return self.values.get(key)

    def set_kv(self, key: str, value: str) -> None:
        self.values[key] = value


def runtimes_for(tmp_path, net: FakeInternet, *, kind: str = "linux", verifier=None, **kwargs) -> JavaRuntimes:
    source = AdoptiumSource(net.client(), kind, "x64", retry_delay=0)
    return JavaRuntimes(tmp_path / "java", lambda: source, verifier=verifier, **kwargs)


def pinned_runtimes(tmp_path, net: FakeInternet, os_name: str = "linux", arch: str = "x64", **kwargs) -> JavaRuntimes:
    """Runtimes that come from where the manager really gets them: Adoptium, and Azul for what a pin names."""
    source = JavaDownloads(net.client(), os_name, arch, retry_delay=0)
    return JavaRuntimes(tmp_path / "java", lambda: source, verifier=None, **kwargs)


def forget_the_pin(runtimes: JavaRuntimes, major: int) -> None:
    """Make an installed runtime look like one from before pins existed."""
    meta_path = runtimes.home(major) / META_FILE
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("pin")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


# ------------------------------------------------------------------------------- Adoptium
def test_the_download_platform_is_named_the_way_adoptium_names_it():
    assert adoptium_platform("x86_64", system="linux", musl=True) == ("alpine-linux", "x64")
    assert adoptium_platform("aarch64", system="linux", musl=False) == ("linux", "aarch64")
    assert adoptium_platform("AMD64", system="windows") == ("windows", "x64")
    with pytest.raises(DownloadError, match="architecture"):
        adoptium_platform("riscv64", system="linux", musl=False)


async def test_a_java_runtime_is_downloaded_checked_and_installed_once(tmp_path):
    net = FakeInternet()
    net.adoptium(21)
    runtimes = runtimes_for(tmp_path, net)
    assert runtimes.get(21) is None and runtimes.installed() == []

    progress: list[tuple[int, int]] = []
    installed = await runtimes.ensure(21, lambda done, total: progress.append((done, total)))
    assert installed.major == 21 and installed.release == "jdk-21.0.12.1+1" and installed.java.is_file()
    assert progress and progress[-1][0] == progress[-1][1], "the download reports its progress up to 100%"
    assert [j.major for j in runtimes.installed()] == [21] and runtimes.get(21).size > 0

    await runtimes.ensure(21)
    assert net.java_downloads == 1, "a runtime that is already there is not downloaded again"
    assert list((tmp_path / "java" / ".partial").iterdir()) == [], "nothing is left behind"


async def test_zip_archives_work_too(tmp_path):
    net = FakeInternet()
    net.adoptium(17, kind="zip", os_name="windows")
    runtimes = runtimes_for(tmp_path, net, kind="windows")
    assert (await runtimes.ensure(17)).java.is_file()


async def test_a_download_with_the_wrong_checksum_is_thrown_away(tmp_path):
    net = FakeInternet()
    net.adoptium(21, corrupt=True)
    runtimes = runtimes_for(tmp_path, net)
    with pytest.raises(BadDownloadError, match="SHA256"):
        await runtimes.ensure(21)
    assert runtimes.get(21) is None and not runtimes.home(21).exists()


@pytest.mark.parametrize("kind", ["tar.gz", "zip"])
async def test_an_archive_that_tries_to_leave_its_folder_is_refused(tmp_path, kind):
    net = FakeInternet()
    net.adoptium(21, kind=kind, evil_member="../../escape.txt")
    runtimes = runtimes_for(tmp_path, net)
    with pytest.raises(BadDownloadError):
        await runtimes.ensure(21)
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path.parent / "escape.txt").exists()
    assert runtimes.get(21) is None


async def test_a_runtime_that_does_not_start_is_not_installed(tmp_path):
    net = FakeInternet()
    net.adoptium(21)

    async def broken(java):
        raise DownloadError("Error loading shared library libz.so.1")

    runtimes = runtimes_for(tmp_path, net, verifier=broken)
    with pytest.raises(DownloadError, match="libz"):
        await runtimes.ensure(21)
    assert runtimes.get(21) is None


async def test_a_newer_build_replaces_the_installed_one_only_when_asked(tmp_path):
    net = FakeInternet()
    net.adoptium(21)
    runtimes = runtimes_for(tmp_path, net)
    await runtimes.ensure(21)
    net.adoptium(21, release="jdk-21.0.13+1")
    assert (await runtimes.ensure(21)).release == "jdk-21.0.12.1+1"
    assert (await runtimes.ensure(21, force=True)).release == "jdk-21.0.13+1"
    assert [p.name for p in (tmp_path / "java").iterdir() if p.name != ".partial"] == ["21"], "the old copy is gone"
    runtimes.remove(21)
    assert runtimes.get(21) is None


async def test_it_refuses_to_start_a_download_when_the_disk_is_too_small(tmp_path):
    net = FakeInternet()
    net.adoptium(21)
    runtimes = runtimes_for(tmp_path, net, free_space=lambda path: 1000)
    with pytest.raises(DownloadError, match="free disk space"):
        await runtimes.ensure(21)
    assert net.java_downloads == 0


async def test_a_platform_without_a_build_gets_an_explanation(tmp_path):
    net = FakeInternet()
    net.adoptium(21, os_name="windows")
    with pytest.raises(DownloadError, match="no Java 21 runtime for linux"):
        await runtimes_for(tmp_path, net).ensure(21)


# ---------------------------------------------------------------------------- the Java 8 pin
async def test_java_8_stays_on_the_pinned_build_while_other_versions_follow_the_newest(tmp_path):
    """Forge for Minecraft 1.16.5 (up to 36.2.25) cannot run on a Java 8 newer than 8u312."""
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09")
    net.adoptium_release(8, "jdk8u312-b07")
    net.adoptium(21)
    runtimes = pinned_runtimes(tmp_path, net)

    java8 = await runtimes.ensure(8)
    assert java8.release == "jdk8u312-b07" and java8.pin == "8u312" and java8.java.is_file()
    java21 = await runtimes.ensure(21)
    assert java21.release == "jdk-21.0.12.1+1" and java21.pin == ""
    assert net.java_downloads == 2


async def test_alpine_gets_the_pinned_java_8_from_azul_because_temurin_never_built_it_there(tmp_path):
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09", os_name="alpine-linux")  # the newest Temurin for Alpine is much newer
    net.adoptium_release(8, "jdk8u312-b07", os_name="linux")  # ... and the pinned one exists for glibc only
    net.azul("8.0.312", os_name="alpine-linux")
    installed = await pinned_runtimes(tmp_path, net, "alpine-linux").ensure(8)
    assert installed.release == "zulu8.58.0.13-ca-jre8.0.312" and installed.pin == "8u312" and installed.java.is_file()
    assert any("api.azul.com" in url and "java_version=8.0.312" in url and "os=linux-musl" in url for url in net.requests)


async def test_an_azul_download_is_checked_like_every_other_download(tmp_path):
    net = FakeInternet()
    net.azul("8.0.312", os_name="alpine-linux", corrupt=True)
    runtimes = pinned_runtimes(tmp_path, net, "alpine-linux")
    with pytest.raises(BadDownloadError, match="SHA256"):
        await runtimes.ensure(8)
    assert runtimes.get(8) is None

    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09", os_name="alpine-linux")
    net.azul("8.0.312", os_name="alpine-linux", missing_hash=True)  # a listing without a checksum is not trusted
    installed = await pinned_runtimes(tmp_path / "other", net, "alpine-linux").ensure(8)
    assert installed.release == "jdk8u492-b09", "without a checksum the pinned build is not used, the newest Temurin is"


async def test_a_platform_without_the_pinned_build_gets_the_newest_once(tmp_path, caplog):
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09")
    runtimes = pinned_runtimes(tmp_path, net)
    with caplog.at_level("WARNING"):
        installed = await runtimes.ensure(8)
    assert installed.release == "jdk8u492-b09" and installed.pin == "8u312"
    assert "no Java 8 build 8u312" in caplog.text
    requests = len(net.requests)
    await runtimes.ensure(8)
    assert len(net.requests) == requests and net.java_downloads == 1, "the pin was applied, so it is not looked for at every start"


async def test_a_java_8_from_before_the_pin_is_replaced_once(tmp_path):
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09")
    net.adoptium_release(8, "jdk8u312-b07")
    runtimes = runtimes_for(tmp_path, net)  # the way the manager used to install it: the newest build
    assert (await runtimes.ensure(8)).release == "jdk8u492-b09"
    forget_the_pin(runtimes, 8)
    assert runtimes.get(8).pin == ""

    runtimes = pinned_runtimes(tmp_path, net)
    replaced = await runtimes.ensure(8)
    assert replaced.release == "jdk8u312-b07" and replaced.pin == "8u312" and net.java_downloads == 2
    assert [p.name for p in (tmp_path / "java").iterdir() if p.name != ".partial"] == ["8"], "the old copy is gone"
    await runtimes.ensure(8)
    assert net.java_downloads == 2, "once is enough"


async def test_a_runtime_is_not_replaced_while_a_server_runs_on_it(tmp_path):
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09")
    net.adoptium_release(8, "jdk8u312-b07")
    runtimes = runtimes_for(tmp_path, net)
    await runtimes.ensure(8)
    forget_the_pin(runtimes, 8)

    busy = {"8": True}
    runtimes = pinned_runtimes(tmp_path, net, in_use=lambda major: busy[str(major)])
    assert (await runtimes.ensure(8)).release == "jdk8u492-b09" and net.java_downloads == 1, "a running server keeps the Java it has"
    busy["8"] = False
    assert (await runtimes.ensure(8)).release == "jdk8u312-b07", "the next start after it stopped does the swap"


async def test_a_replacement_that_fails_leaves_the_installed_java_in_use(tmp_path):
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09")
    net.adoptium_release(8, "jdk8u312-b07")
    runtimes = runtimes_for(tmp_path, net)
    await runtimes.ensure(8)
    forget_the_pin(runtimes, 8)

    runtimes = pinned_runtimes(tmp_path, net)
    net.offline = True
    assert (await runtimes.ensure(8)).release == "jdk8u492-b09", "a server must start even when the network is down"
    assert runtimes.get(8).java.is_file()

    net.offline = False
    requests = len(net.requests)
    assert (await runtimes.ensure(8)).release == "jdk8u492-b09" and len(net.requests) == requests, "it does not hammer the network"
    assert (await pinned_runtimes(tmp_path, net).ensure(8)).release == "jdk8u312-b07", "after a restart, with the network back, it works"


async def test_a_fresh_install_that_fails_is_still_an_error(tmp_path):
    net = FakeInternet()
    net.offline = True
    with pytest.raises(DownloadError):
        await pinned_runtimes(tmp_path, net).ensure(8)


async def test_forcing_a_download_installs_the_pinned_build_too(tmp_path):
    net = FakeInternet()
    net.adoptium(8, release="jdk8u492-b09")
    net.adoptium_release(8, "jdk8u312-b07")
    runtimes = pinned_runtimes(tmp_path, net)
    await runtimes.ensure(8)
    assert (await runtimes.ensure(8, force=True)).release == "jdk8u312-b07" and net.java_downloads == 2


# --------------------------------------------------------------------------------- Mojang
def test_the_java_a_minecraft_version_needs_when_mojang_cannot_be_asked():
    expected = {
        "26.3": 25, "26.1": 25, "1.21.1": 21, "1.21": 21, "1.20.5": 21, "1.20.4": 17, "1.20.1": 17, "1.19.2": 17, "1.18": 17,
        "1.17.1": 17, "1.16.5": 8, "1.12.2": 8, "1.7.10": 8, "1.21.4-pre1": 21, "nonsense": 21,
    }
    assert {mc: guess_java_major(mc) for mc in expected} == expected


def test_a_java_version_nobody_ships_is_mapped_to_the_next_one():
    assert [available_java(m) for m in (8, 16, 17, 18, 21, 22, 25)] == [8, 17, 17, 21, 21, 25, 25]
    with pytest.raises(DownloadError, match="Java 26"):
        available_java(26)


async def test_the_java_version_comes_from_mojangs_metadata_and_is_remembered():
    net = FakeInternet()
    net.minecraft({"26.3": 25, "1.21.1": 21, "1.20.1": 17, "1.16.5": 8, "1.12.2": None})
    store = MemoryStore()
    meta = MojangMeta(net.client(), store, retry_delay=0)
    assert [await meta.java_major(v) for v in ("26.3", "1.21.1", "1.20.1", "1.16.5", "1.12.2")] == [25, 21, 17, 8, 8]

    requests = len(net.requests)
    assert await meta.java_major("1.21.1") == 21 and len(net.requests) == requests, "a known answer needs no network"
    net.offline = True
    assert await MojangMeta(net.client(), store, retry_delay=0).java_major("1.20.1") == 17, "it is remembered across restarts"


async def test_without_network_or_without_a_listing_the_table_is_used_but_not_remembered():
    net = FakeInternet()
    net.minecraft({"1.21.1": 21})
    store = MemoryStore()
    meta = MojangMeta(net.client(), store, retry_delay=0)
    assert await meta.java_major("1.20.4") == 17, "Mojang does not list this version"
    net.offline = True
    assert await MojangMeta(net.client(), MemoryStore(), retry_delay=0).java_major("1.21.1") == 21
    assert store.values == {}, "a guess is never stored as if Mojang had said it"
