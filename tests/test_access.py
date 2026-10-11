"""Accounts for friends: a role per server, temporary passwords, and what each account may see and do.

The three roles: a *player* sees the server's status; a *moderator* also starts it, reads its console and kicks, bans and
whitelists players (but cannot stop it, type commands or make operators); an *admin* can do everything on the server
except delete it. Friends only ever see the servers they were assigned to.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from conftest import Api, make_settings, wait_until
from fastapi.routing import APIRoute
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from pumpkin_manager import access, security
from pumpkin_manager.app import AppContext, create_app
from pumpkin_manager.db import MIGRATIONS, SCHEMA_V1, Database, _split_script
from pumpkin_manager.security import SESSION_COOKIE

OWN_PASSWORD = "my own password 42"
HIDDEN = "That server does not exist."


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """The accounts here are created and signed in many times; a cheaper scrypt keeps the tests quick (the cost is part of each hash)."""
    monkeypatch.setattr(security, "_SCRYPT_N", 2**10)


# ---------------------------------------------------------------------------------------------------- the world
@dataclass
class World:
    admin: Api
    ctx: AppContext
    ids: dict[str, str]
    friends: dict[str, Api]

    def __getitem__(self, name: str) -> Api:
        return self.friends[name]


async def login_status(base: str, username: str, password: str) -> int:
    """HTTP status of a sign-in attempt from a fresh client."""
    api = Api(base)
    try:
        return (await api.login(password, username)).status_code
    finally:
        await api.http.aclose()


async def new_friend(admin: Api, username: str, servers: dict[str, str]) -> tuple[Api, str, int]:
    """Create an account, sign in with the temporary password and choose a real one. Returns (client, temporary password, id)."""
    created = await admin.call("POST", "/api/users", json={"username": username, "servers": servers})
    assert created.status_code == 201, created.text
    temporary = created.json()["temporary_password"]
    api = Api(admin.base)
    login = await api.login(temporary, username)
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    changed = await api.call("POST", "/api/auth/password", json={"current": temporary, "new": OWN_PASSWORD})
    assert changed.status_code == 200, changed.text
    return api, temporary, created.json()["user"]["id"]


@pytest_asyncio.fixture
async def world(live):
    admin, ctx = live
    assert (await admin.login()).status_code == 200
    alpha = (await admin.create_server("Alpha"))["id"]
    beta = (await admin.create_server("Beta"))["id"]
    w = World(admin, ctx, {"alpha": alpha, "beta": beta}, {})
    accounts = {"pia": {alpha: "player"}, "moe": {alpha: "moderator"}, "ada": {alpha: "admin"}, "nina": {}, "bob": {beta: "admin"}}
    for name, servers in accounts.items():
        w.friends[name], _, _ = await new_friend(admin, name, servers)
    try:
        yield w
    finally:
        for api in w.friends.values():
            await api.http.aclose()


def user_id(w: World, name: str) -> int:
    return w.ctx.repo.get_user_by_name(name)["id"]


# ------------------------------------------------------------------------------------- what the roles are
def test_the_three_roles_are_exactly_as_agreed():
    assert access.ROLES == ("player", "moderator", "admin")
    assert access.ROLE_PERMS["player"] == {access.VIEW}
    assert access.ROLE_PERMS["moderator"] == {
        access.VIEW, access.CONSOLE_VIEW, access.ACTIVITY, access.START, access.MODERATE,
    }, "status, console (read-only), start (not stop), kick/ban/whitelist (not op)"
    everything_but_delete = access.ALL_PERMS - {access.DELETE}
    assert access.ROLE_PERMS["admin"] == everything_but_delete
    for perm in (access.CONTROL, access.CONSOLE_COMMAND, access.OPERATORS, access.DELETE):
        assert perm not in access.ROLE_PERMS["moderator"]
    assert access.DELETE not in access.ROLE_PERMS["admin"], "only the administrator deletes servers"
    assert access.OWNER_PERMS == access.ALL_PERMS


def request_for(host: str, *, forwarded_proto: str | None = None):
    """A request as the dashboard sends it: the Host header is the address the administrator typed into the browser."""
    from starlette.requests import Request

    name, _, port = host.partition(":")
    headers = [(b"host", host.encode())] + ([(b"x-forwarded-proto", forwarded_proto.encode())] if forwarded_proto else [])
    return Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"", "headers": headers, "server": (name, int(port or 80))})


def sign_in_for(host: str, *, dashboard: str | None = None, env: str | None = None, forwarded_proto: str | None = None) -> dict:
    from types import SimpleNamespace

    from pumpkin_manager.api.users import sign_in_target

    ctx = SimpleNamespace(app_settings=lambda: SimpleNamespace(public_address=dashboard), settings=SimpleNamespace(public_address=env))
    return sign_in_target(request_for(host, forwarded_proto=forwarded_proto), ctx)


def test_the_sign_in_address_uses_the_public_address_you_configured():
    lan = "192.168.0.233:8420"
    assert sign_in_for(lan) == {"sign_in_url": "http://192.168.0.233:8420", "sign_in_unencrypted": False}, "nothing configured: as before"
    # the address in the screenshot that prompted this: the server cards already show the DynDNS name, so must the sign-in link
    named = sign_in_for(lan, dashboard="niklasko.ddns.net")
    assert named == {"sign_in_url": "http://niklasko.ddns.net:8420", "sign_in_unencrypted": True}
    assert sign_in_for(lan, env="env.example.org")["sign_in_url"] == "http://env.example.org:8420", "the PUBLIC_ADDRESS variable counts too"
    assert sign_in_for(lan, dashboard="dash.example.org", env="env.example.org")["sign_in_url"] == "http://dash.example.org:8420"
    # a reverse proxy: https, and the port people use is none at all
    proxied = sign_in_for("manager.example.com", dashboard="niklasko.ddns.net", forwarded_proto="https")
    assert proxied == {"sign_in_url": "https://niklasko.ddns.net", "sign_in_unencrypted": False}


def test_what_people_type_as_public_address_is_cleaned_up():
    from pumpkin_manager.api.users import bare_host, is_private_host

    assert bare_host("play.example.com") == "play.example.com"
    assert bare_host("  Play.Example.com  ") == "Play.Example.com"
    assert bare_host("https://play.example.com:25565/path?x=1") == "play.example.com"
    assert bare_host("play.example.com:25565") == "play.example.com" and bare_host("203.0.113.7:25565") == "203.0.113.7"
    assert bare_host("::1") == "[::1]" and bare_host("[2001:db8::1]:25565") == "[2001:db8::1]"
    assert bare_host("") is None and bare_host(None) is None and bare_host("https://") is None
    assert sign_in_for("10.0.0.5:8420", dashboard="[2001:db8::1]:25565")["sign_in_url"] == "http://[2001:db8::1]:8420"

    for private in ("localhost", "127.0.0.1", "192.168.0.233", "10.1.2.3", "172.16.0.9", "169.254.1.1", "[::1]", "tower", "nas.local", "box.lan"):
        assert is_private_host(private), private
    for public in ("niklasko.ddns.net", "play.example.com", "1.1.1.1", "8.8.8.8", "[2606:4700:4700::1111]"):  # not the documentation ranges, which count as private
        assert not is_private_host(public), public


async def test_the_dialog_gets_its_sign_in_address_from_the_server(live):
    admin, _ = live
    await admin.login()
    port = admin.base.rsplit(":", 1)[1]

    async def create(name: str, **kwargs):
        return (await admin.call("POST", "/api/users", json={"username": name, "servers": {}}, **kwargs)).json()

    first = await create("addr_one")
    assert first["sign_in_url"] == f"http://127.0.0.1:{port}" and first["sign_in_unencrypted"] is False
    assert (await admin.call("PUT", "/api/settings", json={"public_address": "niklasko.ddns.net"})).status_code == 200
    second = await create("addr_two")
    assert second["sign_in_url"] == f"http://niklasko.ddns.net:{port}" and second["sign_in_unencrypted"] is True

    reset = (await admin.call("POST", f"/api/users/{second['user']['id']}/reset-password")).json()
    assert reset["sign_in_url"] == second["sign_in_url"], "a reset password comes with the same address"
    proxied = await create("addr_three", headers={"X-Forwarded-Proto": "https", "Host": "manager.example.com"})
    assert proxied["sign_in_url"] == "https://niklasko.ddns.net" and proxied["sign_in_unencrypted"] is False


def test_the_dashboard_uses_the_same_permission_and_role_names_as_the_server():
    from pathlib import Path

    js = (Path(__file__).parent.parent / "pumpkin_manager" / "static" / "js" / "access.js").read_text(encoding="utf-8")
    for perm in access.ALL_PERMS:
        assert f'"{perm}"' in js, f"access.js does not know the permission {perm!r}"
    for role in access.ROLES:
        assert f'id: "{role}"' in js, f"access.js does not describe the role {role!r}"
    assert set(re.findall(r'^\s+[A-Z_]+: "([a-z.]+)",?$', js, flags=re.M)) == set(access.ALL_PERMS), "access.js lists permissions the server lacks"


def test_every_api_route_declares_who_may_use_it(tmp_path, source):
    """A route added later without a permission check must fail here, not ship open to every signed-in friend."""
    import importlib
    import pkgutil

    from pumpkin_manager import api as api_package

    public = {("GET", "/api/health"), ("POST", "/api/auth/login")}
    routes: list[tuple[str, str, APIRoute]] = []
    for info in pkgutil.iter_modules(api_package.__path__):
        router = getattr(importlib.import_module(f"pumpkin_manager.api.{info.name}"), "router", None)
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                routes += [(method, "/api" + route.path, route) for method in route.methods - {"HEAD", "OPTIONS"}]
    assert len(routes) >= 55, 'the routers were not found'

    # ...and the app really serves exactly these routes (a router nobody included, or one added elsewhere, shows up here)
    app = create_app(make_settings(tmp_path), release_source=source, verify_elf=False, background=False)
    try:
        served = {(method.upper(), path) for path, operations in app.openapi()["paths"].items() for method in operations}
    finally:
        app.state.ctx.db.close()
    assert served == {(method, path) for method, path, _ in routes}

    def policies(dependant) -> list[tuple[str, str | None]]:
        found = [dependant.call.policy] if getattr(dependant.call, "policy", None) else []
        for sub in dependant.dependencies:
            found += policies(sub)
        return found

    pending_ok = sorted(
        f"{method} {path}" for method, path, route in routes
        if any(kind == "session-while-password-pending" for kind, _ in policies(route.dependant))
    )
    assert pending_ok == ["GET /api/auth/me", "POST /api/auth/logout", "POST /api/auth/password"], (
        "only these routes may work with a temporary password: " + str(pending_ok)
    )

    undeclared = []
    for method, path, route in routes:
        declared = {kind for kind, _ in policies(route.dependant)}
        if (method, path) in public:
            assert not declared, f"{method} {path} is public"
        elif not declared - {"session"}:  # a plain session only proves who you are, not what you may do
            undeclared.append(f"{method} {path}")
    assert not undeclared, f"routes without a permission check: {undeclared}"

# -------------------------------------------------------------------------- every server route, every role
def server_calls(sid: str) -> list[tuple[str, str, str, dict[str, Any], str]]:
    """(label, method, path, request arguments, the permission it needs). Allowed calls really run, so they must be harmless."""
    base = f"/api/servers/{sid}"
    post = lambda body: {"json": body}  # noqa: E731
    return [
        ("status", "GET", base, {}, access.VIEW),
        ("start", "POST", f"{base}/start", {}, access.START),
        ("console", "GET", f"{base}/console", {}, access.CONSOLE_VIEW),
        ("activity", "GET", f"/api/activity?server_id={sid}", {}, access.ACTIVITY),
        ("jobs", "GET", f"/api/jobs?server_id={sid}", {}, access.ACTIVITY),
        ("player lists", "GET", f"{base}/players", {}, access.MODERATE),
        ("whitelist add", "POST", f"{base}/players", post({"action": "whitelist_add", "target": "Steve"}), access.MODERATE),
        ("whitelist remove", "POST", f"{base}/players", post({"action": "whitelist_remove", "target": "Steve"}), access.MODERATE),
        ("kick", "POST", f"{base}/players", post({"action": "kick", "target": "Steve"}), access.MODERATE),
        ("ban", "POST", f"{base}/players", post({"action": "ban", "target": "Griefer", "reason": "x"}), access.MODERATE),
        ("pardon", "POST", f"{base}/players", post({"action": "pardon", "target": "Griefer"}), access.MODERATE),
        ("ban ip", "POST", f"{base}/players", post({"action": "ban_ip", "target": "203.0.113.9"}), access.MODERATE),
        ("pardon ip", "POST", f"{base}/players", post({"action": "pardon_ip", "target": "203.0.113.9"}), access.MODERATE),
        ("make operator", "POST", f"{base}/players", post({"action": "op", "target": "Alex"}), access.OPERATORS),
        ("remove operator", "POST", f"{base}/players", post({"action": "deop", "target": "Alex"}), access.OPERATORS),
        ("operator level", "POST", f"{base}/operators/level", post({"name": "Alex", "level": 1}), access.OPERATORS),
        ("command", "POST", f"{base}/command", post({"command": "list"}), access.CONSOLE_COMMAND),
        ("config read", "GET", f"{base}/config", {}, access.SETTINGS),
        ("config write", "PUT", f"{base}/config", post({"values": {"motd": "hello"}}), access.SETTINGS),
        ("raw config read", "GET", f"{base}/config/raw", {}, access.SETTINGS),
        ("raw config write", "PUT", f"{base}/config/raw", post({"content": "# nothing\n"}), access.SETTINGS),
        ("server settings", "PATCH", base, post({"policy": {"backup_keep": 7}}), access.SETTINGS),
        ("files list", "GET", f"{base}/files", {}, access.FILES),
        ("file read", "GET", f"{base}/files/content?path=pumpkin.toml", {}, access.FILES),
        ("file write", "PUT", f"{base}/files/content", post({"path": "notes.txt", "content": "hi"}), access.FILES),
        ("file download", "GET", f"{base}/files/download?path=pumpkin.toml", {}, access.FILES),
        ("file upload", "PUT", f"{base}/files/upload?name=up.txt", {"content": b"x"}, access.FILES),
        ("make folder", "POST", f"{base}/files/mkdir", post({"path": "newdir"}), access.FILES),
        ("rename", "POST", f"{base}/files/rename", post({"source": "notes.txt", "destination": "notes2.txt"}), access.FILES),
        ("file delete", "DELETE", f"{base}/files?path=notes2.txt", {}, access.FILES),
        ("backup list", "GET", f"/api/backups?server_id={sid}", {}, access.BACKUPS),
        ("backup create", "POST", f"{base}/backups", post({"note": "matrix"}), access.BACKUPS),
        ("backup upload", "PUT", f"{base}/backups/upload", {"content": b"not an archive"}, access.BACKUPS),
        ("update history", "GET", f"{base}/updates", {}, access.VERSIONS),
        ("update check", "POST", f"{base}/update/check", {}, access.VERSIONS),
        ("update", "POST", f"{base}/update", post({}), access.VERSIONS),
        ("rollback", "POST", f"{base}/rollback", post({}), access.VERSIONS),
        ("restart", "POST", f"{base}/restart", {}, access.CONTROL),
        ("stop", "POST", f"{base}/stop", {}, access.CONTROL),
        ("kill", "POST", f"{base}/kill", {}, access.CONTROL),
        ("delete", "DELETE", f"{base}?final_backup=false", {}, access.DELETE),
    ]


async def test_every_server_route_lets_in_exactly_the_roles_that_hold_its_permission(world):
    sid = world.ids["alpha"]
    accounts = (("nina", None), ("bob", None), ("pia", "player"), ("moe", "moderator"), ("ada", "admin"))
    for who, role in accounts:  # the admin role goes last: its calls stop and kill the server
        for label, method, path, kwargs, perm in server_calls(sid):
            resp = await world[who].call(method, path, **kwargs)
            where = f"{who} ({role or 'no access'}): {label} -> {resp.status_code} {resp.text[:120]}"
            hidden = resp.status_code == 404 and resp.json().get("detail") == HIDDEN
            if role is None:
                assert hidden, where  # a server you were not assigned to does not exist, as far as you can tell
            elif perm in access.ROLE_PERMS[role]:
                assert resp.status_code != 403 and not hidden, where
            else:
                assert resp.status_code == 403, where
    still_there = await world.admin.call("GET", f"/api/servers/{sid}")
    assert still_there.status_code == 200, "nobody but the administrator can delete a server"


async def test_everything_global_is_for_the_administrator_only(world):
    sid, bid = world.ids["alpha"], world.ctx.repo.list_servers()[0].build_id
    owner_only = [
        ("GET", "/api/system", {}), ("GET", "/api/settings", {}), ("PUT", "/api/settings", {"json": {"telemetry_default": True}}),
        ("POST", "/api/settings/webhook-test", {"json": {}}), ("POST", "/api/servers", {"json": {"name": "Mine"}}),
        ("POST", "/api/versions/check", {}), ("POST", "/api/versions/download", {"json": {}}), ("POST", "/api/versions/prune", {}),
        ("DELETE", f"/api/versions/{bid}", {}), ("GET", "/api/users", {}),
        ("POST", "/api/users", {"json": {"username": "newbie", "servers": {}}}),
        ("PUT", "/api/users/1/servers", {"json": {"servers": {}}}), ("POST", "/api/users/1/reset-password", {}),
        ("DELETE", "/api/users/1", {}),
    ]
    for who in world.friends:
        for method, path, kwargs in owner_only:
            resp = await world[who].call(method, path, **kwargs)
            assert resp.status_code == 403, f"{who}: {method} {path} -> {resp.status_code}"
    assert (await world.admin.call("GET", "/api/system")).status_code == 200
    assert world.ctx.repo.get_server(sid) is not None


async def test_version_choices_are_open_to_server_admins_without_revealing_other_servers(world):
    full = (await world.admin.call("GET", "/api/versions")).json()
    assert full["builds"], "the administrator sees every build and who uses it"
    for who, allowed in (("ada", True), ("bob", True), ("moe", False), ("pia", False), ("nina", False)):
        resp = await world[who].call("GET", "/api/versions")
        assert (resp.status_code == 200) is allowed, who
        if allowed:
            assert resp.json()["builds"] == [] and set(resp.json()["channels"]) == {"stable", "nightly", "canary"}
        assert (await world[who].call("GET", "/api/versions/releases")).status_code == (200 if allowed else 403)


# -------------------------------------------------------------------------------- what each role can do
async def test_friends_only_see_the_servers_they_were_assigned(world):
    names = {}
    for who in ("pia", "moe", "ada", "nina", "bob"):
        names[who] = sorted(s["name"] for s in (await world[who].call("GET", "/api/servers")).json())
    assert names == {"pia": ["Alpha"], "moe": ["Alpha"], "ada": ["Alpha"], "nina": [], "bob": ["Beta"]}
    assert sorted(s["name"] for s in (await world.admin.call("GET", "/api/servers")).json()) == ["Alpha", "Beta"]

    # A server that exists but is not yours answers exactly like one that does not exist.
    for path in ("{}", "{}/console", "{}/players", "{}/files"):
        mine = await world["pia"].call("GET", "/api/servers/" + path.format(world.ids["beta"]))
        none = await world["pia"].call("GET", "/api/servers/" + path.format("no-such-server"))
        assert mine.status_code == none.status_code == 404 and mine.json() == none.json(), path


async def test_a_player_can_see_the_status_and_nothing_else(world):
    sid = world.ids["alpha"]
    full = (await world.admin.call("GET", f"/api/servers/{sid}")).json()
    seen = (await world["pia"].call("GET", f"/api/servers/{sid}")).json()
    assert seen["access"] == {"role": "player", "can": ["view"]}
    for key in ("state", "uptime", "players", "stats", "ports", "mc_version", "motd", "build"):
        assert seen[key] == full[key] or key in ("uptime", "stats"), key
    assert seen["policy"] == {} and seen["last_backup_at"] is None and seen["last_error"] is None
    assert seen["update"] == {"mode": None, "available": False, "pending_build": None, "pending_since": None, "last_check": None}
    assert full["policy"] and full["access"]["role"] == "owner" and access.DELETE in full["access"]["can"]
    assert set(seen) <= set(full) | {"access"}, "a trimmed view never has more than the full one"
    assert (await world["pia"].call("GET", f"/api/servers/{sid}/console")).status_code == 403
    assert (await world["pia"].call("POST", f"/api/servers/{sid}/start")).status_code == 403


async def test_a_moderator_can_start_but_not_stop_the_server(world):
    sid, admin, mo = world.ids["alpha"], world.admin, world["moe"]
    assert (await admin.job(await admin.call("POST", f"/api/servers/{sid}/stop")))["status"] == "success"
    started = await mo.job(await mo.call("POST", f"/api/servers/{sid}/start"))
    assert started["status"] == "success", started["message"]
    for action in ("stop", "restart", "kill"):
        assert (await mo.call("POST", f"/api/servers/{sid}/{action}")).status_code == 403, action
    assert (await admin.call("GET", f"/api/servers/{sid}")).json()["state"] == "running"


async def test_a_moderator_can_read_the_console_but_not_type_into_it(world):
    sid, mo = world.ids["alpha"], world["moe"]
    lines = (await mo.call("GET", f"/api/servers/{sid}/console")).json()["lines"]
    assert any("Server is now running" in line["m"] for line in lines)
    typed = await mo.call("POST", f"/api/servers/{sid}/command", json={"command": "say hello"})
    assert typed.status_code == 403
    await asyncio.sleep(0.5)
    assert not any(line["m"] == "say hello" for line in world.ctx.pm.backlog(sid)), "nothing reached the server"


async def test_a_moderator_can_kick_ban_and_whitelist_but_not_make_operators(world):
    sid, mo = world.ids["alpha"], world["moe"]
    base = f"/api/servers/{sid}/players"
    for action, target in (("whitelist_add", "Steve"), ("ban", "Griefer"), ("ban_ip", "203.0.113.9"), ("kick", "Steve")):
        assert (await mo.call("POST", base, json={"action": action, "target": target})).status_code == 200, action
    lists = None

    async def filled():
        nonlocal lists
        lists = (await mo.call("GET", base)).json()["lists"]
        return lists["whitelist"] and lists["banned_players"] and lists["banned_ips"]

    await wait_until(filled, 10, what="the moderator's changes in the lists")
    assert lists["whitelist"][0]["name"] == "Steve" and lists["banned_players"][0]["name"] == "Griefer"
    for action in ("pardon", "pardon_ip", "whitelist_remove"):
        target = {"pardon": "Griefer", "pardon_ip": "203.0.113.9", "whitelist_remove": "Steve"}[action]
        assert (await mo.call("POST", base, json={"action": action, "target": target})).status_code == 200, action

    for body in ({"action": "op", "target": "Alex"}, {"action": "deop", "target": "Alex"}):
        assert (await mo.call("POST", base, json=body)).status_code == 403, body
    assert (await mo.call("POST", f"/api/servers/{sid}/operators/level", json={"name": "Alex", "level": 2})).status_code == 403
    await asyncio.sleep(0.5)
    assert (await world.admin.call("GET", base)).json()["lists"]["ops"] == [], "no operator was made"


async def test_an_admin_can_do_everything_on_the_server_except_delete_it(world):
    sid, ada = world.ids["alpha"], world["ada"]
    base = f"/api/servers/{sid}"
    assert (await ada.job(await ada.call("POST", f"{base}/stop")))["status"] == "success"
    assert (await ada.job(await ada.call("POST", f"{base}/start")))["status"] == "success"
    assert (await ada.job(await ada.call("POST", f"{base}/restart")))["status"] == "success"
    assert (await ada.call("POST", f"{base}/command", json={"command": "say hi from ada"})).status_code == 200
    assert (await ada.call("PUT", f"{base}/files/content", json={"path": "ada.txt", "content": "mine"})).status_code == 200
    assert (await ada.call("GET", f"{base}/files/content", params={"path": "ada.txt"})).json()["content"] == "mine"
    assert (await ada.call("PUT", f"{base}/config", json={"values": {"motd": "by ada"}})).status_code == 200
    assert (await ada.job(await ada.call("POST", f"{base}/backups", json={"note": "by ada"})))["status"] == "success"
    assert (await ada.call("POST", f"{base}/players", json={"action": "op", "target": "Alex"})).status_code == 200
    await wait_until(lambda: world.ctx.servers.read_player_lists(world.ctx.repo.get_server(sid))["ops"], 10, what="the operator")
    leveled = await ada.job(await ada.call("POST", f"{base}/operators/level", json={"name": "Alex", "level": 3}))
    assert leveled["status"] == "success", leveled["message"]

    assert (await ada.call("DELETE", base)).status_code == 403
    assert (await ada.call("POST", "/api/servers", json={"name": "Mine"})).status_code == 403
    assert world.ctx.repo.get_server(sid) is not None


async def test_a_server_admin_of_one_server_is_a_stranger_on_another(world):
    bob, beta, alpha = world["bob"], world.ids["beta"], world.ids["alpha"]
    assert (await bob.call("POST", f"/api/servers/{beta}/command", json={"command": "list"})).status_code == 200
    assert (await bob.call("POST", f"/api/servers/{alpha}/command", json={"command": "list"})).status_code == 404


async def test_a_role_change_takes_effect_on_the_next_request(world):
    sid, mo, admin = world.ids["alpha"], world["moe"], world.admin
    assert (await mo.call("GET", f"/api/servers/{sid}/console")).status_code == 200
    changed = await admin.call("PUT", f"/api/users/{user_id(world, 'moe')}/servers", json={"servers": {sid: "player"}})
    assert changed.status_code == 200
    assert (await mo.call("GET", f"/api/servers/{sid}/console")).status_code == 403
    assert (await mo.call("GET", f"/api/servers/{sid}")).json()["access"]["role"] == "player"
    await admin.call("PUT", f"/api/users/{user_id(world, 'moe')}/servers", json={"servers": {}})
    assert (await mo.call("GET", f"/api/servers/{sid}")).status_code == 404
    assert (await mo.call("GET", "/api/servers")).json() == []


async def test_deleting_a_server_removes_every_assignment_to_it(world):
    sid = world.ids["beta"]
    job = await world.admin.job(await world.admin.call("DELETE", f"/api/servers/{sid}", params={"final_backup": "false"}))
    assert job["status"] == "success", job["message"]
    users = {u["username"]: u["servers"] for u in (await world.admin.call("GET", "/api/users")).json()}
    assert users["bob"] == {} and users["ada"] == {world.ids["alpha"]: "admin"}
    # a new server that happens to get the same id must not be inherited by the former administrator of the old one
    again = await world.admin.create_server("Beta")
    assert again["id"] == sid
    assert (await world["bob"].call("GET", f"/api/servers/{sid}")).status_code == 404


# ------------------------------------------------------------------------- activity, jobs and backups
async def test_activity_and_jobs_are_limited_to_servers_the_account_may_follow(world):
    alpha, beta = world.ids["alpha"], world.ids["beta"]
    await world.admin.job(await world.admin.call("POST", f"/api/servers/{beta}/restart"))
    await world.admin.job(await world.admin.call("POST", f"/api/servers/{alpha}/restart"))

    def servers_of(rows: list[dict]) -> set:
        return {r["server_id"] for r in rows}

    everything = (await world.admin.call("GET", "/api/activity?limit=200")).json()
    assert servers_of(everything) >= {None, alpha, beta}, "the administrator also sees sign-ins and other global entries"
    for who, expected in (("moe", {alpha}), ("ada", {alpha}), ("bob", {beta}), ("pia", set()), ("nina", set())):
        rows = (await world[who].call("GET", "/api/activity?limit=200")).json()
        assert servers_of(rows) == expected, who
        jobs = (await world[who].call("GET", "/api/jobs?limit=200")).json()
        assert servers_of(jobs) <= expected, who
    assert (await world["pia"].call("GET", f"/api/activity?server_id={alpha}")).status_code == 403
    assert (await world["bob"].call("GET", f"/api/activity?server_id={alpha}")).status_code == 404

    beta_job = world.ctx.repo.list_jobs(5, beta)[0]["id"]
    alpha_job = world.ctx.repo.list_jobs(5, alpha)[0]["id"]
    for who, sees_alpha, sees_beta in (("moe", True, False), ("bob", False, True), ("pia", False, False), ("nina", False, False)):
        assert ((await world[who].call("GET", f"/api/jobs/{alpha_job}")).status_code == 200) is sees_alpha, who
        assert ((await world[who].call("GET", f"/api/jobs/{beta_job}")).status_code == 200) is sees_beta, who
    assert (await world["moe"].call("GET", "/api/jobs/does-not-exist")).status_code == 404


async def test_backups_are_visible_and_manageable_only_to_admins_of_that_server(world):
    alpha, beta = world.ids["alpha"], world.ids["beta"]
    for sid in (alpha, beta):
        assert (await world.admin.job(await world.admin.call("POST", f"/api/servers/{sid}/backups", json={})))["status"] == "success"
    on_alpha = world.ctx.repo.list_backups(alpha)[0]["id"]
    on_beta = world.ctx.repo.list_backups(beta)[0]["id"]

    listing = {who: (await world[who].call("GET", "/api/backups")).json()["backups"] for who in world.friends}
    assert {b["server_id"] for b in listing["ada"]} == {alpha} and {b["server_id"] for b in listing["bob"]} == {beta}
    assert listing["moe"] == listing["pia"] == listing["nina"] == [], "moderators and players do not see backups at all"
    assert {b["server_id"] for b in (await world.admin.call("GET", "/api/backups")).json()["backups"]} == {alpha, beta}

    for who, status in (("ada", 200), ("bob", 404), ("moe", 403), ("pia", 403), ("nina", 404)):
        assert (await world[who].call("GET", f"/api/backups/{on_alpha}/download")).status_code == status, who
        assert (await world[who].call("PATCH", f"/api/backups/{on_alpha}", json={"protected": False})).status_code == status, who
    restore = await world["bob"].call("POST", f"/api/backups/{on_alpha}/restore", json={})
    assert restore.status_code == 404, "restoring another server's backup is not possible"
    assert (await world["ada"].call("DELETE", f"/api/backups/{on_beta}")).status_code == 404
    assert world.ctx.repo.get_backup(on_beta) is not None and world.ctx.repo.get_backup(on_alpha) is not None


# ------------------------------------------------------------------------------------------ the live feed
def ws_for(api: Api):
    cookie = f"{SESSION_COOKIE}={api.http.cookies[SESSION_COOKIE]}"
    return connect(api.base.replace("http", "ws") + "/api/ws", additional_headers={"Cookie": cookie, "Origin": api.base})


async def drain(ws, seconds: float = 1.5) -> list[dict]:
    messages: list[dict] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while (left := end - loop.time()) > 0:
        try:
            messages.append(json.loads(await asyncio.wait_for(ws.recv(), left)))
        except TimeoutError:
            break
    return messages


def mentions(messages: list[dict], needle: str) -> bool:
    return needle in json.dumps(messages)


def about(messages: list[dict], server_id: str) -> bool:
    """True if any message concerns that server (its console, its view or job, or its entry in a stats map)."""
    for m in messages:
        data = m["data"]
        if m["topic"].endswith(f":{server_id}"):
            return True
        if isinstance(data, dict) and (data.get("id") == server_id or data.get("server_id") == server_id or server_id in data):
            return True
    return False


async def test_the_live_feed_only_carries_what_the_account_may_see(world):
    alpha, beta = world.ids["alpha"], world.ids["beta"]
    async with ws_for(world["moe"]) as mo, ws_for(world["pia"]) as pia, ws_for(world["bob"]) as bob, ws_for(world.admin) as owner:
        for ws, topic in ((mo, alpha), (mo, beta), (pia, alpha), (bob, beta), (bob, alpha)):
            await ws.send(json.dumps({"op": "subscribe", "topic": f"console:{topic}"}))
        got = dict(zip(("moe", "pia", "bob"), await asyncio.gather(drain(mo), drain(pia), drain(bob)), strict=True))
        backlogs = {who: {m["data"]["server_id"] for m in msgs if m["type"] == "backlog"} for who, msgs in got.items()}
        assert backlogs == {"moe": {alpha}, "pia": set(), "bob": {beta}}, "console snapshots only for consoles the role may read"

        # live console lines
        await world.admin.call("POST", f"/api/servers/{beta}/command", json={"command": "say only for beta"})
        await world.admin.call("POST", f"/api/servers/{alpha}/command", json={"command": "say only for alpha"})
        got = dict(zip(("moe", "pia", "bob"), await asyncio.gather(drain(mo), drain(pia), drain(bob)), strict=True))
        assert mentions(got["moe"], "only for alpha") and not mentions(got["moe"], "only for beta")
        assert mentions(got["bob"], "only for beta") and not mentions(got["bob"], "only for alpha")
        assert not mentions(got["pia"], "only for")

        # server state: restarting alpha is news for its three friends (shaped for each role), not for bob
        await world.admin.job(await world.admin.call("POST", f"/api/servers/{alpha}/restart"))
        await world.ctx.servers.monitor_tick()
        got = dict(zip(("moe", "pia", "bob", "owner"), await asyncio.gather(drain(mo, 2), drain(pia, 2), drain(bob, 2), drain(owner, 2)), strict=True))
        assert not about(got["bob"], alpha), "nothing about a server bob has no access to"
        for who, role in (("moe", "moderator"), ("pia", "player")):
            views = [m["data"] for m in got[who] if m["type"] == "server" and m["data"]["id"] == alpha]
            assert views and all(v["access"]["role"] == role and v["policy"] == {} for v in views), who
            stats = [m["data"] for m in got[who] if m["type"] == "stats"]
            assert stats and all(set(s) <= {alpha} for s in stats), who
        assert any(m["topic"] == "jobs" for m in got["moe"]) and not any(m["topic"] == "jobs" for m in got["pia"]), "a player gets no job news"
        assert not any(m["topic"] == "activity" for m in got["pia"])
        full = [m["data"] for m in got["owner"] if m["type"] == "server" and m["data"]["id"] == alpha]
        assert full and full[0]["policy"], "the administrator still gets the complete view"
        assert full[0]["access"]["role"] == "owner" and access.DELETE in full[0]["access"]["can"], "with the same access entry as in the REST views"
        assert any(m["topic"] == "activity" for m in got["owner"]), "and every kind of event, including ones about no server in particular"


async def test_a_console_cannot_be_followed_without_the_role_and_not_after_losing_it(world):
    alpha = world.ids["alpha"]
    async with ws_for(world["moe"]) as mo:
        await mo.send(json.dumps({"op": "subscribe", "topic": f"console:{alpha}"}))
        assert any(m["type"] == "backlog" for m in await drain(mo))
        await world.admin.call("PUT", f"/api/users/{user_id(world, 'moe')}/servers", json={"servers": {alpha: "player"}})
        await world.admin.call("POST", f"/api/servers/{alpha}/command", json={"command": "say after the demotion"})
        later = await drain(mo)
        assert not mentions(later, "after the demotion"), "the lines stop as soon as the role no longer allows them"
        assert any(m["type"] == "access" for m in later), "and the dashboard is told to refresh what it shows"
        await mo.send(json.dumps({"op": "subscribe", "topic": f"console:{alpha}"}))
        assert not any(m["type"] == "backlog" for m in await drain(mo, 1))


async def test_other_accounts_are_not_told_about_access_changes_or_deleted_servers(world):
    beta = world.ids["beta"]
    async with ws_for(world["moe"]) as mo, ws_for(world["ada"]) as ada, ws_for(world["bob"]) as bob:
        await world.admin.call("PUT", f"/api/users/{user_id(world, 'bob')}/servers", json={"servers": {beta: "player"}})
        got = dict(zip(("moe", "bob"), await asyncio.gather(drain(mo, 1), drain(bob, 1)), strict=True))
        assert any(m["type"] == "access" for m in got["bob"]) and not any(m["type"] == "access" for m in got["moe"])

        job = await world.admin.job(await world.admin.call("DELETE", f"/api/servers/{beta}", params={"final_backup": "false"}))
        assert job["status"] == "success"
        got = dict(zip(("moe", "ada", "bob"), await asyncio.gather(drain(mo, 1.5), drain(ada, 1.5), drain(bob, 1.5)), strict=True))
        assert any(m["type"] == "deleted" and m["data"]["id"] == beta for m in got["bob"]), "the accounts that had it are told"
        assert not about(got["moe"], beta) and not about(got["ada"], beta), "everyone else never hears of it"
        assert not any(m["type"] == "deleted" for m in got["moe"] + got["ada"])


def test_events_are_denied_unless_a_rule_allows_them():
    service = access.AccessService(_StubRepo({7: {"id": 7, "username": "x", "kind": "friend"}}, {7: {"s1": "moderator"}}))
    ev = lambda topic, kind, data: {"topic": topic, "type": kind, "data": data}  # noqa: E731
    assert service.filter_event(7, ev("servers", "server", {"id": "s2"})) is None
    assert service.filter_event(7, ev("servers", "stats", {"s2": {"cpu": 1}})) is None
    assert service.filter_event(7, ev("servers", "something-new", {"id": "s1"})) is None, "unknown event types are dropped"
    assert service.filter_event(7, ev("mystery", "x", {"server_id": "s1"})) is None, "and so are unknown topics"
    assert service.filter_event(7, ev("jobs", "job", {"server_id": None})) is None
    assert service.filter_event(7, ev("console:s2", "console", {})) is None
    assert service.filter_event(7, ev("console:s1", "console", {})) is not None
    assert service.filter_event(99, ev("console:s1", "console", {})) is None, "an account that no longer exists gets nothing"
    about_7 = ev("servers", "access", {"user_id": 7})
    assert service.filter_event(7, about_7) is not None and service.filter_event(8, about_7) is None, "access news is for its own account"
    assert service.filter_event(99, ev("servers", "access", {"user_id": 99})) is not None, "an account that is gone is still told, so its feed ends"


class _StubRepo:
    def __init__(self, users: dict[int, dict], assignments: dict[int, dict[str, str]]):
        self.users, self.assignments = users, assignments

    def get_user(self, user_id: int):
        return self.users.get(user_id)

    def user_assignments(self, user_id: int):
        return self.assignments.get(user_id, {})


async def test_a_failing_event_filter_does_not_break_whoever_publishes():
    from pumpkin_manager.events import EventBus

    bus = EventBus()
    healthy = bus.subscribe({"servers"})
    broken = bus.subscribe({"servers"}, user_id=1, view=lambda message: 1 / 0)
    bus.publish("servers", "server", {"id": "x"})
    assert healthy.queue.qsize() == 1 and broken.queue.qsize() == 0


async def test_an_open_live_feed_ends_when_its_account_is_reset_or_deleted(world):
    for name, action in (("pia", "reset"), ("moe", "delete")):
        uid = user_id(world, name)
        async with ws_for(world[name]) as ws:
            if action == "reset":
                resp = await world.admin.call("POST", f"/api/users/{uid}/reset-password")
            else:
                resp = await world.admin.call("DELETE", f"/api/users/{uid}")
            assert resp.status_code == 200
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 4401, f"{name} was {action}"


async def test_a_websocket_is_refused_while_the_password_is_temporary(live):
    admin, _ = live
    await admin.login()
    created = (await admin.call("POST", "/api/users", json={"username": "fresh", "servers": {}})).json()
    api = Api(admin.base)
    await api.login(created["temporary_password"], "fresh")
    with pytest.raises(InvalidStatus):
        async with ws_for(api):
            pass


# ------------------------------------------------------------------------------------ managing accounts
async def test_creating_an_account_gives_a_temporary_password_that_is_shown_once(world):
    alpha = world.ids["alpha"]
    created = await world.admin.call("POST", "/api/users", json={"username": "Kim_01", "servers": {alpha: "moderator"}})
    assert created.status_code == 201
    body = created.json()
    assert re.fullmatch(r"[a-zA-Z2-9]{4}-[a-zA-Z2-9]{4}-[a-zA-Z2-9]{4}", body["temporary_password"])
    assert not set("0O1lI") & set(body["temporary_password"]), "no look-alike characters"
    user = body["user"]
    assert user["username"] == "Kim_01" and user["kind"] == "friend" and user["must_change_password"] is True
    assert user["servers"] == {alpha: "moderator"} and user["last_login_at"] is None
    assert "password_hash" not in user and "temporary" not in json.dumps(user)

    stored = world.ctx.repo.get_user_by_name("kim_01")
    assert stored["password_hash"].startswith("scrypt$") and body["temporary_password"] not in stored["password_hash"]
    listing = (await world.admin.call("GET", "/api/users")).json()
    assert "temporary_password" not in json.dumps(listing) and "password_hash" not in json.dumps(listing)
    kim = next(u for u in listing if u["username"] == "Kim_01")
    assert kim == {**user}

    other = (await world.admin.call("POST", "/api/users", json={"username": "kim2", "servers": {}})).json()["temporary_password"]
    assert other != body["temporary_password"], "every account gets its own"
    assert any("created the account 'Kim_01'" in a["message"] for a in world.ctx.repo.list_activity(50))
    assert not any(body["temporary_password"] in a["message"] for a in world.ctx.repo.list_activity(200))


async def test_account_input_is_validated(world):
    alpha = world.ids["alpha"]
    bad_names = ["ab", "x" * 33, "has space", "semi;colon", "-leading", "emoji☃", "../etc", ""]
    for name in bad_names:
        resp = await world.admin.call("POST", "/api/users", json={"username": name, "servers": {}})
        assert resp.status_code in (422, 400), name
    assert (await world.admin.call("POST", "/api/users", json={"username": "okname", "servers": {"no-such": "player"}})).status_code == 422
    assert (await world.admin.call("POST", "/api/users", json={"username": "okname", "servers": {alpha: "owner"}})).status_code == 422
    assert (await world.admin.call("POST", "/api/users", json={"username": "okname", "servers": {alpha: "root"}})).status_code == 422
    assert world.ctx.repo.get_user_by_name("okname") is None, "a rejected request leaves nothing behind"
    for name in ("PIA", "pia", "ADMIN", "Admin"):
        assert (await world.admin.call("POST", "/api/users", json={"username": name, "servers": {}})).status_code == 409, name


async def test_the_administrators_own_account_cannot_be_managed_as_a_friend(world):
    admin_id = world.ctx.repo.get_user_by_name("admin")["id"]
    assert (await world.admin.call("DELETE", f"/api/users/{admin_id}")).status_code == 400
    assert (await world.admin.call("POST", f"/api/users/{admin_id}/reset-password")).status_code == 400
    assert (await world.admin.call("PUT", f"/api/users/{admin_id}/servers", json={"servers": {}})).status_code == 400
    assert (await world.admin.call("DELETE", "/api/users/99999")).status_code == 404
    assert (await world.admin.call("GET", "/api/servers")).status_code == 200


async def test_resetting_a_password_signs_the_account_out_and_asks_for_a_new_one(world):
    pia, uid = world["pia"], user_id(world, "pia")
    assert (await pia.call("GET", "/api/servers")).status_code == 200
    reset = await world.admin.call("POST", f"/api/users/{uid}/reset-password")
    assert reset.status_code == 200
    temporary = reset.json()["temporary_password"]
    assert (await pia.call("GET", "/api/servers")).status_code == 401, "the old session is gone"
    assert await login_status(world.admin.base, "pia", OWN_PASSWORD) == 401, "so is the old password"

    fresh = Api(world.admin.base)
    login = await fresh.login(temporary, "pia")
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    blocked = await fresh.call("GET", "/api/servers")
    assert blocked.status_code == 403 and blocked.json()["code"] == "password_change_required"
    await fresh.http.aclose()


async def test_deleting_an_account_ends_its_sessions_and_frees_the_name(world):
    mo, uid = world["moe"], user_id(world, "moe")
    assert (await world.admin.call("DELETE", f"/api/users/{uid}")).status_code == 200
    assert (await mo.call("GET", "/api/servers")).status_code == 401
    assert await login_status(world.admin.base, "moe", OWN_PASSWORD) == 401
    assert world.ctx.repo.user_assignments(uid) == {}
    assert (await world.admin.call("POST", "/api/users", json={"username": "moe", "servers": {}})).status_code == 201


# ------------------------------------------------------------------------------ the temporary password
async def test_a_temporary_password_can_only_be_used_to_choose_a_new_one(live):
    admin, ctx = live
    await admin.login()
    sid = (await admin.create_server("Gate"))["id"]
    created = (await admin.call("POST", "/api/users", json={"username": "newbie_01", "servers": {sid: "admin"}})).json()
    temporary = created["temporary_password"]

    device_a, device_b = Api(admin.base), Api(admin.base)
    login = await device_a.login(temporary, "newbie_01")
    await device_b.login(temporary, "newbie_01")
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True and login.json()["kind"] == "friend"
    assert ctx.repo.get_user_by_name("newbie_01")["last_login_at"], "the sign-in itself is recorded"

    # nothing works yet except looking at who you are, signing out and choosing a password
    me = (await device_a.call("GET", "/api/auth/me")).json()
    assert me["must_change_password"] is True and me["username"] == "newbie_01"
    for method, path, kwargs in (
        ("GET", "/api/servers", {}), ("GET", f"/api/servers/{sid}", {}), ("POST", f"/api/servers/{sid}/stop", {}),
        ("GET", "/api/activity", {}), ("GET", "/api/jobs", {}), ("GET", "/api/backups", {}), ("GET", "/api/versions", {}),
        ("GET", f"/api/servers/{sid}/files", {}), ("POST", f"/api/servers/{sid}/command", {"json": {"command": "list"}}),
    ):
        resp = await device_a.call(method, path, **kwargs)
        assert resp.status_code == 403 and resp.json()["code"] == "password_change_required", f"{method} {path}"

    # the new password has to be a real one
    async def attempt(current: str, new: str):
        return await device_a.call("POST", "/api/auth/password", json={"current": current, "new": new})

    assert (await attempt(temporary, "short")).status_code == 400
    assert (await attempt(temporary, temporary)).status_code == 400, "not the same password again"
    assert (await attempt(temporary, "NEWBIE_01")).status_code == 400, "not the username"
    assert (await attempt("wrong password", OWN_PASSWORD)).status_code == 400
    assert (await device_a.call("GET", "/api/servers")).status_code == 403, "still locked after the failed attempts"

    assert (await attempt(temporary, OWN_PASSWORD)).status_code == 200
    assert (await device_a.call("GET", "/api/auth/me")).json()["must_change_password"] is False
    listed = (await device_a.call("GET", "/api/servers")).json()
    assert [s["name"] for s in listed] == ["Gate"] and listed[0]["access"]["role"] == "admin"
    assert (await device_b.call("GET", "/api/auth/me")).status_code == 401, "other devices signed in with the temporary password are out"

    assert await login_status(admin.base, "newbie_01", temporary) == 401, "the temporary password is dead"
    assert await login_status(admin.base, "newbie_01", OWN_PASSWORD) == 200
    assert ctx.repo.get_user_by_name("newbie_01")["must_change_password"] == 0
    # choosing a password later on is the ordinary change, and needs the current one
    assert (await device_a.call("POST", "/api/auth/password", json={"current": OWN_PASSWORD, "new": "yet another one"})).status_code == 200


async def test_the_administrator_is_never_asked_to_change_a_password(live):
    admin, _ = live
    login = await admin.login()
    assert login.json()["must_change_password"] is False and login.json()["kind"] == "admin"
    assert (await admin.call("GET", "/api/servers")).status_code == 200


def test_the_command_line_reset_makes_a_friends_password_temporary(tmp_path, monkeypatch):
    from pumpkin_manager.__main__ import main

    settings = make_settings(tmp_path)

    async def prepare():
        app_ctx = AppContext(settings, verify_elf=False, background=False, release_source=None)
        await app_ctx.startup()
        await app_ctx.accounts.create("kim", {}, "admin")
        await app_ctx.shutdown()

    asyncio.run(prepare())
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("ADMIN_USER", "admin")
    assert main(["reset-password", "--user", "kim", "--password", "typed-by-the-owner"]) == 0
    assert main(["reset-password", "--password", "typed-by-the-owner-2"]) == 0

    db = Database(settings.db_path)
    try:
        assert db.one("SELECT must_change_password FROM users WHERE username='kim'")["must_change_password"] == 1
        assert db.one("SELECT must_change_password FROM users WHERE username='admin'")["must_change_password"] == 0
    finally:
        db.close()


# --------------------------------------------------------------------------------------------- database
def test_an_existing_database_is_upgraded_and_its_account_becomes_the_administrator(tmp_path):
    db = Database(tmp_path / "old.db")
    for statement in _split_script(SCHEMA_V1):
        db.execute(statement)
    db.execute("PRAGMA user_version = 1")
    db.execute("INSERT INTO users(username, password_hash, created_at) VALUES('root', 'x', '2026-01-01T00:00:00Z')")
    db.execute(
        "INSERT INTO servers(id, name, created_at, java_port, mgmt_port, mgmt_secret) VALUES('old', 'Old', '2026-01-01T00:00:00Z', 1, 2, 'z')"
    )
    db.migrate()
    row = db.one("SELECT * FROM users WHERE username='root'")
    assert row["kind"] == "admin" and row["must_change_password"] == 0
    assert db.scalar("PRAGMA user_version") == len(MIGRATIONS)
    assert db.scalar("SELECT COUNT(*) FROM user_servers") == 0
    server = db.one("SELECT engine, spec FROM servers WHERE id='old'")
    assert (server["engine"], server["spec"]) == ("pumpkin", "{}"), "a server from before modpacks is a Pumpkin server"
    db.close()


def test_assignments_follow_their_account_and_their_server(tmp_path):
    db = Database(tmp_path / "fk.db")
    db.migrate()
    now = "2026-01-01T00:00:00Z"
    db.execute("INSERT INTO users(id, username, password_hash, created_at, kind) VALUES(5, 'f', 'x', ?, 'friend')", (now,))
    db.execute(
        "INSERT INTO servers(id, name, created_at, java_port, mgmt_port, mgmt_secret) VALUES('s1', 'S1', ?, 1, 2, 'z')", (now,)
    )
    db.execute("INSERT INTO user_servers(user_id, server_id, role) VALUES(5, 's1', 'player')")
    db.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at, last_seen_at) VALUES('t', 5, 'c', ?, ?, ?)", (now, now, now))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute("INSERT INTO user_servers(user_id, server_id, role) VALUES(5, 's1', 'owner')")
    db.execute("DELETE FROM servers WHERE id='s1'")
    assert db.scalar("SELECT COUNT(*) FROM user_servers") == 0, "deleting the server removes the assignment"
    db.execute("INSERT INTO servers(id, name, created_at, java_port, mgmt_port, mgmt_secret) VALUES('s1', 'S1', ?, 1, 2, 'z')", (now,))
    db.execute("INSERT INTO user_servers(user_id, server_id, role) VALUES(5, 's1', 'player')")
    db.execute("DELETE FROM users WHERE id=5")
    assert db.scalar("SELECT COUNT(*) FROM user_servers") == 0 and db.scalar("SELECT COUNT(*) FROM sessions") == 0
    db.close()
