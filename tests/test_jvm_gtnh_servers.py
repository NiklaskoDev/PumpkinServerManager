"""GT New Horizons servers end to end: created from the official server files, started, updated in steps, rolled back, and the
CurseForge listing that only looks like the pack."""

from __future__ import annotations

import hashlib
import json
import tarfile

import pytest
from cftools import give_key
from conftest import finish
from jvmfakes import SERVERUTILITIES_CFG

from pumpkin_manager.jvm import packupdates
from pumpkin_manager.jvm import properties as props
from pumpkin_manager.servers import ServerError

FORGE_JAR = "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar"
LIBRARY = "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar"


async def try_create(ctx, name: str = "GTNH", *, modpack: dict | None = None, **spec) -> dict:
    body = {"name": name, "engine": "java", "eula": True, "modpack": {"source": "gtnh", **(modpack or {})}, "start": spec.pop("start", False), **spec}
    return await finish(ctx, ctx.servers.submit_create(body), timeout=120)


async def create_gtnh(ctx, name: str = "GTNH", **options):
    row = await try_create(ctx, name, **options)
    assert row["status"] == "success", row["message"]
    return next(s for s in ctx.repo.list_servers() if s.name == name), row


def props_of(ctx, rec) -> dict[str, str]:
    return props.parse((ctx.servers.server_dir(rec.id) / "server.properties").read_text(encoding="utf-8"))


def started_with(ctx, rec) -> dict:
    return json.loads((ctx.servers.server_dir(rec.id) / "fake-java.json").read_text(encoding="utf-8"))


def steps_of(ctx, row) -> list[str]:
    return [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]


def no_leftovers(ctx) -> bool:
    return not list(ctx.settings.servers_dir.glob(".create-*")) and not list((ctx.settings.cache_dir / "uploads").glob("*.zip"))


