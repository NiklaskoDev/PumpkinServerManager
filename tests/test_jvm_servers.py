"""Modpack servers end to end: create, start, configure, players, backups, and what must not happen to them."""

from __future__ import annotations

import json
import time

import pytest
from conftest import create_java_server, finish, wait_until
from jvmfakes import FakeMod
from jvmworld import CREATE, LITHIUM

from pumpkin_manager.jvm import properties as props
from pumpkin_manager.jvm.service import JavaServerService
from pumpkin_manager.pumpkin_config import ConfigError


def props_of(ctx, rec) -> dict[str, str]:
    return props.parse((ctx.servers.server_dir(rec.id) / "server.properties").read_text(encoding="utf-8"))


def started_with(ctx, rec) -> dict:
    return json.loads((ctx.servers.server_dir(rec.id) / "fake-java.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------- create
async def test_a_modpack_server_is_created_installed_and_started(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Modded", memory_mb=3072, settings={"motd": "Hello", "max-players": 8})
    assert rec.engine == "java" and rec.build_id is None and rec.is_java

    spec = rec.spec
    assert (spec["kind"], spec["minecraft"], spec["java"], spec["memory_mb"]) == ("modpack", "1.21.1", 21, 3072)
    assert spec["loader"] == {"kind": "neoforge", "version": "21.1.77"}
    assert spec["pack"]["title"] == "Cool Pack" and spec["pack"]["version_number"] == "1.0.0" and spec["pack"]["source"] == "modrinth"
    assert spec["eula_accepted_at"] and spec["launch"]["argfile"].startswith("libraries/net/neoforged/neoforge/21.1.77/")

    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM.content and (directory / "mods" / "create.jar").read_bytes() == CREATE.content
    assert not (directory / "mods" / "jei.jar").exists(), "client-only mods stay out"
    assert (directory / "config" / "create.toml").exists()
    assert not list(ctx.settings.servers_dir.glob(".create-*")), "the staging folder is gone"

    values = props_of(ctx, rec)
    assert values["server-port"] == str(rec.java_port) and values["motd"] == "Hello" and values["max-players"] == "8"
    assert values["allow-flight"] == "true" and values["view-distance"] == "12", "the pack's own server.properties is the base"
    assert values["op-permission-level"] == "1", "operators start at level 1 here too"
    assert "eula=true" in (directory / "eula.txt").read_text()
    assert rec.policy.update_mode == "notify" and rec.policy.start_timeout >= 600 and "logs" in rec.policy.backup_exclude

    assert ctx.pm.runtime(rec.id).state == "running"
    ran = started_with(ctx, rec)
    argv = ran["argv"]
    assert argv[0] == str(ctx.runtimes.executable(21)) and ran["java_home"] == str(ctx.runtimes.home(21))
    assert "-Xms3072M" in argv and "-Xmx3072M" in argv and argv[-1] == "nogui"
    assert any(a.startswith("@libraries/net/neoforged/neoforge/21.1.77/") for a in argv)
    assert "@user_jvm_args.txt" in argv and argv.index("@user_jvm_args.txt") < argv.index("-Xmx3072M"), "the pack's options come first so the memory wins"

    view = ctx.servers.view(rec)
    assert view["engine"] == "java" and view["build"] is None and view["ports"]["bedrock"] is None
    assert view["modpack"]["title"] == "Cool Pack" and view["modpack"]["loader"]["label"] == "NeoForge 21.1.77"
    assert view["java"]["major"] == 21 and view["java"]["memory_mb"] == 3072 and view["update"]["available"] is False
    assert any("Installing" in step["text"] for step in ctx.repo.get_job(ctx.repo.list_jobs()[0]["id"])["steps"])


async def test_the_minecraft_eula_must_be_accepted(jvm):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "No EULA", "engine": "java", "eula": False, "modpack": {"source": "modrinth", "project": "cool-pack"}}))
    assert row["status"] == "failed" and "EULA" in row["message"]
    assert ctx.repo.list_servers() == [] and world.net.java_downloads == 0, "nothing was downloaded for a server that cannot be created"


