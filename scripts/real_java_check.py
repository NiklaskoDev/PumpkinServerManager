"""Exercise modpack servers against the REAL thing: Adoptium Java, Mojang, Modrinth and the real mod loaders.

Nothing is mocked. For each mod loader the manager's own code builds a small modpack from live data (Fabric: Fabric API and
Lithium from Modrinth; the others: an empty pack, which still exercises the loader's installer), creates a server from it,
downloads Java and the Minecraft server, starts it, pings it, types console commands, takes a hot backup, restores it and
stops it.

    python scripts/real_java_check.py --accept-eula                       # Fabric (the default)
    python scripts/real_java_check.py --accept-eula --loader all          # Fabric, NeoForge, Forge and Quilt
    python scripts/real_java_check.py --accept-eula --loader neoforge --memory 3072

Run it inside the container (Unraid: container icon -> Console) to test the real Linux/Alpine setup, or on a dev machine.

**By passing --accept-eula you accept the Minecraft EULA (https://aka.ms/MinecraftEULA)** for the throw-away servers this
check creates in a temporary folder; without the flag nothing is started.

The servers listen on loopback ports with online mode off. Everything is created in a temporary folder that is deleted
afterwards (use --keep to inspect it). Needs internet access and roughly 1-2 GB of temporary space per loader (Java, the
Minecraft server, the loader's libraries and the world); the first start of a loader downloads a few hundred MB.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_binary_check import check, console_tail, eventually, free_block, report, section, show_job, wait_job  # noqa: E402

from pumpkin_manager.app import AppContext  # noqa: E402
from pumpkin_manager.config import Settings  # noqa: E402
from pumpkin_manager.jvm.net import USER_AGENT  # noqa: E402
from pumpkin_manager.mcnet import server_list_ping  # noqa: E402
from pumpkin_manager.util import strip_ansi  # noqa: E402

LOADERS = ("fabric", "neoforge", "forge", "quilt")
DEFAULT_MINECRAFT = {"fabric": None, "neoforge": "1.21.1", "forge": "1.20.1", "quilt": "1.21.1"}  # None: the newest release
MODS = {"fabric": ("fabric-api", "lithium")}


# --------------------------------------------------------------------------------- what is out there right now
async def get_json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    resp = await client.get(url, params=params or None)
    resp.raise_for_status()
    return resp.json()


async def pick_versions(client: httpx.AsyncClient, loader: str, minecraft: str | None) -> tuple[str, str]:
    """(Minecraft version, loader version) to test: the newest the loader's own site offers for it."""
    if minecraft is None:
        minecraft = (await get_json(client, "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"))["latest"]["release"]
    if loader == "fabric":
        found = await get_json(client, f"https://meta.fabricmc.net/v2/versions/loader/{minecraft}")
        return minecraft, next((e["loader"]["version"] for e in found if e["loader"]["stable"]), found[0]["loader"]["version"])
    if loader == "quilt":
        found = await get_json(client, "https://meta.quiltmc.org/v3/versions/loader")
        return minecraft, next(e["version"] for e in found if "beta" not in e["version"] and "alpha" not in e["version"])
    if loader == "forge":
        promos = (await get_json(client, "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"))["promos"]
        return minecraft, promos.get(f"{minecraft}-recommended") or promos[f"{minecraft}-latest"]
    parts = minecraft.split(".")  # NeoForge numbers its versions 21.1.x for Minecraft 1.21.1
    prefix = f"{parts[1]}.{parts[2] if len(parts) > 2 else 0}."
    data = await get_json(client, "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
    candidates = [v for v in data["versions"] if v.startswith(prefix) and not any(tag in v for tag in ("beta", "alpha"))]
    if not candidates:
        raise RuntimeError(f"NeoForge has no stable version for Minecraft {minecraft}")
    return minecraft, candidates[-1]


async def newest_mod_file(client: httpx.AsyncClient, slug: str, loader: str, minecraft: str) -> dict[str, Any] | None:
    versions = await get_json(
        client, f"https://api.modrinth.com/v2/project/{slug}/version", loaders=json.dumps([loader]), game_versions=json.dumps([minecraft])
    )
    for version in versions:
        if version.get("version_type") == "release":
            return next((f for f in version["files"] if f.get("primary")), version["files"][0])
    return None


async def build_pack(client: httpx.AsyncClient, loader: str, minecraft: str, loader_version: str) -> tuple[bytes, list[str]]:
    """A real .mrpack for ``loader``: the mods are real Modrinth files with their real checksums."""
    files, names = [], []
    for slug in MODS.get(loader, ()):
        found = await newest_mod_file(client, slug, loader, minecraft)
        if found is None:
            print(f"        (no {slug} for Minecraft {minecraft} yet; the pack goes without it)")
            continue
        files.append({
            "path": f"mods/{found['filename']}", "hashes": {"sha1": found["hashes"]["sha1"], "sha512": found["hashes"]["sha512"]},
            "env": {"client": "required", "server": "required"}, "downloads": [found["url"]], "fileSize": found["size"],
        })
        names.append(found["filename"])
    dependency = {"fabric": "fabric-loader", "quilt": "quilt-loader"}.get(loader, loader)
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": "check-1", "name": f"Real check ({loader})", "files": files,
        "dependencies": {"minecraft": minecraft, dependency: loader_version},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        zf.writestr("overrides/config/real-check.txt", "written by the pack\n")
        zf.writestr("server-overrides/server.properties", "allow-flight=true\nspawn-protection=0\n")
    return buf.getvalue(), names


async def chunks(data: bytes):
    for start in range(0, len(data), 1 << 20):
        yield data[start:start + (1 << 20)]


def said(ctx: AppContext, server_id: str, text: str, since: int = 0) -> bool:
    return any(text in strip_ansi(line["m"]) for line in ctx.pm.backlog(server_id, 4000) if line["n"] > since)


# ---------------------------------------------------------------------------------------------------- one loader
async def check_loader(ctx: AppContext, client: httpx.AsyncClient, loader: str, minecraft: str | None, memory: int) -> None:
    section(f"{loader}: look up versions and build a modpack from live data")
    minecraft, loader_version = await pick_versions(client, loader, minecraft)
    pack, mods = await build_pack(client, loader, minecraft, loader_version)
    check(True, "versions chosen", f"Minecraft {minecraft}, {loader} {loader_version}, mods: {', '.join(mods) or 'none'}")
    upload = await ctx.servers.jvm.accept_upload(chunks(pack))
    check(upload["pack"]["loader"]["kind"] == loader, "the manager reads the pack", f"{upload['pack']['mods']} mods, {upload['pack']['loader']['label']}")

    section(f"{loader}: create the server (Java, mods, loader, first start)")
    started = time.monotonic()
    job = ctx.servers.submit_create({
        "name": f"Real {loader}", "engine": "java", "eula": True, "memory_mb": memory, "start": True,
        "modpack": {"source": "upload", "upload": upload["upload"]},
        "settings": {"online-mode": False, "view-distance": 4, "simulation-distance": 4, "max-players": 5, "motd": "real java check"},
    })
    row = await wait_job(ctx, job, timeout=1800)
    show_job(row)
    server = next((s for s in ctx.repo.list_servers() if s.name == f"Real {loader}"), None)
    if not check(row["status"] == "success" and server is not None, "server created and started", (row["message"] or "") + f" ({time.monotonic() - started:.0f}s)"):
        if server:
            print("\n".join(console_tail(ctx, server.id, limit=60)))
        return
    directory = ctx.servers.server_dir(server.id)
    rt = ctx.pm.runtime(server.id)
    print("        console after start:")
    for line in console_tail(ctx, server.id, limit=12):
        print(f"        > {line}")
    check(rt.state == "running", "the manager reports the server as running")
    check(said(ctx, server.id, "Done ("), "the server printed its 'Done (...)!' line")
    check(ctx.runtimes.get(server.spec["java"]) is not None, f"Java {server.spec['java']} was downloaded and works", str(ctx.runtimes.get(server.spec["java"]).release))
    check(all((directory / "mods" / name).is_file() for name in mods), "the mods are in mods/", ", ".join(mods) or "(none)")
    check((directory / "config" / "real-check.txt").is_file(), "the pack's configuration was applied")
    props = (directory / "server.properties").read_text(encoding="utf-8")
    check(f"server-port={server.java_port}" in props and "allow-flight=true" in props and "online-mode=false" in props,
          "server.properties has the manager's port, the pack's setting and the chosen settings")
    check((directory / "eula.txt").read_text(encoding="utf-8").count("eula=true") == 1, "eula.txt was written")

    section(f"{loader}: talk to the running server")
    info = None
    try:
        info = await server_list_ping("127.0.0.1", server.java_port, timeout=10)
    except Exception as exc:  # noqa: BLE001
        check(False, "the server answers the server-list ping", repr(exc))
    if info:
        check(True, "the server answers the server-list ping", f"{info['version']!r}, {info['online']}/{info['max']} players, motd {info['motd']!r}")
        check(info["max"] == 5 and "real java check" in info["motd"], "max players and motd are the ones chosen")
    mark = max((line["n"] for line in ctx.pm.backlog(server.id, 4000)), default=0)
    await ctx.pm.send_command(server.id, "list")
    check(await eventually(lambda: said(ctx, server.id, "players online", mark), 15), "the console answers 'list'")
    await ctx.pm.send_command(server.id, "whitelist add Notch")
    await eventually(lambda: bool(ctx.servers.read_player_lists(server)["whitelist"]), 20)
    check(any(e.get("name") for e in ctx.servers.read_player_lists(server)["whitelist"]), "whitelist add works and the list shows in the server folder")

    section(f"{loader}: hot backup and restore")
    row = await wait_job(ctx, ctx.backups.submit_backup(server, "manual"), timeout=600)
    show_job(row)
    backup = ctx.repo.list_backups(server.id)[0] if ctx.repo.list_backups(server.id) else None
    check(row["status"] == "success" and backup is not None and bool(backup["verified"]), "the hot backup was made and verified", row["message"] or "")
    check(backup is not None and bool(backup["consistent"]), "the server confirmed that the world was saved (consistent backup)")
    (directory / "config" / "real-check.txt").write_text("damaged\n", encoding="utf-8")
    row = await wait_job(ctx, ctx.backups.submit_restore(ctx.servers.get(server.id), backup["id"], True, lambda r: ctx.servers.ensure_config(r)), timeout=900)
    check(row["status"] == "success" and (directory / "config" / "real-check.txt").read_text(encoding="utf-8") == "written by the pack\n",
          "restoring the backup brings the files back", row["message"] or "")
    check(ctx.pm.runtime(server.id).state == "running", "the server runs again after the restore")

    section(f"{loader}: stop")
    row = await wait_job(ctx, ctx.servers.submit_stop(ctx.servers.get(server.id)), timeout=300)
    check(row["status"] == "success" and ctx.pm.runtime(server.id).state == "stopped", "the server stops gracefully", f"exit code {ctx.pm.runtime(server.id).exit_code}")
    check(ctx.pm.runtime(server.id).exit_code == 0, "its exit code is 0")
    check((directory / "world").is_dir() or any(p.is_dir() for p in directory.glob("*/region")), "a world was saved")


async def run(loaders: list[str], minecraft: str | None, memory: int, keep: bool) -> int:
    work = Path(tempfile.mkdtemp(prefix="java-real-check-"))
    print(f"Working folder: {work}")
    started = time.monotonic()
    java = free_block(4)
    mgmt = free_block(20, start=java + 100)
    settings = Settings(
        data_dir=work / "data", backup_dir=work / "backups", web_host="127.0.0.1", web_port=0, port_range=(java, java + 3),
        mgmt_port_range=(mgmt, mgmt + 19), admin_password="real-java-check", timezone="UTC", shutdown_timeout=120,
    )
    ctx = AppContext(settings, verify_elf=False, background=False, stability_seconds=8)
    await ctx.startup()
    client = httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        for loader in loaders:
            try:
                await check_loader(ctx, client, loader, minecraft or DEFAULT_MINECRAFT[loader], memory)
            except Exception as exc:  # noqa: BLE001 - report everything, then go on with the next loader
                import traceback

                traceback.print_exc()
                check(False, f"{loader}: the check ran to completion", repr(exc))
    finally:
        await client.aclose()
        await ctx.shutdown()
        for server in ctx.repo.list_servers():
            check(ctx.pm.runtime(server.id).proc is None, f"no process left running ({server.name})")
    return report(work, keep, started)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accept-eula", action="store_true", help="accept the Minecraft EULA for the throw-away servers (required)")
    parser.add_argument("--loader", default="fabric", choices=[*LOADERS, "all"], help="which mod loader to test (default: fabric)")
    parser.add_argument("--minecraft", help="Minecraft version (default: the newest release for Fabric, a well supported one for the others)")
    parser.add_argument("--memory", type=int, default=2048, help="memory per server in MiB (default 2048)")
    parser.add_argument("--keep", action="store_true", help="do not delete the temporary folder")
    args = parser.parse_args()
    if not args.accept_eula:
        print("This check starts real Minecraft servers. Add --accept-eula to confirm that you accept the Minecraft EULA\n"
              "(https://aka.ms/MinecraftEULA) for them.", file=sys.stderr)
        return 2
    loaders = list(LOADERS) if args.loader == "all" else [args.loader]
    return asyncio.run(run(loaders, args.minecraft, args.memory, args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
