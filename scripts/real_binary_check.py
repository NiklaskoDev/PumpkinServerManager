"""Exercise the manager against a REAL Pumpkin binary.

Nothing is mocked except the GitHub download: a local file stands in for the release asset, and the manager's
own code creates a server, starts it, talks to it, backs it up, changes its settings, updates it (including a
deliberately broken update that must roll back) and stops it.

    python scripts/real_binary_check.py --download                  # inside the container: fetch + test the official build
    python scripts/real_binary_check.py /path/to/pumpkin            # test a binary you already have
    python scripts/real_binary_check.py C:\\path\\to\\pumpkin.exe     # Windows binary on a dev machine

The servers run on loopback ports with online mode and telemetry off. Everything is created in a temporary
folder that is deleted afterwards (use --keep to inspect it). Needs about 600 MB of free temporary space.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sys
import tempfile
import time
import tomllib
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pumpkin_manager.app import AppContext  # noqa: E402
from pumpkin_manager.config import Settings  # noqa: E402
from pumpkin_manager.mcnet import MsmpClient, MsmpError, server_list_ping  # noqa: E402
from pumpkin_manager.releases import ReleaseError, RemoteBuild  # noqa: E402
from pumpkin_manager.servers import port_is_free  # noqa: E402
from pumpkin_manager.util import strip_ansi  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""), flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n== {title}", flush=True)


class LocalSource:
    """A release source whose 'downloads' are local files."""

    def __init__(self) -> None:
        self.builds: dict[str, tuple[RemoteBuild, Path]] = {}
        self.latest_tag = ""
        self.clock = 0

    def add(self, tag: str, path: Path) -> RemoteBuild:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.clock += 1
        stamp = f"2026-10-{self.clock:02d}T00:00:00Z"
        build = RemoteBuild(
            channel="stable", tag=tag, version=tag, mc_version=None, digest=digest, size=path.stat().st_size,
            asset_name=path.name, asset_id=self.clock, download_url=f"file://{path}", release_url=None,
            published_at=stamp, updated_at=stamp, body=None,
        )
        self.builds[tag] = (build, path)
        self.latest_tag = tag
        return build

    async def latest(self, channel: str) -> RemoteBuild:
        return self.builds[self.latest_tag][0]

    async def list_stable(self, limit: int = 15) -> list[RemoteBuild]:
        return [b for b, _ in self.builds.values()]

    async def by_tag(self, tag: str, channel: str | None = None) -> RemoteBuild:
        if tag not in self.builds:
            raise ReleaseError("unknown tag")
        return self.builds[tag][0]

    async def download(self, build, dest: Path, progress=None) -> None:
        shutil.copyfile(self.builds[build.tag][1], dest)

    async def aclose(self) -> None:
        return None


def free_block(size: int, start: int = 31000) -> int:
    for base in range(start, 60000, size + 7):
        if all(port_is_free(p) for p in range(base, base + size)):
            return base
    raise RuntimeError("no free port block")


async def wait_job(ctx: AppContext, job: dict, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = ctx.repo.get_job(job["id"])
        if row and row["status"] != "running":
            return row
        await asyncio.sleep(0.25)
    raise TimeoutError(f"job {job['title']} did not finish within {timeout}s")


def show_job(row: dict) -> None:
    for step in row["steps"]:
        print(f"        | {step['level']:7} {step['text']}")


def console_tail(ctx: AppContext, server_id: str, since: int = 0, limit: int = 40) -> list[str]:
    return [strip_ansi(line["m"]) for line in ctx.pm.backlog(server_id, 4000) if line["n"] > since][-limit:]


async def eventually(predicate, timeout: float = 15.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


async def ask(ctx: AppContext, server_id: str, command: str, prefix: str, timeout: float = 10.0) -> str | None:
    """Type a command into the server's console and return the first answer line that starts with ``prefix``."""
    await ctx.pm.send_command(server_id, command)
    echo = max(line["n"] for line in ctx.pm.backlog(server_id, 4000) if line["s"] == "in" and line["m"] == command)
    found: list[str] = []

    def answered() -> bool:
        for line in ctx.pm.backlog(server_id, 4000):
            text = strip_ansi(line["m"])
            if line["n"] > echo and text.startswith(prefix):
                found.append(text)
                return True
        return False

    return found[0] if await eventually(answered, timeout, 0.2) else None