async def test_a_failed_installation_leaves_nothing_behind(jvm):
    world, ctx = jvm
    world.installer.exit_code = 1
    world.installer.output = ("Downloading libraries...", "ERROR: no route to host")
    row = await finish(ctx, ctx.servers.submit_create({"name": "Broken", "engine": "java", "eula": True, "modpack": {"source": "modrinth", "project": "cool-pack"}}))
    assert row["status"] == "failed" and "no route to host" in row["message"]
    assert ctx.repo.list_servers() == [] and list(ctx.settings.servers_dir.iterdir()) == []
    assert list(ctx.settings.cache_dir.joinpath("uploads").iterdir()) == [], "the downloaded pack file is removed too"


@pytest.mark.parametrize("modpack,message", [
    ({"source": "modrinth", "project": "no-such-pack"}, "does not know"),
    ({"source": "modrinth", "project": "../../etc"}, "short name"),
    ({"source": "modrinth", "project": "cool-pack", "version": "NoSuchVer"}, "does not exist"),
    ({"source": "upload", "upload": "0" * 32}, "Upload the modpack file again"),
    ({"source": "ftp"}, "Choose a modpack"),
    ({}, "Choose a modpack"),
])
async def test_a_modpack_that_cannot_be_found_is_explained(jvm, modpack, message):
    world, ctx = jvm
    row = await finish(ctx, ctx.servers.submit_create({"name": "Nope", "engine": "java", "eula": True, "modpack": modpack}))
    assert row["status"] == "failed" and message in row["message"]
    assert ctx.repo.list_servers() == []


async def test_a_modpack_file_can_be_uploaded_instead(jvm):
    world, ctx = jvm
    pack = world.pack.packs[world.pack.versions[0]["id"]]

    async def stream(data: bytes):
        for start in range(0, len(data), 1000):  # arrives in pieces, like a request body
            yield data[start:start + 1000]

    upload = await ctx.servers.jvm.accept_upload(stream(pack))
    assert upload["pack"]["name"] == "Cool Pack" and upload["pack"]["mods"] == 2
    row = await finish(ctx, ctx.servers.submit_create({"name": "Uploaded", "engine": "java", "eula": True, "modpack": {"source": "upload", "upload": upload["upload"]}, "start": False}))
    assert row["status"] == "success", row["message"]
    rec = next(s for s in ctx.repo.list_servers() if s.name == "Uploaded")
    assert {k: v for k, v in rec.spec["pack"].items() if k not in ("client_only", "client_only_count", "manual", "manual_count")} == {
        "source": "upload", "title": "Cool Pack", "version_number": "1.0.0",
    }
    assert list((ctx.settings.cache_dir / "uploads").iterdir()) == [], "a used upload is removed"
    with pytest.raises(Exception, match="not a Modrinth modpack"):
        await ctx.servers.jvm.accept_upload(stream(b"not a modpack"))
    assert list((ctx.settings.cache_dir / "uploads").iterdir()) == [], "a refused upload leaves nothing"


async def test_the_server_name_and_the_settings_are_checked_before_anything_is_downloaded(jvm):
    world, ctx = jvm
    await create_java_server(ctx, "Taken", start=False)
    downloads = world.net.java_downloads
    for body, message in (
        ({"name": "taken"}, "already exists"),
        ({"name": "Memory", "memory_mb": 10}, "memory_mb"),
        ({"name": "Args", "jvm_args": "-jar evil.jar"}, "not allowed"),
        ({"name": "Settings", "settings": {"max-players": 0}}, "max-players"),
        ({"name": "Policy", "policy": {"stop_timeout": 1}}, "Invalid policy"),
    ):
        row = await finish(ctx, ctx.servers.submit_create({"engine": "java", "eula": True, "modpack": {"source": "modrinth", "project": "cool-pack"}, **body}))
        assert row["status"] == "failed" and message in row["message"], (body, row["message"])
    assert world.net.java_downloads == downloads and len(ctx.repo.list_servers()) == 1


async def test_two_servers_share_one_java_and_one_copy_of_each_mod(jvm):
    world, ctx = jvm
    await create_java_server(ctx, "First", start=False)
    await create_java_server(ctx, "Second", start=False)
    assert world.net.java_downloads == 1 and all(count == 1 for count in world.net.mod_downloads.values())
    assert len({r.java_port for r in ctx.repo.list_servers()}) == 2
    assert [j["major"] for j in ctx.servers.jvm.runtime_overview()] == [21]
    assert len(ctx.servers.jvm.runtime_overview()[0]["used_by"]) == 2


