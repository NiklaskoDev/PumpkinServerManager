"""Shared fixtures: a fake GitHub release feed, a fake Pumpkin binary and a fully wired application."""

from __future__ import annotations

import asyncio
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fakes import FAKE, FakeReleaseSource

from pumpkin_manager.app import AppContext, create_app
from pumpkin_manager.config import Settings
from pumpkin_manager.servers import port_is_free


def free_port_block(size: int, low: int = 30000, high: int = 58000) -> int:
    for _ in range(500):
        base = random.randint(low, high - size)
        if all(port_is_free(p) and port_is_free(p, udp=True) for p in range(base, base + size)):
            return base
    raise RuntimeError("no free port block found")


def make_settings(root: Path) -> Settings:
    # Separate bands guarantee the Java, Bedrock and management pools can never overlap.
    java = free_port_block(12, 30000, 36000)
    bedrock = free_port_block(6, 36000, 42000)
    mgmt = free_port_block(30, 42000, 48000)
    return Settings(
        data_dir=root / "data", backup_dir=root / "backups", web_host="127.0.0.1", web_port=free_port_block(1, 48000, 58000),
        port_range=(java, java + 11), bedrock_port_range=(bedrock, bedrock + 5), mgmt_port_range=(mgmt, mgmt + 29),
        timezone="UTC", admin_user="admin", admin_password="correct horse battery", update_check_minutes=60,
        shutdown_timeout=20,
    )


@pytest.fixture
def source() -> FakeReleaseSource:
    feed = FakeReleaseSource()
    feed.publish("stable", "0.1.0")
    return feed


@pytest_asyncio.fixture
async def ctx(tmp_path: Path, source: FakeReleaseSource):
    """A started application context (no background loops) using the fake feed and fake binary."""
    app_ctx = AppContext(make_settings(tmp_path), release_source=source, verify_elf=False, background=False, stability_seconds=0.6)
    app_ctx.servers.command_builder = lambda rec, build: [sys.executable, "-u", str(FAKE), str(app_ctx.builds.binary_path(build.id))]
    await app_ctx.startup()
    try:
        yield app_ctx
    finally:
        await app_ctx.shutdown()


class Api:
    def __init__(self, base: str):
        self.base = base
        self.http = httpx.AsyncClient(base_url=base, timeout=30)
        self.csrf: str | None = None

    async def login(self, password: str = "correct horse battery", username: str = "admin") -> httpx.Response:
        resp = await self.http.post("/api/auth/login", json={"username": username, "password": password})
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


async def wait_until(predicate: Callable[[], Any | Awaitable[Any]], timeout: float = 30.0, interval: float = 0.05, what: str = "condition") -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(interval)


async def finish(ctx: AppContext, job: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    """Wait for a background job to end and return its final row."""
    def done() -> dict[str, Any] | None:
        row = ctx.repo.get_job(job["id"])
        return row if row and row["status"] != "running" else None

    return await wait_until(done, timeout, what=f"job {job['title']}")


async def create_server(ctx: AppContext, name: str = "Survival", **spec: Any):
    job = ctx.servers.submit_create({"name": name, "start": spec.pop("start", True), **spec})
    row = await finish(ctx, job)
    assert row["status"] == "success", row["message"]
    server = next(s for s in ctx.repo.list_servers() if s.name == name)
    return server


@pytest_asyncio.fixture
async def jvm(tmp_path: Path, source: FakeReleaseSource):
    """A started application context whose Java side talks to a fake world (see jvmworld.py). Yields (world, ctx)."""
    from jvmworld import JavaWorld

    world = JavaWorld()
    app_ctx = AppContext(
        make_settings(tmp_path), release_source=source, verify_elf=False, background=False, stability_seconds=0.6, **world.app_options()
    )
    world.attach(app_ctx)
    app_ctx.servers.command_builder = lambda rec, build: [sys.executable, "-u", str(FAKE), str(app_ctx.builds.binary_path(build.id))]
    await app_ctx.startup()
    try:
        yield world, app_ctx
    finally:
        await app_ctx.shutdown()


async def create_java_server(ctx: AppContext, name: str = "Modded", **spec: Any):
    """Create a modpack server from the fake Modrinth pack and wait for the job; returns the server record."""
    body = {"name": name, "engine": "java", "eula": True, "modpack": {"source": "modrinth", "project": "cool-pack"}, "start": spec.pop("start", True), **spec}
    row = await finish(ctx, ctx.servers.submit_create(body))
    assert row["status"] == "success", row["message"]
    return next(s for s in ctx.repo.list_servers() if s.name == name)


@pytest_asyncio.fixture
async def jvm_live(tmp_path, source):
    """The whole application over HTTP with the fake Java world behind it. Yields (world, api, ctx)."""
    from jvmworld import JavaWorld

    world = JavaWorld()
    settings = make_settings(tmp_path)
    app = create_app(settings, release_source=source, verify_elf=False, background=False, stability_seconds=0.6, **world.app_options())
    ctx = app.state.ctx
    world.attach(ctx)
    ctx.servers.command_builder = lambda rec, build: [sys.executable, "-u", str(FAKE), str(ctx.builds.binary_path(build.id))]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.web_port, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, 20, what="web server")
    api = Api(f"http://127.0.0.1:{settings.web_port}")
    try:
        yield world, api, ctx
    finally:
        await api.http.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 60)