async def run(binary: Path, keep: bool) -> int:
    work = Path(tempfile.mkdtemp(prefix="pumpkin-real-check-"))
    print(f"Working folder: {work}")
    started = time.monotonic()
    source = LocalSource()
    source.add("real-1", binary)
    (work / "build2").mkdir()
    second = work / "build2" / binary.name
    second.write_bytes(binary.read_bytes() + b"\0" * 64)  # same program, different digest: a genuine "new release"
    # Two kinds of bad "release". Not an executable at all: on Linux the manager refuses it while downloading,
    # before the running server is touched. And one that passes that download check (it has an ELF header) but
    # cannot actually be started: that one must be rolled back.
    not_exe = work / "build3" / binary.name
    not_exe.parent.mkdir()
    not_exe.write_bytes(b"this is not an executable\n" * 400)
    unrunnable = work / "build4" / binary.name
    unrunnable.parent.mkdir()
    unrunnable.write_bytes(b"\x7fELF" + b"\0" * 4096)

    java = free_block(6)
    mgmt = free_block(20, start=java + 100)
    bedrock = free_block(3, start=mgmt + 100)
    settings = Settings(
        data_dir=work / "data", backup_dir=work / "backups", web_host="127.0.0.1", web_port=0,
        port_range=(java, java + 5), bedrock_port_range=(bedrock, bedrock + 2), mgmt_port_range=(mgmt, mgmt + 19),
        admin_password="real-binary-check", timezone="UTC", shutdown_timeout=60,
    )
    ctx = AppContext(settings, release_source=source, verify_elf=sys.platform.startswith("linux"), background=False, stability_seconds=8)
    await ctx.startup()
    server = None
    try:
        # ------------------------------------------------------------------------------------------ create & start
        section("Create and start a server with the real binary")
        t0 = time.monotonic()
        job = ctx.servers.submit_create({
            "name": "Real Check", "start": True,
            "settings": {"online_mode": False, "view_distance": 4, "simulation_distance": 4, "max_players": 7,
                         "motd": "real binary check", "default_gamemode": "Creative", "default_difficulty": "Hard"},
        })
        row = await wait_job(ctx, job)
        show_job(row)
        if not check(row["status"] == "success", "server created and started", row["message"] or ""):
            print("\n".join(console_tail(ctx, row.get("server_id") or "real-check")))
            return report(work, keep, started)
        server = ctx.repo.list_servers()[0]
        rt = ctx.pm.runtime(server.id)
        check(rt.state == "running", "manager reports the server as running", f"became ready in {time.monotonic() - t0:.1f}s including setup")
        print("        console after start:")
        for line in console_tail(ctx, server.id, limit=14):
            print(f"        > {line}")
        check(any("server is now running" in line.lower() for line in console_tail(ctx, server.id, limit=200)),
              "readiness log line 'Server is now running' is printed")

        # ------------------------------------------------------------------------------------------------ config
        section("pumpkin.toml as seen by the real server")
        cfg_path = settings.servers_dir / server.id / "pumpkin.toml"
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        java_cfg = cfg["networking"]["java"]
        check(java_cfg["address"] == f"0.0.0.0:{server.java_port}", "Java address is the managed port", java_cfg["address"])
        check(java_cfg["max_players"] == 7 and java_cfg["motd"] == "real binary check" and java_cfg["online_mode"] is False,
              "game settings written by the manager were accepted", f"max_players={java_cfg['max_players']} online_mode={java_cfg['online_mode']}")
        check(cfg["default_gamemode"] == "Creative" and cfg["telemetry"]["enabled"] is False, "gamemode and telemetry-off kept")
        msmp_supported = "management" in cfg["networking"]
        check(cfg["networking"]["bedrock"]["enabled"] is False, "Bedrock stays off (would collide on UDP 19132 otherwise)")
        print(f"        management API in this build: {'yes' if msmp_supported else 'NO (older build: the manager falls back to console commands and pings)'}")
        check(len(cfg_path.read_text(encoding="utf-8").splitlines()) > 60, "Pumpkin filled in its defaults (file was rewritten)",
              f"{len(cfg_path.read_text(encoding='utf-8').splitlines())} lines now")
        print(f"        top-level sections: {sorted(k for k, v in cfg.items() if isinstance(v, dict))}")

        # --------------------------------------------------------------------------------------- server list ping
        section("Server list ping")
        try:
            info = await server_list_ping("127.0.0.1", server.java_port, timeout=5)
            check(True, "ping answered", f"version={info['version']!r} protocol={info['protocol']} motd={info['motd']!r} players={info['online']}/{info['max']}")
            check(info["max"] == 7 and "real binary check" in info["motd"], "ping reflects the configured max players and MOTD")
        except Exception as exc:
            check(False, "ping answered", str(exc))

        # --------------------------------------------------------------------------------------- management API
        section("Management API (JSON-RPC over WebSocket)")
        if not msmp_supported:
            print("        SKIPPED: this Pumpkin build does not have the management API")
            try:
                async with MsmpClient(server.mgmt_port, server.mgmt_secret, timeout=3) as client:
                    await client.players()
                check(False, "no management server is listening on a build without it")
            except MsmpError:
                check(True, "the manager copes with the management API being absent")
        else:
            try:
                async with MsmpClient(server.mgmt_port, server.mgmt_secret) as client:
                    status = await client.status()
                    check(status.get("started") is True, "minecraft:server/status", str(status))
                    check(await client.players() == [], "minecraft:players (nobody online)")
                    t = time.monotonic()
                    check(await client.save(flush=True) is True, "minecraft:server/save (flush)", f"took {time.monotonic() - t:.2f}s")
                    check(await client.broadcast("hello from the manager check") is True, "minecraft:server/system_message")
            except MsmpError as exc:
                check(False, "management API reachable with the managed secret", str(exc))
            try:
                async with MsmpClient(server.mgmt_port, "x" * 40) as bad:
                    await bad.players()
                check(False, "a wrong secret is rejected")
            except MsmpError:
                check(True, "a wrong secret is rejected")

        # ------------------------------------------------------------------------------------------ console I/O
        section("Console over stdin")
        mark = max((line["n"] for line in ctx.pm.backlog(server.id, 10)), default=0)
        for command in ("list", "tps", "say console works", "save-all flush", "save-off", "save-on"):
            await ctx.pm.send_command(server.id, command)
            await asyncio.sleep(0.7)
        lines = console_tail(ctx, server.id, since=mark, limit=60)
        for line in lines:
            print(f"        > {line}")
        joined = "\n".join(lines).lower()
        check(any("players" in x or "online" in x for x in lines), "'list' produced output")
        check("saving" in joined or "saved" in joined, "'save-all' produced output")
        check("disabled" in joined or "off" in joined, "'save-off' acknowledged")
        check("enabled" in joined or " on" in joined, "'save-on' acknowledged")

        # ------------------------------------------------------------------------------------------ player lists
        section("Whitelist, operators and bans (the Players tab shows the files the real server writes)")
        rec = ctx.repo.get_server(server.id)
        data_dir = settings.servers_dir / server.id / "data"
        check(sorted(p.name for p in data_dir.glob("*.json")) == ["banned-ips.json", "banned-players.json", "ops.json", "whitelist.json"],
              "Pumpkin keeps its player lists in the data/ folder", ", ".join(sorted(p.name for p in data_dir.glob("*.json"))))

        def listed(key: str, value: str) -> bool:
            return any(value in (e.get("name"), e.get("ip")) for e in ctx.servers.read_player_lists(rec)[key])

        def unlisted(key: str, value: str) -> bool:
            return not listed(key, value)

        for action, target, reason, key in (
            ("whitelist_add", "RealWhite", None, "whitelist"), ("op", "RealOp", None, "ops"),
            ("ban", "RealBan", "checking the lists", "banned_players"), ("ban_ip", "203.0.113.77", "checking the lists", "banned_ips"),
        ):
            await ctx.servers.player_action(rec, action, target, reason)
            check(await eventually(partial(listed, key, target)), f"'{action} {target}' shows up in the {key.replace('_', ' ')} list")
        lists = ctx.servers.read_player_lists(rec)
        check(any(e.get("level") == 1 for e in lists["ops"]), "a new operator gets level 1, the manager's default for new servers", str(lists["ops"]))
        check(any(e.get("reason") == "checking the lists" for e in lists["banned_players"] + lists["banned_ips"]),
              "ban reasons are recorded", str(lists["banned_players"]))
        for action, target, key in (
            ("whitelist_remove", "RealWhite", "whitelist"), ("deop", "RealOp", "ops"),
            ("pardon", "RealBan", "banned_players"), ("pardon_ip", "203.0.113.77", "banned_ips"),
        ):
            await ctx.servers.player_action(rec, action, target)
            check(await eventually(partial(unlisted, key, target)), f"'{action} {target}' takes it off the list again")

        # ------------------------------------------------------------------------------------------ operator levels
        section("Operator levels (Pumpkin reads them only at startup, so changing one restarts the server)")
        pid = ctx.pm.runtime(server.id).pid
        row = await wait_job(ctx, ctx.servers.submit_operator_level(ctx.repo.get_server(server.id), "RealLevel", 3), 120)
        show_job(row)
        check(row["status"] == "success", "a new operator can be given level 3", row["message"] or "")
        rt = ctx.pm.runtime(server.id)
        check(rt.state == "running" and rt.pid != pid, "the server was restarted to apply it", f"pid {pid} -> {rt.pid}")
        levels = {e["name"]: e["level"] for e in ctx.servers.read_player_lists(rec)["ops"]}
        check(levels.get("RealLevel") == 3, "the real server kept level 3 across its shutdown and start", str(levels))
        await ctx.servers.player_action(rec, "op", "RealPlain")
        check(await eventually(lambda: any(e["name"] == "RealPlain" for e in ctx.servers.read_player_lists(rec)["ops"])), "/op still works afterwards")
        levels = {e["name"]: e["level"] for e in ctx.servers.read_player_lists(rec)["ops"]}
        check(levels.get("RealPlain") == 1 and levels.get("RealLevel") == 3, "a plain /op gives the default level 1 and leaves the other operator alone", str(levels))
        for name in ("RealLevel", "RealPlain"):
            await ctx.servers.player_action(rec, "deop", name)
        check(await eventually(lambda: not ctx.servers.read_player_lists(rec)["ops"]), "both operators removed again")

        # ------------------------------------------------------------------------------------------- difficulty
        section("Difficulty (Pumpkin ignores default_difficulty in pumpkin.toml, so the manager sets it with a command)")
        check(cfg["default_difficulty"] == "Hard", "pumpkin.toml says Hard, as chosen when the server was created")
        answer = await ask(ctx, server.id, "difficulty", "The difficulty is")
        check(answer == "The difficulty is Hard", "the real server is on Hard (left alone it would be on Normal)", answer or "no answer")
        result = await ctx.servers.save_settings(ctx.repo.get_server(server.id), {"default_difficulty": "Easy"})
        check(result["applied_live"] == ["default_difficulty"] and not result["restart_required"],
              "changing it in the dashboard needs no restart", str(result))
        answer = await ask(ctx, server.id, "difficulty", "The difficulty is")
        check(answer == "The difficulty is Easy", "the running server switched to Easy", answer or "no answer")

        # ----------------------------------------------------------------------------------------------- backup
        section("Hot backup of the running server")
        row = await wait_job(ctx, ctx.backups.submit_backup(ctx.repo.get_server(server.id), "manual", "real binary"))
        show_job(row)
        backups = ctx.repo.list_backups(server.id)
        check(row["status"] == "success" and len(backups) == 1, "backup created", row["message"] or "")
        if backups:
            b = backups[0]
            check(bool(b["verified"]) and bool(b["consistent"]), "backup verified and consistent", f"{b['size']} bytes")
        world = settings.servers_dir / server.id / "world"
        check(world.is_dir() and any(world.iterdir()), "a world folder exists", ", ".join(sorted(p.name for p in world.iterdir())[:8]) if world.is_dir() else "")

        # --------------------------------------------------------------------------- settings change + restart
        section("Change a setting, restart, confirm the real server picked it up")
        ctx.servers.update_config(ctx.repo.get_server(server.id), {"max_players": 11, "motd": "second start"})
        row = await wait_job(ctx, ctx.servers.submit_restart(ctx.repo.get_server(server.id)))
        show_job(row)
        check(row["status"] == "success", "restart succeeded", row["message"] or "")
        info = await server_list_ping("127.0.0.1", server.java_port, timeout=5)
        check(info["max"] == 11 and "second start" in info["motd"], "new settings are live", f"{info['online']}/{info['max']} {info['motd']!r}")
        answer = await ask(ctx, server.id, "difficulty", "The difficulty is")
        check(answer == "The difficulty is Easy", "the difficulty was saved with the world and survived the restart", answer or "no answer")
        sent = [line["m"] for line in ctx.pm.backlog(server.id, 4000) if line["s"] == "in" and line["m"].startswith("difficulty ")]
        check(sent == ["difficulty hard", "difficulty easy"], "the manager sets the difficulty only when it changes, not on every start", str(sent))
        check(ctx.pm.runtime(server.id).exit_code == 0 or ctx.pm.runtime(server.id).exit_code is None, "the previous process exited cleanly", f"code {ctx.pm.runtime(server.id).exit_code}")

        # ------------------------------------------------------------------------------------- real update flow
        section("Update to a new build (backup, switch, restart, stability watch)")
        source.add("real-2", second)
        await ctx.updater.check()
        pending = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
        check(pending is not None and pending.tag == "real-2", "update detected by digest")
        row = await wait_job(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), pending, trigger="manual"), 240)
        show_job(row)
        update = ctx.repo.list_updates(server.id)[0]
        check(update["status"] == "success" and ctx.repo.get_server(server.id).build_id == pending.id, "update applied", update["message"] or "")
        check(any(b["kind"] == "pre-update" and b["verified"] for b in ctx.repo.list_backups(server.id)), "a verified pre-update backup exists")
        check(ctx.pm.runtime(server.id).state == "running", "server is running on the new build")

        section("Update to a download that is not an executable at all")
        source.add("real-3-not-executable", not_exe)
        await ctx.updater.check()
        junk = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
        before_build = ctx.repo.get_server(server.id).build_id
        pid_before = ctx.pm.runtime(server.id).pid
        backups_before = len(ctx.repo.list_backups(server.id))
        row = await wait_job(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), junk, trigger="auto"), 240)
        show_job(row)
        update = ctx.repo.list_updates(server.id)[0]
        if ctx.builds.verify_elf:
            # Linux: the download is inspected and refused before anything is stopped or changed.
            check(update["status"] == "failed" and "executable" in (update["message"] or ""), "refused while downloading", update["message"] or "")
            check(ctx.pm.runtime(server.id).pid == pid_before, "the running server was not touched (same process)")
            check(len(ctx.repo.list_backups(server.id)) == backups_before, "no pointless backup was made")
            check(ctx.repo.get_server(server.id).pending_build_id is None and ctx.repo.get_kv(f"skip:{server.id}") == junk.id,
                  "the unusable build is not offered or retried again")
        else:
            # No ELF check on this platform (a Windows development machine): it fails when starting and is rolled back.
            check(update["status"] == "rolled_back", "update recorded as rolled back", update["message"] or "")
        check(ctx.repo.get_server(server.id).build_id == before_build, "the previous build is still active")
        check(ctx.pm.runtime(server.id).state == "running", "the server is still running")

        section("Update to a build that passes the download check but cannot start: must roll back")
        source.add("real-4-unrunnable", unrunnable)
        await ctx.updater.check()
        bad = ctx.repo.get_build(ctx.repo.get_server(server.id).pending_build_id)
        check(bad is not None and bad.tag == "real-4-unrunnable", "the newer build is offered even though the earlier bad one is skipped")
        before_build = ctx.repo.get_server(server.id).build_id
        row = await wait_job(ctx, ctx.updater.submit_update(ctx.repo.get_server(server.id), bad, trigger="auto"), 240)
        show_job(row)
        update = ctx.repo.list_updates(server.id)[0]
        check(update["status"] == "rolled_back", "update recorded as rolled back", update["message"] or "")
        check(ctx.repo.get_server(server.id).build_id == before_build, "previous build is active again")
        check(ctx.pm.runtime(server.id).state == "running", "server is running again after the rollback")
        info = await server_list_ping("127.0.0.1", server.java_port, timeout=5)
        check(info["max"] == 11, "settings survived the rollback", f"{info['online']}/{info['max']}")

        # -------------------------------------------------------------------------------- several servers at once
        section("Three real servers side by side (one with Bedrock enabled)")
        source.latest_tag = "real-2"  # the broken release was "withdrawn"; new servers must get a working build
        extra_ids = []
        for name, kwargs in (("Real Two", {}), ("Real Bedrock", {"bedrock": True}), ("Real Bedrock Two", {"bedrock": True})):
            row = await wait_job(ctx, ctx.servers.submit_create({
                "name": name, "start": True, "settings": {"online_mode": False, "view_distance": 4, "motd": name.lower()}, **kwargs,
            }), 120)
            ok = check(row["status"] == "success", f"'{name}' created and started", row["message"] or "")
            if not ok:
                show_job(row)
                print("\n".join(console_tail(ctx, name.lower().replace(" ", "-"), limit=20)))
            extra_ids.append(name.lower().replace(" ", "-"))
        recs = {r.name: r for r in ctx.repo.list_servers()}
        ports = [(r.name, r.java_port, r.bedrock_port) for r in recs.values()]
        print(f"        ports: {ports}")
        check(len({r.java_port for r in recs.values()} | {r.mgmt_port for r in recs.values()}) == 8, "every server got distinct TCP ports")
        for name, rec in recs.items():
            running = ctx.pm.runtime(rec.id).state == "running"
            try:
                info = await server_list_ping("127.0.0.1", rec.java_port, timeout=5)
                check(running and (name == "Real Check" or info["motd"] == name.lower()), f"{name} answers on its own port", f"motd={info['motd']!r}")
            except Exception as exc:
                check(False, f"{name} answers on its own port", str(exc))
        bedrock_rec = recs["Real Bedrock"]
        check(bedrock_rec.bedrock_port is not None and not port_is_free(bedrock_rec.bedrock_port, udp=True),
              "the Bedrock server holds its UDP port", f"udp/{bedrock_rec.bedrock_port}")
        brcfg = tomllib.loads((settings.servers_dir / bedrock_rec.id / "pumpkin.toml").read_text(encoding="utf-8"))["networking"]["bedrock"]
        check(brcfg["enabled"] is True and brcfg["nethernet"]["address"].endswith(str(bedrock_rec.bedrock_port)),
              "Bedrock is enabled with the assigned port in pumpkin.toml", brcfg["nethernet"]["address"])
        print("        Bedrock server console:")
        for line in console_tail(ctx, bedrock_rec.id, limit=12):
            print(f"        > {line}")
        second_bedrock = recs["Real Bedrock Two"]
        print("        second Bedrock server console (watch for port conflicts):")
        for line in console_tail(ctx, second_bedrock.id, limit=14):
            print(f"        > {line}")
        check(ctx.pm.runtime(second_bedrock.id).state == "running", "a second Bedrock server runs next to the first",
              f"udp/{second_bedrock.bedrock_port} (first uses udp/{bedrock_rec.bedrock_port}, +1 for IPv6 status)")
        for name in ("Real Two", "Real Bedrock", "Real Bedrock Two"):
            rec = recs[name]
            await wait_job(ctx, ctx.servers.submit_stop(ctx.repo.get_server(rec.id)))
            check(ctx.pm.runtime(rec.id).exit_code == 0, f"{name} stopped cleanly", f"exit code {ctx.pm.runtime(rec.id).exit_code}")
        check(port_is_free(bedrock_rec.bedrock_port, udp=True), "the Bedrock UDP port is released after the stop")

        # ------------------------------------------------------------------------------------- graceful stop
        section("Graceful stop")
        t = time.monotonic()
        row = await wait_job(ctx, ctx.servers.submit_stop(ctx.repo.get_server(server.id)))
        rt = ctx.pm.runtime(server.id)
        tail = "\n".join(console_tail(ctx, server.id, limit=15)).lower()
        check(row["status"] == "success" and rt.state == "stopped", "stop job succeeded", f"took {time.monotonic() - t:.1f}s")
        check(rt.exit_code == 0, "exit code is 0 (clean shutdown, not killed)", f"exit code {rt.exit_code}")
        check("stopped" in tail or "completed save" in tail or "starting save" in tail, "shutdown messages were logged")
        for line in console_tail(ctx, server.id, limit=8):
            print(f"        > {line}")

        section("Start again: the world persisted")
        row = await wait_job(ctx, ctx.servers.submit_start(ctx.repo.get_server(server.id)))
        check(row["status"] == "success", "second start", row["message"] or "")
        await wait_job(ctx, ctx.servers.submit_stop(ctx.repo.get_server(server.id)))
        check(ctx.pm.runtime(server.id).exit_code == 0, "second shutdown is clean too")
    except Exception as exc:  # report everything, then clean up
        import traceback

        traceback.print_exc()
        check(False, "script ran to completion", repr(exc))
    finally:
        await ctx.shutdown()
        if server is not None:
            leftover = ctx.pm.runtime(server.id)
            check(leftover.proc is None, "no process left running after manager shutdown")
    return report(work, keep, started)