# ----------------------------------------------------------------------------- settings
async def test_settings_are_read_and_saved_in_server_properties(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Cfg", start=False)
    config = ctx.servers.read_config(rec)
    assert config["values"]["allow-flight"] is True and config["values"]["difficulty"] == "easy" and config["exists"]
    assert {f["key"] for f in config["schema"]} >= {"motd", "difficulty", "max-players"}

    result = await ctx.servers.save_settings(rec, {"difficulty": "hard", "motd": "§bNew"})
    assert result["saved"] and result["restart_required"] is False and result["applied_live"] == []
    assert props_of(ctx, rec)["difficulty"] == "hard" and props_of(ctx, rec)["motd"] == "§bNew"
    assert (ctx.servers.server_dir(rec.id) / "server.properties.manager-bak").exists(), "the previous file is kept"
    with pytest.raises(ConfigError):
        await ctx.servers.save_settings(rec, {"difficulty": "extreme"})
    assert props_of(ctx, rec)["difficulty"] == "hard"


async def test_the_port_belongs_to_the_manager_even_in_the_raw_file(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Raw", start=False)
    raw = ctx.servers.read_raw_config(rec)
    assert raw["managed_keys"] == ["server-port", "enable-rcon"] and "server-port" in raw["content"]
    result = ctx.servers.write_raw_config(rec, raw["content"].replace(f"server-port={rec.java_port}", "server-port=1234") + "\nspawn-protection=0\n")
    assert result["reset_managed_keys"] == ["server-port"]
    assert props_of(ctx, rec)["server-port"] == str(rec.java_port) and props_of(ctx, rec)["spawn-protection"] == "0"


async def test_memory_and_jvm_options_can_be_changed_and_are_checked(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Tune", start=False)
    rec, restart = ctx.servers.patch(rec, {"java": {"memory_mb": 6144, "jvm_args": "-Xss2M -Dfoo=bar"}})
    assert rec.spec["memory_mb"] == 6144 and rec.spec["jvm_args"] == "-Xss2M -Dfoo=bar" and restart is False
    argv = ctx.servers.jvm.build_argv(rec, ctx.runtimes.executable(21), ctx.servers.server_dir(rec.id))
    assert "-Xmx6144M" in argv and "-Xss2M" in argv and "-Dfoo=bar" in argv and argv.index("-Xss2M") < argv.index("nogui")
    for bad in ("-jar x.jar", "evil", "-cp /tmp/x", "a\nb", "-Dfoo='unterminated"):
        with pytest.raises(ConfigError):
            JavaServerService.parse_jvm_args(bad)
    assert JavaServerService.parse_jvm_args("-Dtitle='two words'") == "'-Dtitle=two words'"
    # Pumpkin-only changes do not apply to a modpack server
    rec, _ = ctx.servers.patch(rec, {"channel": "nightly", "bedrock": True, "pinned_tag": "x"})
    assert rec.channel == "" and rec.bedrock_port is None and rec.pinned_tag is None


# ------------------------------------------------------------------------ client-only mods
async def test_mods_that_modrinth_says_only_work_on_a_client_are_left_out(jvm):
    """Packs exported from a client launcher list everything as 'required' on the server; the mod's own project knows better."""
    world, ctx = jvm
    world.net.mod_sides((CREATE, "unsupported"), (LITHIUM, "required"))
    rec = await create_java_server(ctx, "ClientMods", start=False)
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and not (directory / "mods" / "create.jar").exists()
    lock = ctx.servers.jvm.lock_of(rec)
    assert lock["client_only"] == ["mods/create.jar"] and "mods/create.jar" not in lock["files"]
    modpack = ctx.servers.view(ctx.servers.get(rec.id))["modpack"]
    assert modpack["client_only"] == ["mods/create.jar"] and modpack["client_only_count"] == 1 and modpack["leave_out_client_only"] is True
    assert world.net.mod_downloads.get(CREATE.url, 0) == 0, "a mod that is left out is not even downloaded"


async def test_a_client_only_mod_that_another_mod_needs_stays(jvm):
    """Battle Armory: tp_shooting needs Shoulder Surfing, which Modrinth lists as client-only; without it the server cannot load."""
    world, ctx = jvm
    world.net.mod_sides((LITHIUM, "required"), (CREATE, "unsupported"))
    world.net.mod_requires(LITHIUM, CREATE)
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Needed", "engine": "java", "eula": True, "modpack": {"source": "modrinth", "project": "cool-pack"}, "start": False,
    }))
    assert row["status"] == "success", row["message"]
    rec = next(s for s in ctx.repo.list_servers() if s.name == "Needed")
    assert (ctx.servers.server_dir(rec.id) / "mods" / "create.jar").is_file()
    assert ctx.servers.view(rec)["modpack"]["client_only_count"] == 0
    assert any("Keeping 1 mod that only works on a client" in s["text"] for s in ctx.repo.get_job(row["id"])["steps"])


