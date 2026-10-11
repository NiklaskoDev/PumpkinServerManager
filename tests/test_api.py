"""The HTTP API end to end: auth, CSRF, server lifecycle, files, backups, console WebSocket."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import sys
import tarfile
import tomllib
from typing import Any

import httpx
import pytest_asyncio
import uvicorn
from conftest import FAKE, make_settings, wait_until
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from pumpkin_manager.app import create_app
from pumpkin_manager.security import SESSION_COOKIE


class Api:
    def __init__(self, base: str):
        self.base = base
        self.http = httpx.AsyncClient(base_url=base, timeout=30)
        self.csrf: str | None = None

    async def login(self, password: str = "correct horse battery") -> httpx.Response:
        resp = await self.http.post("/api/auth/login", json={"username": "admin", "password": password})
        if resp.status_code == 200:
            self.csrf = resp.json()["csrf"]
        return resp

    async def call(self, method: str, path: str, *, csrf: bool = True, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return await self.http.request(method, path, headers=headers, **kw)

    async def job(self, resp: httpx.Response, timeout: float = 90) -> dict[str, Any]:
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job"]["id"]

        async def finished() -> dict[str, Any] | None:
            row = (await self.call("GET", f"/api/jobs/{job_id}")).json()
            return row if row["status"] != "running" else None

        return await wait_until(finished, timeout, what="job to finish")

    async def create_server(self, name: str = "Alpha", **body: Any) -> dict[str, Any]:
        row = await self.job(await self.call("POST", "/api/servers", json={"name": name, **body}))
        assert row["status"] == "success", row["message"]
        servers = (await self.call("GET", "/api/servers")).json()
        return next(s for s in servers if s["name"] == name)


@pytest_asyncio.fixture
async def live(tmp_path, source):
    settings = make_settings(tmp_path)
    app = create_app(settings, release_source=source, verify_elf=False, background=False, stability_seconds=0.6)
    ctx = app.state.ctx
    ctx.servers.command_builder = lambda rec, build: [sys.executable, "-u", str(FAKE), str(ctx.builds.binary_path(build.id))]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.web_port, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, 20, what="web server")
    api = Api(f"http://127.0.0.1:{settings.web_port}")
    try:
        yield api, ctx
    finally:
        await api.http.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 60)


async def test_everything_but_login_and_health_requires_a_session(live):
    api, _ = live
    assert (await api.http.get("/api/health")).json()["status"] == "ok"
    for path in ["/api/servers", "/api/system", "/api/settings", "/api/backups", "/api/versions", "/api/activity", "/api/jobs"]:
        assert (await api.http.get(path)).status_code == 401, path
    index = await api.http.get("/")
    assert index.status_code == 200 and "Pumpkin" in index.text
    csp = index.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp


async def test_login_cookie_flags_and_logout(live):
    api, _ = live
    assert (await api.login("wrong")).status_code == 401
    resp = await api.login()
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert (await api.call("GET", "/api/auth/me")).json()["username"] == "admin"
    assert (await api.call("POST", "/api/auth/logout")).status_code == 200
    assert (await api.call("GET", "/api/auth/me")).status_code == 401


async def test_state_changing_requests_need_the_csrf_token(live):
    api, _ = live
    await api.login()
    resp = await api.call("POST", "/api/servers", csrf=False, json={"name": "Nope"})
    assert resp.status_code == 403 and "CSRF" in resp.json()["detail"]
    resp = await api.call("POST", "/api/servers", csrf=False, json={"name": "Nope"}, headers={"X-CSRF-Token": "forged"})
    assert resp.status_code == 403
    assert (await api.call("GET", "/api/servers", csrf=False)).status_code == 200, "reads need no token"


async def test_server_lifecycle_over_http(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Alpha", settings={"motd": "From the API", "default_gamemode": "Creative"}, bedrock=False)
    assert server["state"] == "running" and server["build"]["version"] == "0.1.0"
    assert server["ports"]["java"] == ctx.repo.get_server(server["id"]).java_port

    cfg = (await api.call("GET", f"/api/servers/{server['id']}/config")).json()
    assert cfg["values"]["motd"] == "From the API" and cfg["values"]["default_gamemode"] == "Creative"
    assert any(f["key"] == "view_distance" for f in cfg["schema"])

    saved = await api.call("PUT", f"/api/servers/{server['id']}/config", json={"values": {"max_players": 7}})
    assert saved.json() == {"saved": True, "restart_required": True, "applied_live": []}
    bad = await api.call("PUT", f"/api/servers/{server['id']}/config", json={"values": {"view_distance": 99, "motd": "x" * 500}})
    assert bad.status_code == 422 and set(bad.json()["errors"]) == {"view_distance", "motd"}

    assert (await api.job(await api.call("POST", f"/api/servers/{server['id']}/restart")))["status"] == "success"
    detail = (await api.call("GET", f"/api/servers/{server['id']}")).json()
    assert detail["state"] == "running"
    await ctx.servers._poll_players(ctx.repo.get_server(server["id"]))
    assert (await api.call("GET", f"/api/servers/{server['id']}")).json()["players"]["max"] == 7

    renamed = await api.call("PATCH", f"/api/servers/{server['id']}", json={"name": "Alpha Prime", "policy": {"backup_keep": 3}})
    assert renamed.json()["name"] == "Alpha Prime" and renamed.json()["policy"]["backup_keep"] == 3
    assert (await api.call("PATCH", f"/api/servers/{server['id']}", json={"policy": {"backup_keep": 0}})).status_code == 400

    dup = await api.job(await api.call("POST", "/api/servers", json={"name": "alpha prime"}))
    assert dup["status"] == "failed" and "already exists" in dup["message"]

    assert (await api.job(await api.call("POST", f"/api/servers/{server['id']}/stop")))["status"] == "success"
    assert (await api.call("GET", f"/api/servers/{server['id']}")).json()["state"] == "stopped"
    assert (await api.call("POST", f"/api/servers/{server['id']}/command", json={"command": "stop"})).status_code == 400


async def test_raw_config_masks_the_management_secret(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Raw", start=False)
    secret = ctx.repo.get_server(server["id"]).mgmt_secret
    raw = (await api.call("GET", f"/api/servers/{server['id']}/config/raw")).json()
    assert secret not in raw["content"] and "********" in raw["content"]

    edited = raw["content"] + "\n[pvp]\nenabled = false\n"
    result = (await api.call("PUT", f"/api/servers/{server['id']}/config/raw", json={"content": edited})).json()
    assert result["saved"]
    path = ctx.settings.servers_dir / server["id"] / "pumpkin.toml"
    on_disk = path.read_text()
    assert secret in on_disk and "[pvp]" in on_disk, "the real secret is kept and the edit applied"

    broken = await api.call("PUT", f"/api/servers/{server['id']}/config/raw", json={"content": "oops = = ="})
    assert broken.status_code == 422 and "not valid TOML" in broken.json()["detail"]
    assert path.read_text() == on_disk, "bad input never reaches the file"

    tampered = edited.replace("enabled = true", "enabled = false", 1)
    again = (await api.call("PUT", f"/api/servers/{server['id']}/config/raw", json={"content": tampered})).json()
    assert again["reset_managed_keys"], "keys owned by the manager are put back and reported"


async def test_file_api_is_confined_to_the_server_folder(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Files", start=False)
    base = f"/api/servers/{server['id']}/files"
    (ctx.settings.servers_dir / "outside.txt").write_text("secret")

    assert (await api.call("PUT", f"{base}/content", json={"path": "notes.txt", "content": "hi there"})).status_code == 200
    assert (await api.call("GET", f"{base}/content", params={"path": "notes.txt"})).json()["content"] == "hi there"
    up = await api.call("PUT", f"{base}/upload", params={"name": "pack.bin"}, content=b"\x00\x01" * 1000)
    assert up.json()["size"] == 2000
    assert (await api.call("GET", f"{base}/download", params={"path": "pack.bin"})).content == b"\x00\x01" * 1000
    names = [e["name"] for e in (await api.call("GET", base)).json()["entries"]]
    assert {"notes.txt", "pack.bin", "pumpkin.toml"} <= set(names)

    for evil in ["../outside.txt", "world/../../outside.txt", r"..\outside.txt", "/../outside.txt"]:
        resp = await api.call("GET", f"{base}/content", params={"path": evil})
        assert resp.status_code in (400, 404) and "secret" not in resp.text, evil
    assert (await api.call("GET", f"{base}/content", params={"path": "../outside.txt"})).status_code == 400
    assert (await api.call("PUT", f"{base}/upload", params={"name": "../x.txt"}, content=b"x")).status_code == 400
    assert (await api.call("PUT", f"{base}/content", json={"path": "../evil.txt", "content": "x"})).status_code == 400
    assert not (ctx.settings.servers_dir / "evil.txt").exists()

    toml_write = await api.call("PUT", f"{base}/content", json={"path": "pumpkin.toml", "content": "= broken"})
    assert toml_write.status_code == 422, "the main config is validated even when saved through the file editor"
    assert (await api.call("DELETE", base, params={"path": "notes.txt"})).status_code == 200
    assert (await api.call("DELETE", base, params={"path": ""})).status_code == 403


async def test_backup_endpoints(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Saver")
    sid = server["id"]
    row = await api.job(await api.call("POST", f"/api/servers/{sid}/backups", json={"note": "before experiments"}))
    assert row["status"] == "success", row["message"]

    listing = (await api.call("GET", "/api/backups", params={"server_id": sid})).json()
    backup = listing["backups"][0]
    assert backup["kind"] == "manual" and backup["verified"] and backup["note"] == "before experiments"
    assert "/" not in backup["file_name"]

    download = await api.call("GET", f"/api/backups/{backup['id']}/download")
    assert download.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(download.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert ".pumpkin-manager/manifest.json" in names and "pumpkin.toml" in names

    assert (await api.call("PATCH", f"/api/backups/{backup['id']}", json={"protected": True})).json()["protected"]
    assert (await api.call("DELETE", f"/api/backups/{backup['id']}")).status_code == 400
    await api.call("PATCH", f"/api/backups/{backup['id']}", json={"protected": False})

    upload = await api.call("PUT", f"/api/servers/{sid}/backups/upload", params={"name": "mine.tar.gz"}, content=download.content)
    assert upload.status_code == 200 and upload.json()["kind"] == "uploaded"
    junk = await api.call("PUT", f"/api/servers/{sid}/backups/upload", content=gzip.compress(b"not a tar"))
    assert junk.status_code == 400
    assert not list(ctx.settings.backup_dir.glob("*/*.partial"))

    restore = await api.job(await api.call("POST", f"/api/backups/{backup['id']}/restore", json={"safety_backup": True}))
    assert restore["status"] == "success", restore["message"]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["state"] == "running", "a running server comes back after a restore"
    assert (await api.call("DELETE", f"/api/backups/{backup['id']}")).status_code == 200
    assert (await api.call("GET", f"/api/backups/{backup['id']}/download")).status_code == 404


async def test_player_management_goes_through_the_console(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Players")
    sid = server["id"]
    ok = await api.call("POST", f"/api/servers/{sid}/players", json={"action": "whitelist_add", "target": "Steve_01"})
    assert ok.status_code == 200

    for action, target in (("op", "Alex"), ("ban", "Griefer"), ("ban_ip", "203.0.113.9")):
        assert (await api.call("POST", f"/api/servers/{sid}/players", json={"action": action, "target": target})).status_code == 200

    async def every_list_filled() -> dict[str, list[dict[str, Any]]] | None:
        seen = (await api.call("GET", f"/api/servers/{sid}/players")).json()["lists"]
        return seen if all(seen[key] for key in ("whitelist", "ops", "banned_players", "banned_ips")) else None

    seen = await wait_until(every_list_filled, 10, what="every list on the Players tab")
    assert seen["whitelist"][0]["name"] == "Steve_01" and seen["ops"][0]["name"] == "Alex"
    assert seen["banned_players"][0]["name"] == "Griefer" and seen["banned_ips"][0]["ip"] == "203.0.113.9"
    assert (await api.call("GET", f"/api/servers/{sid}/players")).json()["running"] is True
    for target in ["Steve; stop", "a b", "x" * 40, "../etc"]:
        resp = await api.call("POST", f"/api/servers/{sid}/players", json={"action": "op", "target": target})
        assert resp.status_code in (400, 422), target
    assert (await api.call("POST", f"/api/servers/{sid}/players", json={"action": "ban_ip", "target": "not-an-ip"})).status_code == 400
    await api.job(await api.call("POST", f"/api/servers/{sid}/stop"))
    stopped = await api.call("POST", f"/api/servers/{sid}/players", json={"action": "op", "target": "Alex"})
    assert stopped.status_code == 400 and "must be running" in stopped.json()["detail"]


async def test_operator_levels_over_http(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Levels")
    sid = server["id"]
    players = (await api.call("GET", f"/api/servers/{sid}/players")).json()
    assert players["op_level"] == 1, "the form preselects the server's default level"

    for body in ({"name": "Alex", "level": 0}, {"name": "Alex", "level": 5}, {"name": "", "level": 2}, {"level": 2}):
        assert (await api.call("POST", f"/api/servers/{sid}/operators/level", json=body)).status_code == 422, body
    assert (await api.call("POST", f"/api/servers/{sid}/operators/level", json={"name": "a b", "level": 2})).status_code == 400

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/operators/level", json={"name": "Alex", "level": 3}))
    assert row["status"] == "success", row["message"]
    ops = (await api.call("GET", f"/api/servers/{sid}/players")).json()["lists"]["ops"]
    assert [(e["name"], e["level"]) for e in ops] == [("Alex", 3)]
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["state"] == "running"


async def test_saving_a_new_difficulty_applies_it_to_the_running_server(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Dial", settings={"default_difficulty": "Easy"})
    sid = server["id"]
    await wait_until(lambda: any("Set game difficulty to Easy" in line["m"] for line in ctx.pm.backlog(sid)), 10, what="the creation setting")

    saved = (await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"default_difficulty": "Hard"}})).json()
    assert saved["saved"] and saved["applied_live"] == ["default_difficulty"] and saved["restart_required"] is False
    await wait_until(lambda: any("Set game difficulty to Hard" in line["m"] for line in ctx.pm.backlog(sid)), 10, what="the live change")

    other = (await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"motd": "changed"}})).json()
    assert other["applied_live"] == [] and other["restart_required"] is True
    bad = await api.call("PUT", f"/api/servers/{sid}/config", json={"values": {"default_difficulty": "Impossible"}})
    assert bad.status_code == 422


async def test_console_websocket_streams_and_checks_origin(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Console")
    sid = server["id"]
    cookie = f"{SESSION_COOKIE}={api.http.cookies[SESSION_COOKIE]}"
    ws_url = api.base.replace("http", "ws") + "/api/ws"

    async with connect(ws_url, additional_headers={"Cookie": cookie, "Origin": api.base}) as ws:
        await ws.send(json.dumps({"op": "subscribe", "topic": f"console:{sid}"}))
        backlog = json.loads(await asyncio.wait_for(ws.recv(), 10))
        while backlog["type"] != "backlog":
            backlog = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert any("Server is now running" in line["m"] for line in backlog["data"]["lines"])

        assert (await api.call("POST", f"/api/servers/{sid}/command", json={"command": "say hello from the dashboard"})).status_code == 200
        seen: list[str] = []
        for _ in range(40):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg["type"] == "console":
                seen += [line["m"] for line in msg["data"]["lines"]]
                if any("[Server] hello from the dashboard" in m for m in seen):
                    break
        assert any(m == "say hello from the dashboard" for m in seen), "the command is echoed"
        assert any("[Server] hello from the dashboard" in m for m in seen), "and the server's reply arrives"

    # A foreign Origin is refused even with a valid session cookie, and so is a missing session:
    # the handshake itself is rejected (HTTP 403), so no socket is ever established.
    for headers in ({"Cookie": cookie, "Origin": "http://evil.example"}, {"Origin": api.base}):
        try:
            async with connect(ws_url, additional_headers=headers):
                raise AssertionError("the connection must be refused")
        except InvalidStatus as exc:
            assert exc.response.status_code == 403


async def test_versions_and_update_endpoints(live):
    api, ctx = live
    await api.login()
    server = await api.create_server("Updatable")
    sid = server["id"]
    ctx.release_source.publish("stable", "0.9.0")
    checked = (await api.call("POST", "/api/versions/check")).json()
    assert checked["channels"]["stable"]["version"] == "0.9.0" and "stable" not in checked["errors"]
    detail = (await api.call("GET", f"/api/servers/{sid}")).json()
    assert detail["update"]["available"] and detail["update"]["pending_build"]["version"] == "0.9.0"

    releases = (await api.call("GET", "/api/versions/releases")).json()["releases"]
    assert [r["version"] for r in releases][:2] == ["0.9.0", "0.1.0"]

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/update", json={}))
    assert row["status"] == "success", row["message"]
    after = (await api.call("GET", f"/api/servers/{sid}")).json()
    assert after["build"]["version"] == "0.9.0" and after["previous_build"]["version"] == "0.1.0" and not after["update"]["available"]
    history = (await api.call("GET", f"/api/servers/{sid}/updates")).json()["updates"]
    assert history[0]["status"] == "success" and history[0]["backup_id"]

    in_use = await api.call("DELETE", f"/api/versions/{after['previous_build']['id']}")
    assert in_use.status_code == 409, "a version a server can still roll back to cannot be deleted"

    row = await api.job(await api.call("POST", f"/api/servers/{sid}/rollback", json={"restore_data": False}))
    assert row["status"] == "success"
    assert (await api.call("GET", f"/api/servers/{sid}")).json()["build"]["version"] == "0.1.0"


async def test_settings_never_leak_secrets_and_validate_input(live):
    api, ctx = live
    await api.login()
    put = await api.call("PUT", "/api/settings", json={"github_token": "ghp_abc", "update_check_minutes": 30, "telemetry_default": True})
    body = put.json()
    assert put.status_code == 200 and body["has_github_token"] and "github_token" not in body
    assert (await api.call("GET", "/api/settings")).json()["update_check_minutes"] == 30
    assert ctx._github_token() == "ghp_abc"
    assert (await api.call("PUT", "/api/settings", json={"update_check_minutes": 1})).status_code == 422
    assert (await api.call("PUT", "/api/settings", json={"webhook_url": "javascript:alert(1)"})).status_code == 422
    assert (await api.call("PUT", "/api/settings", json={"github_token": ""})).json()["has_github_token"] is False

    server = await api.create_server("Telemetry", start=False)
    cfg = tomllib.loads((ctx.settings.servers_dir / server["id"] / "pumpkin.toml").read_text())
    assert cfg["telemetry"]["enabled"] is True, "the default for new servers follows the global setting"


async def test_password_change_signs_out_other_sessions(live):
    api, _ = live
    await api.login()
    other = Api(api.base)
    await other.login()
    changed = await api.call("POST", "/api/auth/password", json={"current": "correct horse battery", "new": "a different password"})
    assert changed.status_code == 200
    assert (await api.call("GET", "/api/auth/me")).status_code == 200
    assert (await other.call("GET", "/api/auth/me")).status_code == 401
    await other.http.aclose()
    bad = await api.call("POST", "/api/auth/password", json={"current": "nope", "new": "yet another password"})
    assert bad.status_code == 400


async def test_uploads_respect_the_size_limit(live):
    api, ctx = live
    object.__setattr__(ctx.settings, "max_upload_bytes", 4096)  # frozen dataclass: tiny limit for the test
    await api.login()
    server = await api.create_server("Limits", start=False)
    big = b"x" * 10_000

    async def chunks():  # chunked body: no Content-Length for the server to check up front
        for i in range(0, len(big), 1000):
            yield big[i : i + 1000]

    resp = await api.http.put(f"/api/servers/{server['id']}/backups/upload", content=chunks(), headers={"X-CSRF-Token": api.csrf})
    assert resp.status_code == 413 and "limited" in resp.json()["detail"]
    assert not list(ctx.settings.backup_dir.glob("*/*.partial")) and not list(ctx.settings.backup_dir.glob("*/*uploaded*"))

    resp = await api.call("PUT", f"/api/servers/{server['id']}/files/upload", params={"name": "big.bin"}, content=big)
    assert resp.status_code == 413
    assert not (ctx.settings.servers_dir / server["id"] / "big.bin").exists()
    assert not list((ctx.settings.servers_dir / server["id"]).glob(".*.upload"))
