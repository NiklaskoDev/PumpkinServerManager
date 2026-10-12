"""A complete fake outside world for Java servers, shared by the integration tests and the local dev dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jvmfakes import FakeCurseForge, FakeInternet, FakeJava, FakeMod, FakeModrinth

FAKE_JAVA = Path(__file__).parent / "fake_java_server.py"
LITHIUM = FakeMod("mods/lithium.jar", b"lithium" * 100)
CREATE = FakeMod("mods/create.jar", b"create" * 300)
JEI = FakeMod("mods/jei.jar", b"jei" * 50, server="unsupported")


class JavaWorld:
    """Adoptium, Mojang, Modrinth and the loaders' download sites, plus a fake ``java -jar <installer>`` and a fake Java server."""

    def __init__(self) -> None:
        self.net = FakeInternet()
        self.net.adoptium(21)
        self.net.adoptium(17, release="jdk-17.0.20.1+1")
        self.net.minecraft({"1.21.1": 21, "1.20.1": 17, "1.16.5": 8})
        self.net.maven_installer("neoforge", "1.21.1", "21.1.77")
        self.net.fabric("1.21.1", "0.16.9", "1.0.1")
        self.cf = FakeCurseForge(self.net)  # unused unless a test adds CurseForge packs (and gives the app a key)
        self.installer = FakeJava()
        self.behavior: dict[str, Any] = {"minecraft": "1.21.1"}  # what the fake Java server does (see fake_java_server.py)
        self.pack = FakeModrinth(self.net, slug="cool-pack", title="Cool Pack", project_id="AbCdEf12")
        self.pack.add_version(
            "1.0.0", mods=[LITHIUM, CREATE, JEI], overrides={"config/create.toml": b"speed = 1\n"},
            server_overrides={"server.properties": b"allow-flight=true\nview-distance=12\n"},
        )

    def app_options(self) -> dict[str, Any]:
        """Keyword arguments that make an ``AppContext`` / ``create_app`` use this world instead of the real one."""
        return {"http_client": self.net.client(), "java_runner": self.installer, "verify_java": False}

    def attach(self, ctx: Any) -> None:
        """Start the fake Java server wherever the manager would start the real Java."""
        ctx.servers.jvm.argv_wrapper = lambda argv: [sys.executable, "-u", str(FAKE_JAVA), json.dumps(self.behavior), *argv]