async def test_what_a_kept_client_only_mod_needs_stays_too_and_the_rest_is_left_out(jvm):
    world, ctx = jvm
    jade = FakeMod("mods/jade.jar", b"jade" * 80)
    library = FakeMod("mods/library.jar", b"library" * 80)
    minimap = FakeMod("mods/minimap.jar", b"minimap" * 80)
    version = world.pack.add_version("2.0.0", mods=[LITHIUM, CREATE, jade, library, minimap])
    world.net.mod_sides((LITHIUM, "required"), (CREATE, "unsupported"), (jade, "unsupported"), (library, "unsupported"), (minimap, "unsupported"))
    world.net.mod_requires(LITHIUM, CREATE)  # lithium needs create, which needs jade, which needs the library: all of them stay
    world.net.mod_requires(CREATE, jade)
    world.net.mod_requires(jade, library)
    rec = await create_java_server(ctx, "Chain", start=False, modpack={"source": "modrinth", "project": "cool-pack", "version": version["id"]})
    mods = {p.name for p in (ctx.servers.server_dir(rec.id) / "mods").iterdir()}
    assert mods == {"lithium.jar", "create.jar", "jade.jar", "library.jar"}, "the minimap is the only one nobody needs"
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["client_only"] == ["mods/minimap.jar"]


async def test_client_only_mods_that_a_pack_ships_inside_its_overrides_are_left_out_too(jvm):
    """Zombie Invade carries sixteen mod jars in overrides/ (CIT Resewn among them) instead of listing them."""
    world, ctx = jvm
    shaders = FakeMod("mods/shader-helper.jar", b"shader helper" * 50)
    kept = FakeMod("mods/extra-library.jar", b"extra library" * 50)
    version = world.pack.add_version(
        "3.0.0", mods=[LITHIUM], overrides={"mods/shader-helper.jar": shaders.content, "mods/extra-library.jar": kept.content, "config/a.toml": b"a=1\n"},
        server_overrides={"mods/server-only.jar": b"x" * 40},
    )
    world.net.mod_sides((shaders, "unsupported"), (kept, "optional"))  # the jar in server-overrides is not known to Modrinth at all
    rec = await create_java_server(ctx, "Shipped", start=False, modpack={"source": "modrinth", "project": "cool-pack", "version": version["id"]})
    directory = ctx.servers.server_dir(rec.id)
    assert not (directory / "mods" / "shader-helper.jar").exists()
    assert (directory / "mods" / "extra-library.jar").is_file() and (directory / "mods" / "server-only.jar").is_file() and (directory / "config" / "a.toml").is_file()
    lock = ctx.servers.jvm.lock_of(ctx.servers.get(rec.id))
    assert lock["client_only"] == ["mods/shader-helper.jar"] and "mods/shader-helper.jar" not in lock["overrides"]


async def test_mods_modrinth_does_not_know_are_installed_as_the_pack_says(jvm):
    world, ctx = jvm
    world.net.mod_sides((LITHIUM, "optional"))  # create.jar is unknown to Modrinth here: it stays
    rec = await create_java_server(ctx, "Unknown", start=False)
    directory = ctx.servers.server_dir(rec.id)
    assert (directory / "mods" / "lithium.jar").is_file() and (directory / "mods" / "create.jar").is_file()
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["client_only_count"] == 0