# ------------------------------------------------------------------------------------------------------- creating
async def test_the_newest_stable_version_is_installed_from_the_official_server_files_and_started(jvm):
    world, ctx = jvm
    rec, row = await create_gtnh(ctx, "GTNH", start=True, settings={"motd": "Hello"})
    assert rec.engine == "java" and rec.is_java and not rec.spec.get("software")

    spec = rec.spec
    assert (spec["kind"], spec["minecraft"], spec["java"], spec["memory_mb"]) == ("modpack", "1.7.10", 25, 6144), "Java 25 and the 6 GiB the pack asks for"
    assert spec["loader"] == {"kind": "forge", "version": "10.13.4.1614"}
    assert spec["launch"] == {"jar": "lwjgl3ify-forgePatches.jar", "before": ["-Dfml.readTimeout=180", "@java9args.txt"]}
    pack = spec["pack"]
    assert (pack["source"], pack["project"], pack["title"], pack["version_id"], pack["version_type"], pack["variant"]) == (
        "gtnh", "gtnh", "GT New Horizons", "2.8.4", "release", "modern"), "the newest stable version, not the release candidate"
    assert pack["server_pack"] is True and pack["java_max"] == 25 and pack["published"] == "2025-12-23"
    assert world.gtnh.downloads == ["/ServerPacks/GT_New_Horizons_2.8.4_Server_Java_17-25.zip"]

    directory = ctx.servers.server_dir(rec.id)
    for path in ("mods/gregtech-5.09.jar", "mods/NewHorizonsCoreMod.jar", LIBRARY, FORGE_JAR, "minecraft_server.1.7.10.jar", "lwjgl3ify-forgePatches.jar",
                 "java9args.txt", "serverutilities/server/ranks.txt", "config/GregTech/GregTech.cfg", "journeymap/config/5.2/journeymap.core.config", "server-icon.png"):
        assert (directory / path).is_file(), path
    assert not (directory / "startserver-java9.sh").exists() and not (directory / "startserver-java9.bat").exists(), "the manager starts the server itself"
    assert "eula=true" in (directory / "eula.txt").read_text() and no_leftovers(ctx)

    values = props_of(ctx, rec)
    assert values["level-type"] == "rwg" and values["difficulty"] == "3" and values["allow-flight"] == "true", "the pack's own settings stay"
    assert values["server-port"] == str(rec.java_port) and values["enable-rcon"] == "false" and values["motd"] == "Hello"

    su = (directory / "serverutilities" / "serverutilities.cfg").read_text()
    assert "B:enable_backups=false" in su and "B:enabled=true" in su, "the mod's own backups are off, nothing else of its configuration"
    lock = ctx.servers.jvm.lock_of(rec)
    assert lock["mode"] == "archive" and lock["source"] == "gtnh" and lock["version_id"] == "2.8.4" and lock["loader"]["kind"] == "forge"
    assert set(lock["files"]) == {f"mods/{n}.jar" for n in ("gregtech-5.09", "NewHorizonsCoreMod", "hodgepodge", "gtnhlib")}
    assert lock["overrides"]["serverutilities/serverutilities.cfg"] == hashlib.sha256((directory / "serverutilities" / "serverutilities.cfg").read_bytes()).hexdigest()

    assert rec.policy.start_timeout == 1800 and rec.policy.update_mode == "notify" and "backups/*" in rec.policy.backup_exclude and "logs" in rec.policy.backup_exclude
    steps = steps_of(ctx, row)
    assert any("Downloading GT New Horizons 2.8.4 (the server files for Java 17 and newer)" in s for s in steps)
    assert any("GT New Horizons runs on Java 25" in s for s in steps) and any("Switched off the backups of ServerUtilities" in s for s in steps)
    assert any("Not taken over from the pack's server files: 6 files (the manager looks after it)" in s for s in steps)
    assert not any("Installing Forge" in s for s in steps), "the server files bring Forge"

    assert ctx.pm.runtime(rec.id).state == "running"
    argv = started_with(ctx, rec)["argv"]
    assert argv[0] == str(ctx.runtimes.executable(25)) and argv[1:3] == ["-Xms6144M", "-Xmx6144M"]
    assert argv[-5:] == ["-Dfml.readTimeout=180", "@java9args.txt", "-jar", "lwjgl3ify-forgePatches.jar", "nogui"], "what the pack's own start script runs"
    assert "-XX:+UseG1GC" not in argv, "the default of modern Java"

    view = ctx.servers.view(rec)
    modpack = view["modpack"]
    assert (modpack["title"], modpack["source"], modpack["variant"], modpack["version_number"], modpack["server_pack"]) == ("GT New Horizons", "gtnh", "modern", "2.8.4", True)
    assert modpack["loader"]["label"] == "Forge 10.13.4.1614" and modpack["notice"] is None and view["java"]["major"] == 25
    assert rec.java_tag == "GT New Horizons 2.8.4"


async def test_the_build_for_java_8_runs_on_java_8_the_way_its_own_script_starts_it(jvm):
    world, ctx = jvm
    rec, row = await create_gtnh(ctx, "Legacy", modpack={"variant": "legacy"}, start=True)
    assert rec.spec["java"] == 8 and rec.spec["pack"]["variant"] == "legacy" and rec.spec["launch"]["jar"] == FORGE_JAR
    assert world.gtnh.downloads == ["/ServerPacks/GT_New_Horizons_2.8.4_Server_Java_8.zip"]
    directory = ctx.servers.server_dir(rec.id)
    assert not (directory / "java9args.txt").exists() and not (directory / "lwjgl3ify-forgePatches.jar").exists() and (directory / FORGE_JAR).is_file()
    argv = started_with(ctx, rec)["argv"]
    assert argv[0] == str(ctx.runtimes.executable(8)) and argv.index("-XX:+UseG1GC") < argv.index("-XX:+UseStringDeduplication"), "that option needs G1"
    assert argv[-7:] == ["-Dfml.readTimeout=180", "-XX:+UseStringDeduplication", "-XX:+UseCompressedOops", "-XX:+UseCodeCacheFlushing", "-jar", FORGE_JAR, "nogui"]
    assert any("runs on Java 8" in s for s in steps_of(ctx, row))


