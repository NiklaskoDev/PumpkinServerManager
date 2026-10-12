"""Run the real dashboard locally against a *fake* GitHub feed and a *fake* Pumpkin binary.

For UI development only: no network access, no downloads, nothing leaves this machine. State is kept in
.devdata/ so it survives restarts. Log in with admin / devpassword.

    python scripts/dev_server.py            # http://127.0.0.1:8420
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from fakes import FAKE, FakeReleaseSource  # noqa: E402
from jvmfakes import FakeMod, FakeModrinth  # noqa: E402
from jvmworld import JavaWorld  # noqa: E402

from pumpkin_manager.app import create_app  # noqa: E402
from pumpkin_manager.config import Settings  # noqa: E402


def main() -> None:
    data = ROOT / ".devdata"
    port = int(os.environ.get("WEB_PORT", "8420"))
    settings = Settings(
        data_dir=data / "data", backup_dir=data / "backups", web_host="127.0.0.1", web_port=port,
        port_range=(25570, 25580), bedrock_port_range=(19140, 19145), mgmt_port_range=(29100, 29199),
        admin_password="devpassword", update_check_minutes=5, cookie_secure="never", timezone="Europe/Berlin",
    )
    source = FakeReleaseSource()
    source.publish("stable", "0.1.0")
    source.publish("nightly", "nightly-1")
    source.publish("canary", "canary-1")

    # Modpack servers: a fake Modrinth, Adoptium and loader sites, and a fake Java server (nothing is downloaded for real).
    world = JavaWorld()
    world.net.maven_installer("forge", "1.20.1", "47.2.0")
    jei = FakeMod("mods/jei.jar", b"jei" * 400)
    world.net.mod_sides((jei, "unsupported"))  # the pack lists it for the server, but Modrinth says it only works on a client
    packs: dict[str, FakeModrinth] = {}
    for slug, title, version, minecraft, loader, mods in (
        ("skyblock-dreams", "Skyblock Dreams", "2.4.1", "1.21.1", ("fabric", "0.16.9"), [FakeMod("mods/fabric-api.jar", b"fabric-api" * 500), FakeMod("mods/lithium.jar", b"lithium" * 200)]),
        ("old-school-adventures", "Old School Adventures", "7.0", "1.20.1", ("forge", "47.2.0"), [jei, FakeMod("mods/waystones.jar", b"waystones" * 100)]),
    ):
        packs[slug] = FakeModrinth(world.net, slug=slug, title=title, project_id=slug.replace("-", "")[:8].ljust(8, "x"))
        packs[slug].add_version(version, minecraft=minecraft, loader=loader, mods=mods)

    # A fake CurseForge too. Its API key is "cf-test-key": enter it in Settings > Modpacks to see CurseForge packs (search is not allowed
    # for this key, like for many real ones, so packs are found in the list or by project number: 2000 and 2001).
    cf = world.cf
    cf.search_enabled = False
    create_mod = FakeMod("mods/create.jar", b"create" * 400)
    cf.add_mod(5001, 9001, create_mod, name="Create")
    cf.add_mod(5002, 9002, FakeMod("mods/xaeros-minimap.jar", b"minimap" * 100), name="Xaero's Minimap", client_only=True)
    cf.add_mod(5003, 9003, FakeMod("mods/secret-sauce.jar", b"secret" * 100), name="Secret Sauce", downloadable=False)
    cf.add_project(2001, "Kitchen Sink", class_id=4471)
    cf.add_pack_version(
        2001, 9500, "3.2.0", mods=((5001, 9001), (5002, 9002), (5003, 9003)), minecraft="1.20.1", loader="forge-47.2.0",
        overrides={"config/kitchen.toml": b"sink = true\n"},
    )
    cf.add_project(2000, "Cobble Adventures", class_id=4471)
    cobble_files = {"mods/create.jar": create_mod.content, "mods/cobble.jar": b"cobble" * 200, "config/cobble.toml": b"spawns = 12\n", "start.sh": b"#!/bin/sh\n"}
    cf.add_pack_version(2000, 9600, "1.0.0", server_pack=cobble_files, wrapper="Server-Files-1.0.0", date="2026-09-01T12:00:00.000Z")
    app = create_app(settings, release_source=source, verify_elf=False, stability_seconds=3, **world.app_options())
    ctx = app.state.ctx
    ctx.servers.command_builder = lambda rec, build: [sys.executable, "-u", str(FAKE), str(ctx.builds.binary_path(build.id))]
    world.attach(ctx)

    def publish_update() -> None:  # a newer stable release "appears" so the update flow is visible
        source.publish("stable", "0.2.0")
        print("[dev] published fake release 0.2.0 - the next update check will find it", flush=True)
        packs["skyblock-dreams"].add_version(
            "2.5.0", minecraft="1.21.1", loader=("fabric", "0.16.9"), mods=[FakeMod("mods/fabric-api.jar", b"fabric-api" * 500), FakeMod("mods/sodium-server.jar", b"sodium" * 300)],
            overrides={"config/skyblock.toml": b"islands = 4\n"},
        )
        print("[dev] published fake modpack version Skyblock Dreams 2.5.0", flush=True)
        cf.add_pack_version(
            2000, 9700, "1.1.0", server_pack={**cobble_files, "config/cobble.toml": b"spawns = 20\n", "mods/waystones.jar": b"waystones" * 100},
            wrapper="Server-Files-1.1.0", date="2026-09-20T12:00:00.000Z",
        )
        print("[dev] published fake CurseForge version Cobble Adventures 1.1.0", flush=True)

    threading.Timer(float(os.environ.get("DEV_UPDATE_AFTER", "90")), publish_update).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
