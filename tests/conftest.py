"""Shared fixtures: a fake GitHub release feed, a fake Pumpkin binary and a fully wired application."""

from __future__ import annotations

import asyncio
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fakes import FAKE, FakeReleaseSource

from pumpkin_manager.app import AppContext
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