async def test_any_listed_version_can_be_chosen_and_gets_the_newest_java_its_build_is_made_for(jvm):
    world, ctx = jvm
    old, _ = await create_gtnh(ctx, "Old", modpack={"version": "2.7.4"})
    assert old.spec["java"] == 21 and old.spec["pack"]["version_id"] == "2.7.4" and old.spec["pack"]["java_max"] == 21
    beta, _ = await create_gtnh(ctx, "Beta", modpack={"version": "2.9.0-RC-2"}, memory_mb=8192, jvm_args="-Xss4M")
    assert beta.spec["java"] == 25, "the release candidate is made for Java 17 to 26: 25 is the newest that can be installed"
    assert (beta.spec["pack"]["version_type"], beta.spec["memory_mb"], beta.spec["jvm_args"]) == ("beta", 8192, "-Xss4M")
    assert (ctx.servers.server_dir(beta.id) / "mods" / "gregtech-5.09.jar").read_bytes() == b"gregtech 2.9.0-RC-2" * 30


async def test_what_cannot_be_installed_is_refused_and_leaves_nothing(jvm):
    world, ctx = jvm
    world.gtnh.add_release("2.6.0", legacy=False)
    for modpack, message in (
        ({"version": "1.2.3"}, "does not exist"), ({"variant": "java 11"}, "Choose the server files"),
        ({"version": "2.6.0", "variant": "legacy"}, "has no server files for Java 8"),
    ):
        row = await try_create(ctx, "Nope", modpack=modpack)
        assert row["status"] == "failed" and message in row["message"], row["message"]
    assert ctx.repo.list_servers() == [] and no_leftovers(ctx)

    world.gtnh.add_release("2.6.1", modern=False, legacy=True)
    ctx.servers.jvm.gtnh.invalidate()  # the page was read a moment ago
    row = await try_create(ctx, "Old only", modpack={"version": "2.6.1"}, start=False)
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_servers()[0].spec["pack"]["variant"] == "legacy"


async def test_a_download_that_leads_away_from_the_packs_host_is_refused(jvm):
    world, ctx = jvm
    world.gtnh.add_release("9.9.9", first=True, redirect_to="https://evil.example/server.zip")
    row = await try_create(ctx, "Evil", modpack={"version": "9.9.9"})
    assert row["status"] == "failed" and "not one of the allowed hosts" in row["message"]
    assert ctx.repo.list_servers() == [] and no_leftovers(ctx) and not any("evil.example" in url for url in world.net.requests)


async def test_server_files_that_cannot_be_what_they_claim_to_be_fail_before_anything_is_installed(jvm):
    world, ctx = jvm
    mods = {f"mods/{n}.jar": None for n in ("gregtech-5.09", "NewHorizonsCoreMod", "hodgepodge", "gtnhlib")}
    world.gtnh.add_release("8.0.0", first=True, changes=mods)
    world.gtnh.add_release("8.0.1", first=True, changes={"java9args.txt": None})
    world.gtnh.add_release("8.0.2", first=True)
    world.net.blob("https://downloads.gtnewhorizons.com/ServerPacks/GT_New_Horizons_8.0.2_Server_Java_17-25.zip", b"this is not a zip file")
    for version, message in (("8.0.0", "hold no mods"), ("8.0.1", "hold no lwjgl3ify-forgePatches.jar and java9args.txt"), ("8.0.2", "not a usable zip")):
        row = await try_create(ctx, "Broken", modpack={"version": version})
        assert row["status"] == "failed" and message in row["message"], (version, row["message"])
    assert ctx.repo.list_servers() == [] and no_leftovers(ctx)
    assert ctx.runtimes.get(25) is None and world.net.java_downloads == 0, "the launch is checked before Java is downloaded"


async def test_the_website_being_down_is_explained(jvm):
    world, ctx = jvm
    world.gtnh.page_status = 503
    row = await try_create(ctx, "Down")
    assert row["status"] == "failed" and "GT New Horizons website" in row["message"] and "503" in row["message"]
    with pytest.raises(ServerError, match="GT New Horizons website"):
        await ctx.servers.jvm.pack_details("gtnh", "gtnh")


