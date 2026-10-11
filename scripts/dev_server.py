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

    app = create_app(settings, release_source=source, verify_elf=False, stability_seconds=3)
    ctx = app.state.ctx
    ctx.servers.command_builder = lambda rec, build: [sys.executable, "-u", str(FAKE), str(ctx.builds.binary_path(build.id))]

    def publish_update() -> None:  # a newer stable release "appears" so the update flow is visible
        source.publish("stable", "0.2.0")
        print("[dev] published fake release 0.2.0 - the next update check will find it", flush=True)

    threading.Timer(float(os.environ.get("DEV_UPDATE_AFTER", "90")), publish_update).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
