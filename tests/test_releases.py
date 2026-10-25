"""GitHub release discovery and downloads, driven by real release data captured from the Pumpkin repo."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from pumpkin_manager.releases import (
    BadBuildError,
    GitHubReleaseSource,
    ReleaseError,
    asset_name_for_machine,
    build_id_for,
    parse_stable_tag,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "github_releases.json").read_text(encoding="utf-8"))


class FakeGitHub:
    def __init__(self, releases=None):
        self.releases = copy.deepcopy(releases or FIXTURE)
        self.requests: list[httpx.Request] = []
        self.etag = '"v1"'
        self.rate_limited = False
        self.download_body = b"x" * 2048

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host != "api.github.com":
            return httpx.Response(200, content=self.download_body, headers={"content-length": str(len(self.download_body))})
        if self.rate_limited:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1799999999"})
        if request.headers.get("if-none-match") == self.etag:
            return httpx.Response(304)
        headers = {"etag": self.etag}
        if path.endswith("/releases"):
            return httpx.Response(200, json=self.releases, headers=headers)
        tag = path.rsplit("/", 1)[-1].replace("%2B", "+")
        for rel in self.releases:
            if rel["tag_name"] == tag:
                return httpx.Response(200, json=rel, headers=headers)
        return httpx.Response(404)


def source_for(gh: FakeGitHub, machine="x86_64", token=None) -> GitHubReleaseSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(gh.handler), follow_redirects=True)
    return GitHubReleaseSource("Pumpkin-MC/Pumpkin", token_getter=lambda: token, machine=machine, client=client)


def test_asset_selection_by_architecture():
    assert asset_name_for_machine("x86_64") == "pumpkin-X64-Linux-musl"
    assert asset_name_for_machine("AMD64") == "pumpkin-X64-Linux-musl"
    assert asset_name_for_machine("aarch64") == "pumpkin-ARM64-Linux-musl"
    with pytest.raises(ReleaseError, match="Unsupported"):
        asset_name_for_machine("riscv64")


def test_stable_tag_parsing():
    assert parse_stable_tag("0.2.0+26.3-26.51") == ("0.2.0", "26.3")
    assert parse_stable_tag("0.1.0-dev+26.2-26.45") == ("0.1.0-dev", "26.2")
    assert parse_stable_tag("something-else") == (None, None)
    assert build_id_for("0.2.0+26.3-26.51", "sha256:798fc1aee4eb99") == "0.2.0_26.3-26.51-798fc1aee4eb"


async def test_latest_stable_uses_newest_real_release_and_its_digest():
    build = await source_for(FakeGitHub()).latest("stable")
    assert build.tag == "0.2.0+26.3-26.51" and build.version == "0.2.0" and build.mc_version == "26.3"
    assert build.digest == "798fc1aee4eb99" or build.digest.startswith("798fc1aee4eb")
    assert build.asset_name == "pumpkin-X64-Linux-musl" and build.size == 126400904
    assert build.channel == "stable" and build.download_url.endswith("pumpkin-X64-Linux-musl")


async def test_rolling_channels_are_identified_by_digest():
    gh = FakeGitHub()
    nightly = await source_for(gh).latest("nightly")
    canary = await source_for(gh).latest("canary")
    assert nightly.tag == "nightly" and canary.tag == "canary"
    assert nightly.digest != canary.digest and len(nightly.digest) == 64
    assert nightly.id != canary.id

    before = nightly.id
    for rel in gh.releases:
        if rel["tag_name"] == "nightly":
            for asset in rel["assets"]:
                if asset["name"] == "pumpkin-X64-Linux-musl":
                    asset["digest"] = "sha256:" + "ab" * 32  # the binary behind the same tag was replaced
    gh.etag = '"v2"'
    again = await source_for(gh).latest("nightly")
    assert again.tag == "nightly" and again.id != before


async def test_arm64_machines_get_the_arm_binary():
    build = await source_for(FakeGitHub(), machine="aarch64").latest("stable")
    assert build.asset_name == "pumpkin-ARM64-Linux-musl" and build.size == 110910240


async def test_release_whose_assets_are_still_uploading_is_skipped():
    gh = FakeGitHub()
    newest = copy.deepcopy(gh.releases[2])
    newest.update(tag_name="0.3.0+26.4-26.60", published_at="2026-10-09T00:00:00Z", assets=[])
    gh.releases.insert(0, newest)
    build = await source_for(gh).latest("stable")
    assert build.tag == "0.2.0+26.3-26.51", "an empty fresh release must not be offered yet"


async def test_stable_listing_excludes_rolling_prereleases_and_orders_newest_first():
    builds = await source_for(FakeGitHub()).list_stable()
    assert [b.tag for b in builds] == ["0.2.0+26.3-26.51", "0.1.0-dev+26.2-26.45"]


async def test_conditional_requests_reuse_the_cache():
    gh = FakeGitHub()
    src = source_for(gh)
    first = await src.latest("stable")
    second = await src.latest("stable")
    assert first == second
    api = [r for r in gh.requests if r.url.host == "api.github.com"]
    assert len(api) == 2 and "if-none-match" not in api[0].headers and api[1].headers["if-none-match"] == '"v1"'


async def test_rate_limit_has_a_helpful_message():
    gh = FakeGitHub()
    gh.rate_limited = True
    with pytest.raises(ReleaseError, match="rate limit.*token"):
        await source_for(gh).latest("stable")


async def test_missing_release_is_reported():
    with pytest.raises(ReleaseError, match="404|find"):
        await source_for(FakeGitHub()).by_tag("9.9.9")


async def test_token_is_sent_to_the_api_but_never_to_downloads(tmp_path):
    gh = FakeGitHub()
    src = source_for(gh, token="ghp_secret")
    build = await src.latest("stable")
    gh.download_body = b"x" * 2048
    api_requests = [r for r in gh.requests if r.url.host == "api.github.com"]
    assert all(r.headers.get("authorization") == "Bearer ghp_secret" for r in api_requests)

    small = copy.copy(build)
    small.size, small.digest = 2048, hashlib.sha256(b"x" * 2048).hexdigest()
    await src.download(small, tmp_path / "bin")
    downloads = [r for r in gh.requests if r.url.host != "api.github.com"]
    assert downloads and all("authorization" not in r.headers for r in downloads)
    assert (tmp_path / "bin").read_bytes() == b"x" * 2048


async def test_download_rejects_checksum_mismatch_and_truncation(tmp_path):
    gh = FakeGitHub()
    src = source_for(gh)
    build = await src.latest("stable")
    wrong = copy.copy(build)
    wrong.size = 2048  # right size, wrong hash
    with pytest.raises(ReleaseError, match="Checksum mismatch"):
        await src.download(wrong, tmp_path / "bin")

    short = copy.copy(build)
    short.size = 5000  # server sent fewer bytes than announced
    with pytest.raises(ReleaseError, match="incomplete"):
        await src.download(short, tmp_path / "bin2")


async def test_a_build_without_a_published_sha256_is_listed_but_never_downloaded(tmp_path):
    gh = FakeGitHub()
    for rel in gh.releases:
        if rel["tag_name"] == "nightly":
            for asset in rel["assets"]:
                asset.pop("digest", None)  # GitHub gave no digest, and the release has no checksums file either
    src = source_for(gh)
    build = await src.latest("nightly")
    assert not build.has_verifiable_digest and build.digest.startswith("unverified-"), "it can still be told apart from other builds"

    gh.requests.clear()
    with pytest.raises(BadBuildError, match="no SHA-256.*not downloaded"):
        await src.download(build, tmp_path / "bin")
    assert gh.requests == [] and not (tmp_path / "bin").exists(), "nothing that cannot be checked is fetched, let alone run"