async def test_the_dashboard_is_given_the_pack_and_its_versions(jvm):
    world, ctx = jvm
    project = await ctx.servers.jvm.pack_details("gtnh", "gtnh")
    assert (project["id"], project["title"], project["source"], project["icon_url"]) == ("gtnh", "GT New Horizons", "gtnh", None)
    assert [(v["id"], v["type"]) for v in project["versions"]] == [("2.9.0-RC-2", "beta"), ("2.8.4", "release"), ("2.8.3", "release"), ("2.7.4", "release")]
    assert project["versions"][0]["builds"] == [{"variant": "modern", "name": "Java 17 and newer", "runs_on": 25}, {"variant": "legacy", "name": "Java 8", "runs_on": 8}]
    assert [v["id"] for v in await ctx.servers.jvm.pack_versions("gtnh", "gtnh")] == ["2.9.0-RC-2", "2.8.4", "2.8.3", "2.7.4"]


async def test_a_launcher_that_went_missing_is_explained_and_not_guessed_at(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Gone")
    (ctx.servers.server_dir(rec.id) / "lwjgl3ify-forgePatches.jar").unlink()
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)), timeout=60)
    assert row["status"] == "failed" and "lwjgl3ify-forgePatches.jar is missing from the server folder" in row["message"] and "Restore a backup" in row["message"]