async def test_leaving_client_only_mods_out_can_be_switched_off(jvm):
    world, ctx = jvm
    world.net.mod_sides((CREATE, "unsupported"))
    rec = await create_java_server(ctx, "AllMods", start=False, leave_out_client_only=False)
    assert (ctx.servers.server_dir(rec.id) / "mods" / "create.jar").is_file()
    assert rec.spec["leave_out_client_only"] is False
    assert ctx.servers.view(ctx.servers.get(rec.id))["modpack"]["leave_out_client_only"] is False


async def test_a_modrinth_outage_stops_the_creation_with_a_clear_message_and_leaves_nothing_behind(jvm):
    world, ctx = jvm
    world.net.lookup_status = 503
    row = await finish(ctx, ctx.servers.submit_create({
        "name": "Outage", "engine": "java", "eula": True, "modpack": {"source": "modrinth", "project": "cool-pack"}, "start": False,
    }))
    assert row["status"] == "failed" and "which mods are client-only" in row["message"]
    assert ctx.repo.list_servers() == [] and not list(ctx.settings.servers_dir.glob(".create-*"))


# ------------------------------------------------------------------------------ running
async def test_the_eula_file_is_written_again_if_it_went_missing(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Eula", start=False)
    (ctx.servers.server_dir(rec.id) / "eula.txt").unlink()
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "success", row["message"]
    assert "eula=true" in (ctx.servers.server_dir(rec.id) / "eula.txt").read_text()


async def test_a_deleted_java_runtime_is_downloaded_again_when_the_server_starts(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Java", start=False)
    ctx.runtimes.remove(21)
    assert world.net.java_downloads == 1
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "success", row["message"]
    assert world.net.java_downloads == 2 and ctx.runtimes.get(21) is not None


async def test_the_launch_files_are_found_again_after_a_restore(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Launch", start=False)
    stored = rec.spec["launch"]["argfile"]
    spec = {**rec.spec, "launch": {"argfile": "libraries/gone/unix_args.txt"}}
    ctx.repo.update_server(rec.id, spec=spec)
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]
    assert ctx.servers.get(rec.id).spec["launch"]["argfile"] == stored
    # and when nothing can be found there is an explanation instead of a crash
    await finish(ctx, ctx.servers.submit_stop(ctx.servers.get(rec.id)))
    for path in (ctx.servers.server_dir(rec.id) / "libraries").rglob("*_args.txt"):
        path.unlink()
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "failed" and "mod loader are missing" in row["message"]


async def test_a_server_that_crashes_while_starting_reports_what_it_said(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Crash", start=False)
    world.behavior["crash_on_start"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "failed" and "simulated crash" in row["message"] and ctx.pm.runtime(rec.id).state == "crashed"


async def test_a_server_that_ends_during_startup_says_why_and_not_just_what_it_printed_last(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Trace", start=False)
    world.behavior["trace_crash"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "failed" and "exited during startup (exit code 1)" in row["message"]
    assert "FileNotFoundException: https://repo.example/lib-1.1.jar" in row["message"], "the root cause of the stack trace"
    assert "ExitTrappedException" not in row["message"] and "HttpURLConnection" not in row["message"] and "Runtime.exit" not in row["message"]


async def test_the_known_old_forge_on_new_java_failure_is_explained(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "OldForge", start=False)
    world.behavior["old_forge_on_new_java"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "failed" and "ManifestEntryVerifier" in row["message"]
    assert "old Forge builds for Minecraft 1.16.5" in row["message"] and "36.2.26 or newer" in row["message"]


async def test_a_server_that_answers_with_its_real_status_before_it_says_done_is_still_not_called_up(jvm):
    """NeoForge with hundreds of mods did: an update was judged by a server that was still loading."""
    world, ctx = jvm
    rec = await create_java_server(ctx, "AnswersEarly", start=False)
    world.behavior["early_full_status"] = 8.0
    started = time.monotonic()
    row = await finish(ctx, ctx.servers.submit_start(rec), timeout=60)
    assert row["status"] == "success", row["message"]
    assert any("Done (" in line["m"] for line in ctx.pm.backlog(rec.id, 200)), "it was only called up once the console said so"
    assert time.monotonic() - started >= 7, "the first answer (after 5 seconds) was not taken for 'up'"
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_a_server_that_answers_but_never_says_done_is_called_up_after_a_while(jvm, monkeypatch):
    from pumpkin_manager import process

    monkeypatch.setattr(process, "ANSWER_GRACE", 1.0)
    world, ctx = jvm
    rec = await create_java_server(ctx, "Quiet", start=False)
    world.behavior["answers_without_done"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec), timeout=60)
    assert row["status"] == "success", row["message"]
    assert not any("Done (" in line["m"] for line in ctx.pm.backlog(rec.id, 200))
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_a_server_that_opens_its_port_early_is_not_called_up_before_it_says_done(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "EarlyPort", start=False)
    world.behavior["early_empty_status"] = 2.0  # Forge listens long before the world is loaded and answers pings with an empty status
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "success", row["message"]
    lines = [line["m"] for line in ctx.pm.backlog(rec.id, 200)]
    assert any("Done (" in line for line in lines), "the start must wait for the 'Done' line, not for the first answer on the port"
    assert ctx.pm.runtime(rec.id).state == "running"
    assert sum("still starting" in line for line in lines) <= 2, "the console is not filled with readiness probes while the server loads"


async def test_a_server_that_answers_pings_with_a_message_while_it_loads_is_waited_for(jvm):
    """Forge 1.7.10 does: the answer is the JSON string "Server is still starting!...", and reading it as a status used to fail the start."""
    world, ctx = jvm
    rec = await create_java_server(ctx, "Message", start=False)
    world.behavior["early_empty_status"] = 2.0
    world.behavior["loading_message"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "success", row["message"]
    assert any("Done (" in line["m"] for line in ctx.pm.backlog(rec.id, 200))
    assert ctx.pm.runtime(rec.id).state == "running"


@pytest.mark.parametrize("how", ["exit", "hang"])
async def test_a_server_that_says_it_cannot_start_is_stopped_at_once_and_the_reason_is_shown(jvm, monkeypatch, how):
    """Forge keeps the JVM alive after 'Failed to start the minecraft server'; waiting for the startup timeout helps nobody."""
    from pumpkin_manager import process

    monkeypatch.setattr(process, "FAILURE_EXIT_GRACE", 1.0)
    world, ctx = jvm
    rec = await create_java_server(ctx, "Fatal", start=False)
    world.behavior["fatal_start"] = how
    row = await finish(ctx, ctx.servers.submit_start(rec), timeout=60)
    assert row["status"] == "failed" and "could not start" in row["message"]
    assert "CIT Resewn (citresewn) has failed to load correctly" in row["message"] and "NoClassDefFoundError" in row["message"]
    assert "§" not in row["message"] and "gatherAndInitializeMods" not in row["message"], "no colour codes, no stack frames"
    assert ctx.pm.runtime(rec.id).proc is None and ctx.pm.runtime(rec.id).state == "crashed"


async def test_a_pack_that_ends_its_first_start_on_purpose_is_started_once_more_by_itself(jvm):
    """Some packs (Better MC) download their mods at the first start and then close the server on purpose."""
    world, ctx = jvm
    world.behavior["exit_clean_once"] = True
    rec = await create_java_server(ctx, "SelfClosing", start=False)
    row = await finish(ctx, ctx.servers.submit_start(rec))
    assert row["status"] == "success", row["message"]
    assert any("Starting it once more" in s["text"] for s in ctx.repo.get_job(row["id"])["steps"])
    assert ctx.pm.runtime(rec.id).state == "running"


async def test_a_server_that_keeps_ending_by_itself_is_tried_twice_and_then_explained(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "NeverStays", start=False)
    world.behavior["exit_clean_on_start"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec), timeout=60)
    assert row["status"] == "failed" and "exit code 0" in row["message"] and "starting the server again" in row["message"]
    assert "Installed 3 missing mods" in row["message"], "what the server said last is shown too"
    assert any("Starting it once more" in s["text"] for s in ctx.repo.get_job(row["id"])["steps"]), "it did try a second time"
    world.behavior.pop("exit_clean_on_start")
    row = await finish(ctx, ctx.servers.submit_start(ctx.servers.get(rec.id)))
    assert row["status"] == "success", row["message"]


async def test_a_server_that_never_says_done_is_stopped_with_a_hint_about_slow_modpacks(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Slow", start=False, policy={"start_timeout": 15})  # 15 seconds is the shortest allowed
    world.behavior["no_done_line"] = True
    row = await finish(ctx, ctx.servers.submit_start(rec), timeout=60)
    assert row["status"] == "failed" and "did not become ready" in row["message"] and "several minutes" in row["message"]


# ------------------------------------------------------------------------------ players
async def test_player_lists_are_read_from_the_server_folder(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Lists")
    await ctx.servers.player_action(rec, "whitelist_add", "Alex")
    await ctx.servers.player_action(rec, "ban", "Grief", "no griefing")
    await ctx.servers.player_action(rec, "op", "Boss")
    await ctx.servers.player_action(rec, "ban_ip", "203.0.113.9")
    def filled():
        found = ctx.servers.read_player_lists(rec)
        return found if all(found.values()) else None

    lists = await wait_until(filled, what="the lists")
    assert [e["name"] for e in lists["whitelist"]] == ["Alex"] and lists["banned_players"][0]["reason"] == "no griefing"
    assert lists["ops"][0]["name"] == "Boss" and lists["ops"][0]["level"] == 1 and lists["ops"][0]["bypasses_player_limit"] is False
    assert lists["banned_ips"][0]["ip"] == "203.0.113.9"
    assert (ctx.servers.server_dir(rec.id) / "ops.json").exists() and not (ctx.servers.server_dir(rec.id) / "data").exists()
    assert ctx.servers.op_default_level(rec) == 1


async def test_an_operators_level_can_be_changed_through_a_restart(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Ops")
    # On a running server the manager makes the player an operator first, then restarts to apply a level other than the default.
    row = await finish(ctx, ctx.servers.submit_operator_level(rec, "Boss", 3), timeout=60)
    assert row["status"] == "success", row["message"]
    assert ctx.servers.read_player_lists(rec)["ops"][0]["level"] == 3 and ctx.pm.runtime(rec.id).state == "running"
    row = await finish(ctx, ctx.servers.submit_operator_level(rec, "Boss", 2), timeout=60)
    assert row["status"] == "success" and ctx.servers.read_player_lists(rec)["ops"][0]["level"] == 2
    # A stopped server cannot look a new player up, so it only changes players that already are operators.
    await finish(ctx, ctx.servers.submit_stop(rec))
    row = await finish(ctx, ctx.servers.submit_operator_level(rec, "Newcomer", 2))
    assert row["status"] == "failed" and "not an operator yet" in row["message"]


# ------------------------------------------------------------- backups, updates, deleting
async def test_backups_skip_logs_and_crash_reports_but_keep_mods_and_the_world(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Backup")
    directory = ctx.servers.server_dir(rec.id)
    (directory / "logs").mkdir(exist_ok=True)
    (directory / "logs" / "latest.log").write_text("noise")
    (directory / "crash-reports").mkdir(exist_ok=True)
    (directory / "crash-reports" / "crash.txt").write_text("noise")
    (directory / "world").mkdir(exist_ok=True)
    (directory / "world" / "session.lock").write_text("held by Java")  # on Windows a running server's copy cannot even be read
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
    assert row["status"] == "success", row["message"]
    backup = ctx.repo.list_backups(rec.id)[0]
    assert backup["verified"] and backup["consistent"], "a hot backup works on a Java server through the vanilla console commands"
    import tarfile

    with tarfile.open(ctx.settings.backup_dir / backup["file_name"]) as tar:
        names = tar.getnames()
    assert any(n.endswith("mods/lithium.jar") for n in names) and any(n.endswith("world/level.dat") for n in names)
    assert not any("latest.log" in n or "crash.txt" in n for n in names)
    assert not any(n.endswith("session.lock") for n in names), "the lock file is recreated at every start and never belongs in a backup"


async def test_a_hot_backup_of_a_minecraft_1_7_server_is_consistent_and_does_not_wait_for_a_message_it_never_prints(jvm):
    """Minecraft 1.7 says "Turned off world auto-saving" and "Saved the world"; waiting for the modern wording cost 30 seconds and the flag."""
    world, ctx = jvm
    world.behavior["old_save_messages"] = True
    rec = await create_java_server(ctx, "Old")
    started = time.monotonic()
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
    assert row["status"] == "success", row["message"]
    assert ctx.repo.list_backups(rec.id)[0]["consistent"], "the server did confirm: just in its own words"
    assert time.monotonic() - started < 25, "it did not sit out the 30 seconds for a confirmation that was already there"
    assert "did not confirm" not in (row["message"] or "")


async def test_modpack_servers_are_not_offered_pumpkin_updates(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "NoPumpkin", start=False)
    ctx.release_source.publish("stable", "9.9.9")  # a newer Pumpkin build exists; a modpack server must not be flagged for it
    result = await ctx.updater.check()
    assert result["servers_with_updates"] == 0
    after = ctx.repo.get_server(rec.id)
    assert after.pending_build_id is None and ctx.servers.view(after)["update"]["available"] is False


async def test_deleting_a_modpack_server_removes_its_folder_and_leftover_installs(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Gone")
    (ctx.settings.servers_dir / ".create-leftover").mkdir()
    row = await finish(ctx, ctx.servers.submit_delete(rec, final_backup=False, delete_backups=True))
    assert row["status"] == "success", row["message"]
    assert not ctx.servers.server_dir(rec.id).exists() and ctx.repo.list_servers() == []
    ctx.servers.reconcile()
    assert not (ctx.settings.servers_dir / ".create-leftover").exists(), "an install that was interrupted is cleaned up at the next start"


async def test_a_hot_backup_waits_until_the_server_says_the_world_is_saved(jvm):
    world, ctx = jvm
    world.behavior["slow_save"] = 2.0  # a big modded world takes a while to flush
    rec = await create_java_server(ctx, "SlowSave")
    began = time.monotonic()
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
    assert row["status"] == "success", row["message"]
    assert time.monotonic() - began >= 2.0, "the archive is only started after the server confirmed the save"
    assert ctx.repo.list_backups(rec.id)[0]["consistent"]
    steps = [s["text"] for s in ctx.repo.get_job(row["id"])["steps"]]
    assert any("Saving the world" in text for text in steps) and not any("did not confirm" in text for text in steps)


async def test_a_backup_says_so_when_the_server_never_confirms_the_save(jvm):
    world, ctx = jvm
    world.behavior["no_save_confirmation"] = True
    ctx.backups.java_save_timeout = 1.0
    rec = await create_java_server(ctx, "NoConfirm")
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
    assert row["status"] == "success", row["message"]
    backup = ctx.repo.list_backups(rec.id)[0]
    assert backup["verified"] and not backup["consistent"], "it is still a good archive, but only crash-consistent"
    assert any("did not confirm within 1 seconds" in s["text"] for s in ctx.repo.get_job(row["id"])["steps"])
    assert ctx.pm.runtime(rec.id).state == "running", "autosave is switched back on and the server keeps running"


async def test_restoring_a_backup_brings_back_the_mods_and_keeps_the_managers_port(jvm):
    world, ctx = jvm
    rec = await create_java_server(ctx, "Restore")
    directory = ctx.servers.server_dir(rec.id)
    row = await finish(ctx, ctx.backups.submit_backup(rec, "manual"))
    assert row["status"] == "success", row["message"]
    backup = ctx.repo.list_backups(rec.id)[0]

    (directory / "mods" / "lithium.jar").write_bytes(b"broken by a bad mod update")
    (directory / "config" / "create.toml").write_text("speed = 99\n")
    ctx.servers.write_raw_config(rec, "server-port=1234\nmotd=Changed\n")

    job = ctx.backups.submit_restore(ctx.servers.get(rec.id), backup["id"], True, lambda r: ctx.servers.ensure_config(r))
    row = await finish(ctx, job, timeout=120)
    assert row["status"] == "success", row["message"]
    assert (directory / "mods" / "lithium.jar").read_bytes() == LITHIUM.content
    assert (directory / "config" / "create.toml").read_text() == "speed = 1\n"
    assert props_of(ctx, rec)["server-port"] == str(rec.java_port), "the port is the manager's, whatever the backup or the file said"
    assert ctx.pm.runtime(rec.id).state == "running", "a server that was running is started again"
    assert {b["kind"] for b in ctx.repo.list_backups(rec.id)} == {"manual", "pre-restore"}, "the state before the restore is kept as a safety backup"