def report(work: Path, keep: bool, started: float) -> int:
    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed in {time.monotonic() - started:.0f}s")
    for _, name, detail in failed:
        print(f"  FAILED: {name} {detail}")
    if keep:
        print(f"Kept {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


async def fetch_latest_stable(destination: Path) -> Path:
    """Download the newest stable Pumpkin build for this machine (verified against GitHub's SHA-256)."""
    from pumpkin_manager.releases import GitHubReleaseSource

    source = GitHubReleaseSource("Pumpkin-MC/Pumpkin")
    try:
        build = await source.latest("stable")
        print(f"Downloading Pumpkin {build.tag} ({build.size // (1024 * 1024)} MiB) ...", flush=True)
        await source.download(build, destination)
        destination.chmod(0o755)
        print(f"Verified SHA-256 {build.digest[:16]}... against GitHub", flush=True)
    finally:
        await source.aclose()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("binary", nargs="?", type=Path, help="path to a Pumpkin executable")
    parser.add_argument("--download", action="store_true", help="download the latest stable Pumpkin for this machine instead of using a local file")
    parser.add_argument("--keep", action="store_true", help="do not delete the temporary folder")
    args = parser.parse_args()
    if args.download:
        scratch = Path(tempfile.mkdtemp(prefix="pumpkin-real-download-"))
        try:
            binary = asyncio.run(fetch_latest_stable(scratch / "pumpkin"))
            return asyncio.run(run(binary.resolve(), args.keep))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if args.binary is None or not args.binary.is_file():
        print("Give the path of a Pumpkin executable, or use --download.", file=sys.stderr)
        return 2
    return asyncio.run(run(args.binary.resolve(), args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