async def test_backups_leave_out_the_copies_serverutilities_made_but_keep_everything_else(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Saved")
    directory = ctx.servers.server_dir(rec.id)
    for path in ("backups/2026-10-13-12-00.zip", "backups/2026-10-13-12-30.zip", "logs/latest.log", "World/region/r.0.0.mca", "mods/backups/keep.txt"):
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_bytes(b"x" * 100)
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"), timeout=120)
    assert row["status"] == "success", row["message"]
    with tarfile.open(ctx.settings.backup_dir / ctx.repo.list_backups(rec.id)[0]["file_name"]) as tar:
        names = tar.getnames()
    assert not any("backups/2026" in n or n.endswith("latest.log") for n in names), names
    assert all(any(n.endswith(path) for n in names) for path in ("World/region/r.0.0.mca", "mods/backups/keep.txt", "mods/gregtech-5.09.jar", LIBRARY, "java9args.txt", FORGE_JAR))


# -------------------------------------------------------------------------------------------------------- updating
async def test_the_update_replaces_the_pack_and_keeps_what_is_the_players(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Update", modpack={"version": "2.8.3"}, start=True)
    directory = ctx.servers.server_dir(rec.id)
    (directory / "World" / "playerdata").mkdir(parents=True, exist_ok=True)
    (directory / "World" / "playerdata" / "steve.dat").write_bytes(b"the world")
    (directory / "mods" / "mine.jar").write_bytes(b"added by hand")
    (directory / "config" / "hodgepodge.cfg").write_text("I:fixes=0\n")  # rewritten by the mod, or changed by a person
    (directory / "config" / "JourneyMapServer" / "world.cfg").write_text("uuid=the maps of the players\n")
    (directory / "server.properties").write_text((directory / "server.properties").read_text() + "motd=mine\n")
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 1
    pending = ctx.servers.view(ctx.servers.get(rec.id))["update"]["pending_pack"]
    assert (pending["number"], pending["type"]) == ("2.8.4", "release"), "the newest stable version, not the release candidate"

    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", row["message"]
    steps = steps_of(ctx, row)
    positions = [next(i for i, s in enumerate(steps) if needle in s) for needle in ("Stopping", "Backup created", "Switching", "Starting")]
    assert positions == sorted(positions), "the backup is made after the server stopped and before anything changed"
    assert any("replaced by the new version's" in s and "config/hodgepodge.cfg" in s for s in steps), "the person is told which files were replaced"
    assert not any("Installing Forge" in s for s in steps)

    assert (directory / "mods" / "gregtech-5.09.jar").read_bytes() == b"gregtech 2.8.4" * 30 and (directory / "lwjgl3ify-forgePatches.jar").read_bytes() == b"patches 2.8.4"
    assert (directory / "mods" / "mine.jar").read_bytes() == b"added by hand" and (directory / "World" / "playerdata" / "steve.dat").read_bytes() == b"the world"
    assert (directory / "config" / "hodgepodge.cfg").read_text() == "I:fixes=1\n", "the pack's own update guide says to replace the configuration"
    assert (directory / "config" / "JourneyMapServer" / "world.cfg").read_text() == "uuid=the maps of the players\n"
    assert "motd=mine" in (directory / "server.properties").read_text(), "server.properties is never replaced"
    assert not (directory / "changelog to 2.8.3.md").exists() and (directory / "changelog to 2.8.4.md").is_file()

    su = directory / "serverutilities" / "serverutilities.cfg"
    assert b"B:enable_backups=false" in su.read_bytes() and not su.with_name(su.name + ".pack-new").exists(), "the manager's own change is not taken for somebody's"
    assert any("Switched off the backups of ServerUtilities again" in s for s in steps)
    lock = ctx.servers.jvm.lock_of(ctx.servers.get(rec.id))
    assert lock["version_id"] == "2.8.4" and lock["overrides"]["serverutilities/serverutilities.cfg"] == hashlib.sha256(su.read_bytes()).hexdigest()

    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "2.8.4" and rec.spec["previous"]["pack"]["version_id"] == "2.8.3" and "pending" not in rec.spec["pack"]
    assert rec.spec["launch"] == {"jar": "lwjgl3ify-forgePatches.jar", "before": ["-Dfml.readTimeout=180", "@java9args.txt"]} and rec.spec["java"] == 25
    update = ctx.repo.list_updates(rec.id)[0]
    assert (update["status"], update["from_tag"], update["to_tag"]) == ("success", "2.8.3", "2.8.4")
    backup = ctx.repo.get_backup(update["backup_id"])
    assert backup["kind"] == "pre-update" and backup["verified"] and backup["build_tag"] == "GT New Horizons 2.8.3"
    assert ctx.pm.runtime(rec.id).state == "running" and (await ctx.pack_updates.check())["servers_with_updates"] == 0

    row = await finish(ctx, ctx.pack_updates.submit_rollback(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "2.8.3" and "previous" not in rec.spec
    assert (directory / "mods" / "gregtech-5.09.jar").read_bytes() == b"gregtech 2.8.3" * 30 and (directory / "config" / "hodgepodge.cfg").read_text() == "I:fixes=0\n"
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_a_serverutilities_setting_that_somebody_changed_stays_when_the_update_brings_a_new_file(jvm):
    world, ctx = jvm
    world.gtnh.add_release("2.8.5", first=True, changes={"serverutilities/serverutilities.cfg": SERVERUTILITIES_CFG + b"# newer\n"})
    rec, _ = await create_gtnh(ctx, "Tuned", modpack={"version": "2.8.4"})
    directory = ctx.servers.server_dir(rec.id)
    su = directory / "serverutilities" / "serverutilities.cfg"
    su.write_bytes(su.read_bytes().replace(b"B:enable_backups=false", b"B:enable_backups=true"))  # the person wants the mod's backups
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", row["message"]
    assert b"B:enable_backups=true" in su.read_bytes() and su.with_name(su.name + ".pack-new").read_bytes().endswith(b"# newer\n")
    assert any("serverutilities/serverutilities.cfg" in s and ".pack-new" in s for s in steps_of(ctx, row))
    assert not any("Switched off the backups" in s for s in steps_of(ctx, row))


async def test_a_version_that_does_not_start_is_rolled_back_automatically(jvm):
    world, ctx = jvm
    world.gtnh.add_release("2.8.5", first=True, changes={"mods/conflict.jar": b"does not get along"})
    rec, _ = await create_gtnh(ctx, "Bad", modpack={"version": "2.8.4"}, start=True)
    directory = ctx.servers.server_dir(rec.id)
    world.behavior["crash_if_present"] = "mods/conflict.jar"
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", "the job itself ends normally: the problem was handled"
    steps = steps_of(ctx, row)
    assert any("Update failed" in s for s in steps) and any("Rolling back" in s for s in steps)
    assert not (directory / "mods" / "conflict.jar").exists() and (directory / "mods" / "gregtech-5.09.jar").read_bytes() == b"gregtech 2.8.4" * 30
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "2.8.4" and "previous" not in rec.spec and ctx.pm.runtime(rec.id).state == "running"
    assert ctx.repo.list_updates(rec.id)[0]["status"] == "rolled_back"
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "a version that failed is not offered again"


async def test_an_update_is_not_rolled_back_because_the_pack_goes_on_loading_after_it_said_done(jvm, monkeypatch):
    """GT New Horizons answers 'Server is still starting!' for half a minute after its Done line (its quest book is loaded again after every update)."""
    world, ctx = jvm
    monkeypatch.setattr(packupdates, "PING_PAUSE", 0.3)
    rec, _ = await create_gtnh(ctx, "Quests", modpack={"version": "2.8.3"}, start=True)
    world.behavior.update({"loading_message": True, "still_starting_after_done": 4.0})  # longer than the watch waits for a silent server
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id)), timeout=180)
    assert row["status"] == "success", row["message"]
    update = ctx.repo.list_updates(rec.id)[0]
    assert update["status"] == "success", update["message"]
    assert ctx.servers.get(rec.id).spec["pack"]["version_id"] == "2.8.4" and ctx.pm.runtime(rec.id).state == "running"


async def test_a_server_on_the_java_8_build_stays_on_it_and_an_update_without_that_build_changes_nothing(jvm):
    world, ctx = jvm
    world.gtnh.add_release("2.8.5", first=True, legacy=False)
    world.gtnh.add_release("2.8.6", first=True)
    rec, _ = await create_gtnh(ctx, "Stays", modpack={"version": "2.8.4", "variant": "legacy"}, start=True)
    await ctx.pack_updates.check()
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id), "2.8.5"), timeout=120)
    update = ctx.repo.list_updates(rec.id)[0]
    assert row["status"] == "success" and update["status"] == "failed" and "has no server files for Java 8" in update["message"]
    assert ctx.servers.get(rec.id).spec["pack"]["version_id"] == "2.8.4" and not any(b["kind"] == "pre-update" for b in ctx.repo.list_backups(rec.id))
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id), "2.8.6"), timeout=180)
    assert row["status"] == "success", row["message"]
    rec = ctx.servers.get(rec.id)
    assert (rec.spec["pack"]["variant"], rec.spec["java"], rec.spec["launch"]["jar"]) == ("legacy", 8, FORGE_JAR)
    assert (ctx.servers.server_dir(rec.id) / "mods" / "gregtech-5.09.jar").read_bytes() == b"gregtech 2.8.6" * 30 and ctx.pm.runtime(rec.id).state == "running"


