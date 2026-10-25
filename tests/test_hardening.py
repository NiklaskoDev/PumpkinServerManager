"""Hardening: who may read the database, where a redirect may lead a download, and the browser-facing headers."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import stat

import httpx
import pytest
from conftest import make_settings
from jvmfakes import PNG_HEADER, FakeInternet

from pumpkin_manager.app import content_security_policy, create_app
from pumpkin_manager.config import Settings
from pumpkin_manager.db import Database
from pumpkin_manager.jvm.curseforge import CurseForgeClient
from pumpkin_manager.jvm.modrinth import ModrinthClient
from pumpkin_manager.jvm.net import BadDownloadError, DownloadError, download, get_text

# ----------------------------------------------------------------------------------------------- database files
posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows has no owner-only file mode")


def modes(directory, name: str) -> dict[str, int]:
    found = {suffix: stat.S_IMODE((directory / f"{name}{suffix}").stat().st_mode) for suffix in ("", "-wal", "-shm") if (directory / f"{name}{suffix}").exists()}
    assert "" in found
    return found


@posix_only
def test_the_database_is_readable_by_its_owner_only_whatever_the_umask(tmp_path):
    previous = os.umask(0o000)  # the most permissive a container can have
    try:
        db = Database(tmp_path / "manager.db")
        db.migrate()
        db.execute("INSERT INTO settings(key, value) VALUES('a', 'b')")
        found = modes(tmp_path, "manager.db")
        db.close()
    finally:
        os.umask(previous)
    assert found and set(found.values()) == {0o600}, found


@posix_only
def test_a_database_that_an_older_version_left_readable_is_closed_at_the_next_start(tmp_path):
    first = Database(tmp_path / "manager.db")
    first.migrate()
    first.close()
    for path in tmp_path.glob("manager.db*"):
        os.chmod(path, 0o644)
    second = Database(tmp_path / "manager.db")
    try:
        assert set(modes(tmp_path, "manager.db").values()) == {0o600}
    finally:
        second.close()


# ------------------------------------------------------------------------------------------ redirects of downloads
HELLO = b"PK\x03\x04 hello"
CDN = frozenset({"cdn.example"})


def redirect(to: str):
    return lambda request: httpx.Response(302, headers={"Location": to})


async def fetch(net: FakeInternet, url: str, tmp_path, allowed=CDN, **kwargs):
    await download(net.client(), url, tmp_path / "file", allowed_hosts=allowed, attempts=1, delay=0, **kwargs)
    return (tmp_path / "file").read_bytes()


async def test_a_redirect_to_a_host_that_is_not_allowed_is_never_followed(tmp_path):
    net = FakeInternet()
    net.route("https://cdn.example/pack.zip", redirect("https://evil.example/pack.zip"))
    net.blob("https://evil.example/pack.zip", HELLO)
    with pytest.raises(BadDownloadError, match="evil.example"):
        await fetch(net, "https://cdn.example/pack.zip", tmp_path)
    assert not (tmp_path / "file").exists() and not any("evil.example" in url for url in net.requests), "the request was never sent"


async def test_a_redirect_to_another_allowed_host_or_to_a_relative_address_is_followed(tmp_path):
    net = FakeInternet()
    net.route("https://cdn.example/a.zip", redirect("/b.zip"))
    net.route("https://cdn.example/b.zip", redirect("https://mirror.example/c.zip"))
    net.blob("https://mirror.example/c.zip", HELLO)
    assert await fetch(net, "https://cdn.example/a.zip", tmp_path, allowed=frozenset({"cdn.example", "mirror.example"})) == HELLO


@pytest.mark.parametrize("allowed", [CDN, None], ids=["with allowed hosts", "without"])
async def test_a_redirect_to_plain_http_is_never_followed(tmp_path, allowed):
    net = FakeInternet()
    net.route("https://cdn.example/pack.zip", redirect("http://cdn.example/plain.zip"))  # the fake ignores the scheme: another path
    net.blob("http://cdn.example/plain.zip", HELLO)
    with pytest.raises(BadDownloadError, match="only https"):
        await fetch(net, "https://cdn.example/pack.zip", tmp_path, allowed=allowed)
    assert not any(url.startswith("http://") for url in net.requests)


async def test_without_a_list_of_hosts_any_https_host_may_be_the_target_of_a_redirect(tmp_path):
    """Java comes from Adoptium, which sends the download on to GitHub and from there to its storage; the checksum is what is trusted."""
    net = FakeInternet()
    net.route("https://api.adoptium.example/binary", redirect("https://github.com/adoptium/jre.tar.gz"))
    net.route("https://github.com/adoptium/jre.tar.gz", redirect("https://release-assets.githubusercontent.com/jre.tar.gz"))
    net.blob("https://release-assets.githubusercontent.com/jre.tar.gz", HELLO)
    assert await fetch(net, "https://api.adoptium.example/binary", tmp_path, allowed=None, sha256=hashlib.sha256(HELLO).hexdigest()) == HELLO


async def test_github_may_hand_a_file_over_to_its_storage_only_where_github_is_a_permitted_source(tmp_path):
    net = FakeInternet()
    net.route("https://github.com/owner/repo/releases/download/1/mod.jar", redirect("https://release-assets.githubusercontent.com/mod.jar"))
    net.route("https://cdn.example/mod.jar", redirect("https://release-assets.githubusercontent.com/mod.jar"))
    net.blob("https://release-assets.githubusercontent.com/mod.jar", HELLO)
    allowed = frozenset({"github.com", "cdn.modrinth.com"})
    assert await fetch(net, "https://github.com/owner/repo/releases/download/1/mod.jar", tmp_path, allowed=allowed) == HELLO
    with pytest.raises(BadDownloadError, match="release-assets.githubusercontent.com"):
        await fetch(net, "https://cdn.example/mod.jar", tmp_path)  # github.com is not one of its sources
    with pytest.raises(BadDownloadError, match="not one of the allowed hosts"):
        await fetch(net, "https://release-assets.githubusercontent.com/mod.jar", tmp_path, allowed=allowed)  # never a place to start from


async def test_a_loop_of_redirects_ends(tmp_path):
    net = FakeInternet()
    net.route("https://cdn.example/loop", redirect("https://cdn.example/loop"))
    with pytest.raises(DownloadError, match="redirects too many times"):
        await fetch(net, "https://cdn.example/loop", tmp_path)


async def test_a_page_is_not_fetched_through_a_redirect_to_another_host():
    net = FakeInternet()
    net.route("https://hub.example/versions", redirect("https://evil.example/versions"))
    net.route("https://evil.example/versions", lambda request: httpx.Response(200, text="<html>evil</html>"))
    with pytest.raises(BadDownloadError, match="evil.example"):
        await get_text(net.client(), "https://hub.example/versions", allowed_hosts=frozenset({"hub.example"}), attempts=1)
    assert not any("evil.example" in url for url in net.requests)

    net.route("https://hub.example/ok", redirect("/versions2"))
    net.route("https://hub.example/versions2", lambda request: httpx.Response(200, text="fine"))
    assert await get_text(net.client(), "https://hub.example/ok", allowed_hosts=frozenset({"hub.example"}), attempts=1) == "fine"


@pytest.mark.parametrize("kind, host, make", [
    ("Modrinth", "cdn.modrinth.com", lambda client: ModrinthClient(client)),
    ("CurseForge", "media.forgecdn.net", lambda client: CurseForgeClient(client, lambda: "key")),
])
async def test_a_pack_icon_is_not_fetched_through_a_redirect_to_another_host(kind, host, make):
    net = FakeInternet()
    net.route(f"https://{host}/moved.png", redirect("https://evil.example/icon.png"))
    net.blob("https://evil.example/icon.png", PNG_HEADER + b"x" * 20, "image/png")
    net.blob(f"https://{host}/icon.png", PNG_HEADER + b"y" * 20, "image/png")
    net.route(f"https://{host}/same.png", redirect(f"https://{host}/icon.png"))
    icon = make(net.client())
    with pytest.raises(DownloadError):
        await icon.fetch_image(f"https://{host}/moved.png")
    assert not any("evil.example" in url for url in net.requests), kind
    data, content_type = await icon.fetch_image(f"https://{host}/same.png")
    assert data.startswith(PNG_HEADER) and content_type == "image/png"


# ------------------------------------------------------------------------------------------ browser-facing headers
def test_the_policy_names_the_host_of_the_page_for_the_live_socket_and_no_other():
    policy = content_security_policy("tower:8420")
    assert "connect-src 'self' ws://tower:8420 wss://tower:8420;" in policy
    assert not re.search(r"\bwss?:(?!//)", policy), "no 'ws:' or 'wss:' for every host"
    for part in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'"):
        assert part in policy
    assert "ws://192.168.1.5:8420" in content_security_policy("192.168.1.5:8420")


@pytest.mark.parametrize("hostile", [
    None, "", "evil.example; script-src *", "a b", "x' 'unsafe-inline", "host\nX-Injected: 1", "[::1]:8420", "-bad", "a" * 300, "host:99999999",
])
def test_a_host_header_that_is_not_a_host_cannot_change_the_policy(hostile):
    policy = content_security_policy(hostile)
    assert "connect-src 'self';" in policy and "ws://" not in policy and policy.count(";") == 7


def test_the_hsts_setting_is_read():
    assert Settings.from_env({}).hsts_max_age == 31536000
    assert Settings.from_env({"HSTS_MAX_AGE": "600"}).hsts_max_age == 600
    assert Settings.from_env({"HSTS_MAX_AGE": "0"}).hsts_max_age == 0
    assert Settings.from_env({"HSTS_MAX_AGE": "-5"}).hsts_max_age == 0


async def headers_of(tmp_path, url: str, *, max_age: int | None = None, headers: dict[str, str] | None = None) -> httpx.Headers:
    settings = make_settings(tmp_path)
    if max_age is not None:
        settings = dataclasses.replace(settings, hsts_max_age=max_age)
    app = create_app(settings, verify_elf=False, background=False, release_source=None)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4000))
        async with httpx.AsyncClient(transport=transport) as client:
            return (await client.get(url, headers=headers)).headers


async def test_hsts_is_sent_over_https_only_and_can_be_switched_off(tmp_path):
    secure = await headers_of(tmp_path / "a", "https://dashboard/api/health")
    assert secure["strict-transport-security"] == "max-age=31536000"
    assert "strict-transport-security" not in await headers_of(tmp_path / "b", "http://dashboard/api/health"), "a browser would ignore it over http"
    assert "strict-transport-security" not in await headers_of(tmp_path / "c", "https://dashboard/api/health", max_age=0)
    assert (await headers_of(tmp_path / "d", "https://dashboard/", max_age=600))["strict-transport-security"] == "max-age=600"


async def test_every_kind_of_answer_carries_the_policy_for_the_host_it_was_asked_on(tmp_path):
    for index, url in enumerate(("http://tower:8420/", "http://tower:8420/api/health", "http://tower:8420/static/js/main.js", "http://tower:8420/nope")):
        headers = await headers_of(tmp_path / str(index), url)
        assert "connect-src 'self' ws://tower:8420 wss://tower:8420;" in headers["content-security-policy"], url
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    hostile = await headers_of(tmp_path / "h", "http://dashboard/api/health", headers={"Host": "evil.example; script-src *"})
    assert "ws://" not in hostile["content-security-policy"]
