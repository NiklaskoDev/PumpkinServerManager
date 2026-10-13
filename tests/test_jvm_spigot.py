"""SpigotMC: what its hub publishes, and the builder that makes Spigot with BuildTools (against a fake BuildTools, and for the process
handling against real child processes)."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest
from jvmfakes import FakeBungee, FakeInternet, FakeSpigot

from pumpkin_manager.jvm import spigot
from pumpkin_manager.jvm.net import DownloadError
from pumpkin_manager.jvm.runtime import JavaDownloads, JavaRuntimes
from pumpkin_manager.jvm.spigot import (
    BuildError,
    BuildProgress,
    SpigotBuilder,
    SpigotHub,
    build_environment,
    java_floor,
    run_build,
)


def world() -> tuple[FakeInternet, FakeSpigot, FakeBungee]:
    net = FakeInternet()
    net.adoptium(21, image="jdk")
    net.adoptium(17, release="jdk-17.0.20.1+1", image="jdk")
    fake = FakeSpigot(net)
    for version, name, java in (("1.21.4", "4458", (65, 68)), ("1.20.4", "4023", (61, 65)), ("1.8.8", "582b", None)):
        fake.add_version(version, name, java=java)
    bungee = FakeBungee(net)
    for number, result in ((2105, "UNSTABLE"), (2104, "SUCCESS"), (2102, "FAILURE"), (2101, "SUCCESS")):
        bungee.add_build(number, result)
    return net, fake, bungee


def hub_of(net: FakeInternet) -> SpigotHub:
    return SpigotHub(net.client(), retry_delay=0.01)


def builder_of(tmp_path: Path, net: FakeInternet, fake: FakeSpigot) -> SpigotBuilder:
    jdks = JavaRuntimes(tmp_path / "jdk", lambda: JavaDownloads(net.client(), "linux", "x64", 0.01, image_type="jdk"), verifier=None, tools=("javac",))
    return SpigotBuilder(net.client(), jdks, tmp_path / "build", fake.runner, retry_delay=0.01)


# -------------------------------------------------------------------------------------------------------------------------------- the hub
async def test_the_hub_lists_every_version_with_when_its_file_changed():
    net, fake, _ = world()
    listed = dict(await hub_of(net).versions())
    assert listed["1.21.4"] == "2026-10-04T02:43:00Z" and "latest" not in listed
    assert {"1.21.4", "1.20.4", "1.8.8", "1.13-pre7", "1.18-rc3"} <= set(listed), "the pre-releases are in the list too: the catalog filters them"


async def test_an_index_without_any_version_is_called_what_it_is():
    net, fake, _ = world()
    fake.versions.clear()
    fake.pre_releases.clear()
    with pytest.raises(DownloadError, match="empty or has changed its layout"):
        await hub_of(net).versions()


async def test_an_index_that_does_not_answer_is_an_error_with_the_status():
    net, fake, _ = world()
    fake.index_status = 503
    with pytest.raises(DownloadError, match="503"):
        await hub_of(net).versions()


async def test_the_file_of_a_version_says_its_build_and_what_it_can_be_built_with():
    net, fake, _ = world()
    info = await hub_of(net).version_info("1.21.4")
    assert info["name"] == "4458" and info["javaVersions"] == [65, 68] and set(info["refs"]) == {"BuildData", "Bukkit", "CraftBukkit", "Spigot"}
    assert "javaVersions" not in await hub_of(net).version_info("1.8.8"), "the oldest versions say nothing about Java"


async def test_a_version_spigotmc_has_no_file_for_is_a_404():
    net, fake, _ = world()
    with pytest.raises(DownloadError) as caught:
        await hub_of(net).version_info("1.2.3")
    assert caught.value.status == 404


@pytest.mark.parametrize("info,expected", [
    ({"javaVersions": [65, 68]}, 21), ({"javaVersions": [52, 60]}, 8), ({"javaVersions": [69, 70]}, 25), ({}, None), ({"javaVersions": []}, None),
    ({"javaVersions": ["65", "68"]}, None), ({"javaVersions": [True, 1]}, None), ({"javaVersions": [40, 41]}, None), ({"javaVersions": "65"}, None),
])
def test_the_oldest_java_is_read_from_the_class_file_versions(info, expected):
    assert java_floor(info) == expected


async def test_bungeecord_builds_are_the_successful_ones_newest_first():
    net, _, bungee = world()
    builds = await hub_of(net).bungee_builds()
    assert [b["number"] for b in builds] == [2104, 2101], "unstable and failed builds are not offered"
    assert builds[0]["published"].endswith("Z") and builds[0]["published"] > builds[1]["published"]
    asked = [url for url in net.requests if "BungeeCord/api/json" in url]
    assert len(asked) == 1 and "tree=builds" in asked[0] and "0%2C60" in asked[0], "only the newest 60 builds are asked for, not all of them"


async def test_the_number_of_builds_asked_for_is_limited():
    net, _, bungee = world()
    for number in range(2000, 2100):
        bungee.add_build(number)
    # the five newest are 2105 (unstable), 2104, 2102 (failed), 2101 and 2099: three of them are good
    assert [b["number"] for b in await hub_of(net).bungee_builds(limit=5)] == [2104, 2101, 2099]


async def test_a_build_server_without_a_good_build_is_an_error():
    net, _, bungee = world()
    bungee.builds.clear()
    bungee.add_build(5, "FAILURE")
    with pytest.raises(DownloadError, match="no successful BungeeCord build"):
        await hub_of(net).bungee_builds()


# -------------------------------------------------------------------------------------------------------------------------------- progress
def test_a_failure_quotes_the_lines_that_name_the_problem_not_just_the_last_ones():
    tail = ["Starting clone", "[INFO] Total time: 01:45 min", "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile",
            "[ERROR] Could not resolve dependencies for project org.spigotmc:spigot-api:jar:1.21.4-R0.1-SNAPSHOT", "[INFO] ", "[INFO] For more information, run Maven with -e"]
    detail = spigot.failure_detail(tail)
    assert "Failed to execute goal" in detail and "Could not resolve dependencies" in detail and "Total time" not in detail and "For more information" not in detail
    assert spigot.failure_detail(["one", "two", "three"]) == "one | two | three" and spigot.failure_detail([]) == "no output"
    assert len(spigot.failure_detail(["[ERROR] x"] * 20).split(" | ")) == 5 and len(spigot.failure_detail(["Exception " + "y" * 500])) < 230


def test_the_stages_of_a_build_are_told_once_each_and_in_order():
    told: list[tuple[float, str]] = []
    progress = BuildProgress(lambda fraction, message: told.append((fraction, message)))
    lines = [
        "Loading BuildTools version: git-BuildTools-5fc1010-201 (#201)", "Starting clone of https://hub.spigotmc.org/stash/scm/spigot/bukkit.git to Bukkit",
        "Starting clone of https://hub.spigotmc.org/stash/scm/spigot/spigot.git to Spigot", "Maven does not exist, downloading. Please wait.",
        "Attempting to build Minecraft with details: VersionInfo(minecraftVersion=1.21.4)", "Final mapped jar: work\\mapped.jar does not exist, creating (please wait)!",
        "INFO:  Decompiling class net/minecraft/SharedConstants", "INFO:  Decompiling class net/minecraft/Other", "Applying CraftBukkit Patches",
        "Maven does not exist again", "  Rebuilding Forked projects.... ", "[INFO] Building Spigot-API 1.21.4-R0.1-SNAPSHOT                           [1/3]",
        "[INFO] Building Spigot-Parent dev-SNAPSHOT                                [2/3]", "[INFO] Building Spigot 1.21.4-R0.1-SNAPSHOT                               [3/3]",
        "Success! Everything completed successfully. Copying final .jar files now.",
    ]
    for line in lines:
        progress.feed(line)
    assert [m for _, m in told] == [
        "Downloading Spigot's source code", "Downloading Maven, the tool that compiles it", "Getting Minecraft's server from Mojang",
        "Giving Minecraft's classes readable names", "Decompiling Minecraft (the slowest part)", "Applying CraftBukkit's changes",
        "Applying Spigot's changes", "Compiling Spigot", "Spigot is built",
    ]
    fractions = [f for f, _ in told]
    assert fractions == sorted(fractions) and 0 < fractions[0] and fractions[-1] < 1


# ------------------------------------------------------------------------------------------------------------------------- the environment
def test_buildtools_runs_with_the_jdk_first_a_home_of_its_own_and_an_identity_for_git(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSEFORGE_API_KEY", "must-not-reach-buildtools")
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    jdk, home = tmp_path / "jdk-21", tmp_path / "home"
    env = build_environment(jdk, home)
    assert env["PATH"].split(os.pathsep)[0] == str(jdk / "bin") and env["JAVA_HOME"] == str(jdk) and env["HOME"] == str(home)
    assert env["JAVA_TOOL_OPTIONS"] == f"-Duser.home={home}", "every JVM it starts agrees where 'home' is"
    assert env["GIT_COMMITTER_NAME"] and env["GIT_AUTHOR_EMAIL"] and env["GIT_TERMINAL_PROMPT"] == "0"
    assert "CURSEFORGE_API_KEY" not in env, "only the environment a child program needs"


def test_a_home_with_a_space_in_its_path_is_quoted_for_the_jvms(tmp_path):
    env = build_environment(tmp_path / "jdk", tmp_path / "my home")
    assert env["JAVA_TOOL_OPTIONS"] == f'"-Duser.home={tmp_path / "my home"}"'


# ------------------------------------------------------------------------------------------------------------------------ the builder
async def test_spigot_is_built_with_a_jdk_and_only_the_jar_is_kept(tmp_path):
    net, fake, _ = world()
    builder = builder_of(tmp_path, net, fake)
    said: list[str] = []
    fractions: list[float] = []
    target = tmp_path / "server"
    jar = await builder.build("1.21.4", target, 21, said.append, lambda done, total: fractions.append(done / total))

    assert jar == target / "spigot.jar" and jar.read_bytes() == FakeSpigot.jar("1.21.4", "4458") and sorted(p.name for p in target.iterdir()) == ["spigot.jar"]
    call = fake.calls[0]
    java = Path(call["argv"][0])
    assert java.is_file() and java.parent.parent == tmp_path / "jdk" / "21", "the JDK the manager downloaded for it"
    assert call["argv"][1:] == ["-jar", "BuildTools.jar", "--rev", "1.21.4", "--nogui", "--compile", "SPIGOT", "--final-name", "spigot.jar", "-o", call["argv"][-1]]
    assert call["env"]["JAVA_HOME"] == str(tmp_path / "jdk" / "21") and Path(call["env"]["HOME"]).parent == Path(call["cwd"]), "its home is inside its folder"
    assert net.jdk_downloads == 1 and fake.buildtools_downloads == 1
    assert not (tmp_path / "build").exists() or list((tmp_path / "build").iterdir()) == [], "the folder of the build is gone with its Maven repository"
    assert any("Decompiling" in text for text in said) and any("five to fifteen minutes" in text for text in said) and any("development kit" in text for text in said)
    assert fractions == sorted(fractions) and fractions[-1] > 0.9


async def test_the_folder_of_a_build_is_removed_even_when_git_left_read_only_files_in_it(tmp_path):
    """BuildTools clones with git, whose objects are read-only: on Windows such files cannot be deleted until the flag is cleared (the first
    real build there left 2600 of them behind)."""
    net, fake, _ = world()
    fake.leave_read_only = True
    await builder_of(tmp_path, net, fake).build("1.21.4", tmp_path / "server", 21, lambda text: None)
    assert (tmp_path / "server" / "spigot.jar").is_file() and not list((tmp_path / "build").iterdir())
    leftover = tmp_path / "build" / "dead1234" / "Bukkit" / ".git" / "objects" / "pack"
    leftover.mkdir(parents=True)
    (leftover / "pack-1.pack").write_bytes(b"x")
    os.chmod(leftover / "pack-1.pack", stat.S_IREAD)
    assert builder_of(tmp_path, net, fake).purge() == 1 and not list((tmp_path / "build").iterdir()), "and what a dead build left behind goes too"


async def test_the_jdk_is_downloaded_once_and_used_for_every_build(tmp_path):
    net, fake, _ = world()
    builder = builder_of(tmp_path, net, fake)
    await builder.build("1.21.4", tmp_path / "a", 21, lambda text: None)
    await builder.build("1.21.4", tmp_path / "b", 21, lambda text: None)
    assert net.jdk_downloads == 1 and fake.buildtools_downloads == 2, "BuildTools itself is fetched again: it is the newest one that is wanted"


async def test_a_failed_build_says_what_buildtools_said_and_leaves_nothing_behind(tmp_path):
    net, fake, _ = world()
    fake.fail = "Exception in thread main java.io.IOException: Server returned HTTP response code: 503"
    target = tmp_path / "server"
    with pytest.raises(BuildError, match=r"BuildTools failed \(exit code 1\).*HTTP response code: 503"):
        await builder_of(tmp_path, net, fake).build("1.21.4", target, 21, lambda text: None)
    assert not target.exists() or not list(target.iterdir())
    assert not list((tmp_path / "build").iterdir()), "no folder of the failed build is left"


async def test_a_build_that_ends_well_but_makes_no_jar_is_an_error(tmp_path):
    net, fake, _ = world()

    async def silent(argv, cwd, env, on_line):
        return 0, []

    builder = builder_of(tmp_path, net, fake)
    builder._run = silent
    with pytest.raises(BuildError, match="left no jar file behind"):
        await builder.build("1.21.4", tmp_path / "server", 21, lambda text: None)


async def test_a_jar_that_is_not_a_jar_is_an_error(tmp_path):
    net, fake, _ = world()

    async def garbage(argv, cwd, env, on_line):
        (Path(argv[argv.index("-o") + 1]) / "spigot.jar").write_bytes(b"not a zip at all" * 10)
        return 0, []

    builder = builder_of(tmp_path, net, fake)
    builder._run = garbage
    with pytest.raises(BuildError, match="not a complete jar file"):
        await builder.build("1.21.4", tmp_path / "server", 21, lambda text: None)
    assert not (tmp_path / "server" / "spigot.jar").exists()


async def test_without_room_for_the_build_nothing_is_started(tmp_path, monkeypatch):
    net, fake, _ = world()
    monkeypatch.setattr(spigot, "free_bytes", lambda path: 100 * 1024 * 1024)
    with pytest.raises(BuildError, match=r"Not enough free disk space to build Spigot: about 2\.0 GiB"):
        await builder_of(tmp_path, net, fake).build("1.21.4", tmp_path / "server", 21, lambda text: None)
    assert not fake.calls and net.jdk_downloads == 0, "nothing was downloaded for a build that cannot be made"


async def test_a_buildtools_that_cannot_be_downloaded_is_an_error_naming_it(tmp_path):
    net, fake, _ = world()
    net.routes.pop((fake.HOST, fake.BUILDTOOLS))
    with pytest.raises(BuildError, match="BuildTools"):
        await builder_of(tmp_path, net, fake).build("1.21.4", tmp_path / "server", 21, lambda text: None)


async def test_a_buildtools_download_that_is_not_a_jar_is_refused(tmp_path):
    net, fake, _ = world()
    net.blob(f"https://{fake.HOST}{fake.BUILDTOOLS}", b"<html>a login page</html>")
    with pytest.raises(BuildError, match="not a complete jar file"):
        await builder_of(tmp_path, net, fake).build("1.21.4", tmp_path / "server", 21, lambda text: None)
    assert not fake.calls


async def test_a_jdk_that_cannot_be_downloaded_is_explained(tmp_path):
    net, fake, _ = world()
    net.offline = True
    with pytest.raises(BuildError, match="Java 21 development kit that builds Spigot could not be downloaded"):
        await builder_of(tmp_path, net, fake).build("1.21.4", tmp_path / "server", 21, lambda text: None)


async def test_an_archive_without_a_compiler_is_not_accepted_as_a_jdk(tmp_path):
    net = FakeInternet()
    net.adoptium(21)  # a JRE: it runs servers, it does not compile
    jdks = JavaRuntimes(tmp_path / "jdk", lambda: JavaDownloads(net.client(), "linux", "x64", 0.01), verifier=None, tools=("javac",))
    with pytest.raises(DownloadError, match="does not contain bin/javac"):
        await jdks.ensure(21)
    assert jdks.get(21) is None, "nothing half installed stays behind"


async def test_only_one_spigot_is_built_at_a_time(tmp_path):
    net, fake, _ = world()
    fake.delay = 0.01
    builder = builder_of(tmp_path, net, fake)
    said: list[str] = []
    await asyncio.gather(
        builder.build("1.21.4", tmp_path / "a", 21, said.append), builder.build("1.20.4", tmp_path / "b", 17, said.append),
    )
    assert fake.max_running == 1 and len(fake.calls) == 2
    assert sum("Another Spigot build is running" in text for text in said) == 1
    assert (tmp_path / "a" / "spigot.jar").read_bytes() == FakeSpigot.jar("1.21.4", "4458") and (tmp_path / "b" / "spigot.jar").read_bytes() == FakeSpigot.jar("1.20.4", "4023")
    assert fake.calls[1]["env"]["JAVA_HOME"].endswith(os.path.join("jdk", "17")) or fake.calls[0]["env"]["JAVA_HOME"].endswith(os.path.join("jdk", "17"))


async def test_a_build_that_is_cancelled_leaves_no_folder_behind(tmp_path):
    net, fake, _ = world()
    started = asyncio.Event()

    async def forever(argv, cwd, env, on_line):
        started.set()
        await asyncio.sleep(3600)
        return 0, []

    builder = builder_of(tmp_path, net, fake)
    builder._run = forever
    task = asyncio.create_task(builder.build("1.21.4", tmp_path / "server", 21, lambda text: None))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / "build").iterdir()) and not (tmp_path / "server" / "spigot.jar").exists()
    # and the lock is free again: the next build goes through
    builder._run = fake.runner
    assert (await builder.build("1.21.4", tmp_path / "server", 21, lambda text: None)).is_file()


def test_what_a_build_that_died_with_the_manager_left_behind_is_removed(tmp_path):
    net, fake, _ = world()
    builder = builder_of(tmp_path, net, fake)
    (tmp_path / "build" / "abc12345" / "work").mkdir(parents=True)
    (tmp_path / "build" / "abc12345" / "work" / "big.jar").write_bytes(b"x" * 1000)
    assert builder.purge() == 1 and not list((tmp_path / "build").iterdir())
    assert builder.purge() == 0


# ------------------------------------------------------------------------------------------------------ the real process handling
def python_program(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "program.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, "-u", str(script)]


@pytest.fixture
def git_on_path(monkeypatch, tmp_path):
    """run_build refuses to start where there is no git (outside Windows); the programs here are Python, so a stand-in git is put on the path."""
    if os.name != "nt":
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        git = fake_bin / "git"
        git.write_text("#!/bin/sh\nexit 0\n")
        git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")


async def test_every_line_is_handed_on_and_the_tail_leaves_out_the_noise(tmp_path, git_on_path):
    argv = python_program(tmp_path, """
        print("Starting clone of x"); print("Extracted: a"); print("Progress (1): 3 kB"); print("INFO:  Decompiling class a/B"); print(""); print("A real message")
    """)
    lines: list[str] = []
    code, tail = await run_build(argv, tmp_path, {**os.environ}, lines.append)
    assert code == 0 and lines[0] == "Starting clone of x" and "A real message" in lines and "Extracted: a" in lines
    assert tail == ["Starting clone of x", "A real message"]


async def test_the_exit_code_comes_back(tmp_path, git_on_path):
    code, tail = await run_build(python_program(tmp_path, 'import sys\nprint("boom")\nsys.exit(3)\n'), tmp_path, {**os.environ}, lambda line: None)
    assert code == 3 and tail == ["boom"]


async def test_a_build_that_says_nothing_for_too_long_is_stopped(tmp_path, git_on_path):
    started = time.monotonic()
    with pytest.raises(BuildError, match="printed nothing for"):
        await run_build(python_program(tmp_path, 'import time\nprint("hello", flush=True)\ntime.sleep(60)\n'), tmp_path, {**os.environ}, lambda line: None, idle=0.6)
    assert time.monotonic() - started < 15


async def test_a_build_that_never_ends_is_stopped(tmp_path, git_on_path):
    with pytest.raises(BuildError, match="did not finish within"):
        await run_build(python_program(tmp_path, 'import time\nwhile True:\n    print("busy", flush=True)\n    time.sleep(0.05)\n'), tmp_path, {**os.environ}, lambda line: None, timeout=0.6)


async def test_cancelling_a_build_ends_the_programs_it_started_too(tmp_path, git_on_path):
    pid_file = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(600)\n", encoding="utf-8")
    argv = python_program(tmp_path, f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, {str(child_script)!r}, {str(pid_file)!r}])
        print("started", flush=True)
        time.sleep(600)
    """)
    lines: list[str] = []
    task = asyncio.create_task(run_build(argv, tmp_path, {**os.environ}, lines.append))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    child = int(pid_file.read_text())
    assert psutil.pid_exists(child)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if not psutil.pid_exists(child):
            break
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(child), "the build's children do not outlive it"


@pytest.mark.skipif(os.name == "nt", reason="BuildTools brings its own git on Windows")
async def test_where_there_is_no_git_the_build_is_not_started_and_the_message_says_what_to_do(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BuildError, match="git is not installed here"):
        await run_build([sys.executable, "-c", "print(1)"], tmp_path, {"PATH": str(empty)}, lambda line: None)


async def test_a_program_that_cannot_be_started_is_an_error(tmp_path, git_on_path):
    with pytest.raises(BuildError, match="Could not start Java"):
        await run_build([str(tmp_path / "no-such-java")], tmp_path, {**os.environ}, lambda line: None)