async def test_an_update_across_more_than_one_major_version_is_allowed_with_a_warning(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Jump", modpack={"version": "2.7.4"})
    row = await finish(ctx, ctx.pack_updates.submit_update(ctx.servers.get(rec.id), "2.9.0-RC-2"), timeout=180)
    assert row["status"] == "success", row["message"]
    assert any("one major version at a time" in s for s in steps_of(ctx, row))
    rec = ctx.servers.get(rec.id)
    assert rec.spec["pack"]["version_id"] == "2.9.0-RC-2" and rec.spec["java"] == 25 and rec.spec["pack"]["java_max"] == 26


# ------------------------------------------------------------------------------------------------- finding updates
async def test_a_stable_server_is_only_offered_stable_versions_and_a_beta_server_the_betas_too(jvm):
    world, ctx = jvm
    stable, _ = await create_gtnh(ctx, "Stable", modpack={"version": "2.8.4"})
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "the release candidate is not for a server on a stable version"
    beta, _ = await create_gtnh(ctx, "Beta", modpack={"version": "2.9.0-RC-2"})
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0
    world.gtnh.add_release("2.9.0-RC-3", beta=True, date="2026/10/20", first=True)
    await ctx.pack_updates.check(ctx.servers.get(beta.id))
    assert ctx.servers.view(ctx.servers.get(beta.id))["update"]["pending_pack"]["number"] == "2.9.0-RC-3"
    assert not ctx.servers.view(ctx.servers.get(stable.id))["update"]["available"]
    world.gtnh.add_release("2.9.0", date="2026/11/01", first=True)
    await ctx.pack_updates.check(ctx.servers.get(stable.id))
    assert ctx.servers.view(ctx.servers.get(stable.id))["update"]["pending_pack"]["number"] == "2.9.0"


async def test_two_versions_published_on_the_same_day_are_told_apart_by_their_place_on_the_page(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Hotfix", modpack={"version": "2.8.4"})
    world.gtnh.add_release("2.8.5", date="2025/12/23", first=True)  # the same day as 2.8.4: a hotfix
    ctx.servers.jvm.gtnh.invalidate()  # an hour has passed since the list was read
    await ctx.pack_updates.check()
    assert ctx.servers.view(ctx.servers.get(rec.id))["update"]["pending_pack"]["number"] == "2.8.5"


async def test_check_now_does_not_answer_from_the_list_read_an_hour_ago(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Fresh", modpack={"version": "2.8.4"})
    await ctx.pack_updates.check()
    world.gtnh.add_release("2.8.5", date="2025/12/24", first=True)
    assert (await ctx.pack_updates.check())["servers_with_updates"] == 0, "the scheduled check uses the list it has"
    assert (await ctx.pack_updates.check(ctx.servers.get(rec.id)))["servers_with_updates"] == 1, "somebody asking gets the page as it is now"


async def test_the_versions_of_a_server_are_listed_with_the_installed_one_marked(jvm):
    world, ctx = jvm
    rec, _ = await create_gtnh(ctx, "Listed", modpack={"version": "2.8.3"})
    available = await ctx.pack_updates.available(ctx.servers.get(rec.id))
    assert available["current"] == "2.8.3" and [v["number"] for v in available["versions"]] == ["2.9.0-RC-2", "2.8.4", "2.8.3", "2.7.4"]


# ----------------------------------------------------------------------- the CurseForge page that only lists the pack
async def reference_listing(world, ctx) -> None:
    """What CurseForge really has: a project 'GT New Horizons' whose file lists a few mods, without GregTech."""
    give_key(ctx, "cf-test-key")
    world.cf.add_project(252507, "GT New Horizons", class_id=4471)
    world.cf.add_pack_version(252507, 7446117, "2.8.4", minecraft="1.7.10", loader="forge-10.13.4.1614", overrides={"README.MD": b"install from the website\n"})


async def test_a_server_cannot_be_made_from_the_curseforge_listing_that_only_lists_the_pack(jvm):
    world, ctx = jvm
    await reference_listing(world, ctx)
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Stub", "engine": "java", "eula": True, "modpack": {"source": "curseforge", "project": "252507"}, "start": False,
    }))
    assert row["status"] == "failed" and "only a list for reference" in row["message"] and "New server → GT New Horizons" in row["message"]
    assert ctx.repo.list_servers() == [] and no_leftovers(ctx)
    with pytest.raises(ServerError, match="only a list for reference"):
        await ctx.servers.jvm.pack_details("252507", "curseforge")
    with pytest.raises(ServerError, match="only a list for reference"):
        await ctx.servers.jvm.pack_details("252507")  # a bare project number is CurseForge's too


async def test_a_server_that_was_made_from_that_listing_says_what_is_wrong(jvm):
    world, ctx = jvm
    give_key(ctx, "cf-test-key")
    world.cf.add_project(1000, "Alpha Pack", class_id=4471)
    world.cf.add_pack_version(1000, 5000, "1.0.0")
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Made before", "engine": "java", "eula": True, "modpack": {"source": "curseforge", "project": "1000"}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    rec = next(s for s in ctx.repo.list_servers() if s.name == "Made before")
    assert ctx.servers.view(rec)["modpack"]["notice"] is None, "other CurseForge packs are fine"
    ctx.repo.update_server(rec.id, spec={**rec.spec, "pack": {**rec.spec["pack"], "project": "252507"}})  # as if it had been made from the listing
    notice = ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["notice"]
    assert notice and "only a list for reference" in notice and "New server → GT New Horizons" in notice
