"""A complete fake outside world for Java servers, shared by the integration tests and the local dev dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jvmfakes import (
    FakeBungee,
    FakeCurseForge,
    FakeFabric,
    FakeFabricApi,
    FakeGtnh,
    FakeInternet,
    FakeJava,
    FakeMod,
    FakeModrinth,
    FakePaper,
    FakeSpigot,
)

FAKE_JAVA = Path(__file__).parent / "fake_java_server.py"
LITHIUM = FakeMod("mods/lithium.jar", b"lithium" * 100)
CREATE = FakeMod("mods/create.jar", b"create" * 300)
JEI = FakeMod("mods/jei.jar", b"jei" * 50, server="unsupported")


class JavaWorld:
    """Adoptium, Mojang, PaperMC, Fabric, Modrinth and the loaders' download sites, plus a fake ``java -jar <installer>`` and a fake
    Java server."""

    def __init__(self) -> None:
        self.net = FakeInternet()
        self.net.adoptium(21)
        self.net.adoptium(17, release="jdk-17.0.20.1+1")
        self.net.adoptium(8, release="jdk8u312-b07")
        self.net.minecraft({"1.21.4": 21, "1.21.1": 21, "1.20.4": 17, "1.20.1": 17, "1.16.5": 8}, snapshots={"1.21.5-snapshot-1": 21})
        self.net.maven_installer("neoforge", "1.21.1", "21.1.77")
        self.net.fabric("1.21.1", "0.16.9", "1.0.1")
        # Servers without a modpack: PaperMC and Fabric offer the same Minecraft versions, and Fabric API exists for some of them
        self.paper = FakePaper(self.net)
        for version, java in (("1.21.4", 21), ("1.21.1", 21), ("1.20.4", 17), ("1.16.5", 8)):
            self.paper.add_version(version, java=java, support="SUPPORTED" if version == "1.21.4" else "UNSUPPORTED", end=None if version == "1.21.4" else "2025-11-14")
        self.paper.add_version("1.21.5-rc1", java=21)  # release candidates are listed by the real service as well
        self.paper.add_build("1.21.5-rc1", 1, "ALPHA")
        for version, builds in (("1.21.4", (10, 11)), ("1.21.1", (100, 101)), ("1.20.4", (5,)), ("1.16.5", (1, 2))):
            for build in builds:
                self.paper.add_build(version, build, "STABLE")
        self.paper.add_build("1.21.4", 12, "ALPHA")  # the newest build is experimental, as it often is
        self.fabric = FakeFabric(self.net)
        for version in ("1.21.4", "1.21.1", "1.20.4", "1.20.1"):
            self.fabric.add_game(version)
        self.fabric.add_game("1.21.5-snapshot-1", stable=False)
        for loader, stable in (("0.16.9", True), ("0.16.10", True), ("0.17.0-beta.1", False)):
            self.fabric.add_loader(loader, stable)
        self.fabric_api = FakeFabricApi(self.net)
        self.fabric_api.add_version("0.105.0+1.21.1", "1.21.1")
        self.fabric_api.add_version("0.106.0+1.21.1", "1.21.1")
        self.fabric_api.add_version("0.110.0+1.21.4", "1.21.4")
        self.cf = FakeCurseForge(self.net)  # unused unless a test adds CurseForge packs (and gives the app a key)
        # GT New Horizons: its website lists the versions (newest first), its download site serves the zips of complete servers
        self.net.adoptium(25, release="jdk-25.0.4.1+1")
        self.gtnh = FakeGtnh(self.net)
        for version, beta, date, java_max in (
            ("2.9.0-RC-2", True, "2026/10/04", 26), ("2.8.4", False, "2025/12/23", 25), ("2.8.3", False, "2025/12/12", 25), ("2.7.4", False, "2025/04/13", 21),
        ):
            self.gtnh.add_release(version, beta=beta, date=date, java_max=java_max)
        # Spigot is built with a JDK (a runtime with a compiler) by SpigotMC's BuildTools; BungeeCord is a download of SpigotMC's build server
        for major, release in ((21, "jdk-21.0.12.1+1"), (17, "jdk-17.0.20.1+1"), (25, "jdk-25.0.4.1+1"), (8, "jdk8u312-b07")):
            self.net.adoptium(major, release=release, image="jdk")
        self.spigot = FakeSpigot(self.net)
        for version, name, java in (("1.21.4", "4458", (65, 68)), ("1.21.1", "4205", (65, 66)), ("1.20.4", "4023", (61, 65)), ("1.16.5", "3096a", (52, 60))):
            self.spigot.add_version(version, name, java=java)
        self.bungee = FakeBungee(self.net)
        for number, result in ((2105, "UNSTABLE"), (2104, "SUCCESS"), (2103, "SUCCESS"), (2102, "FAILURE"), (2101, "SUCCESS"), (2100, "SUCCESS")):
            self.bungee.add_build(number, result)
        self.installer = FakeJava()
        self.behavior: dict[str, Any] = {"minecraft": "1.21.1"}  # what the fake Java server does (see fake_java_server.py)
        self.pack = FakeModrinth(self.net, slug="cool-pack", title="Cool Pack", project_id="AbCdEf12")
        self.pack.add_version(
            "1.0.0", mods=[LITHIUM, CREATE, JEI], overrides={"config/create.toml": b"speed = 1\n"},
            server_overrides={"server.properties": b"allow-flight=true\nview-distance=12\n"},
        )

    def app_options(self) -> dict[str, Any]:
        """Keyword arguments that make an ``AppContext`` / ``create_app`` use this world instead of the real one."""
        return {"http_client": self.net.client(), "java_runner": self.installer, "verify_java": False, "build_runner": self.spigot.runner}

    def attach(self, ctx: Any) -> None:
        """Start the fake Java server wherever the manager would start the real Java."""
        ctx.servers.jvm.argv_wrapper = lambda argv: [sys.executable, "-u", str(FAKE_JAVA), json.dumps(self.behavior), *argv]
