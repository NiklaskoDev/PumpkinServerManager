"""Modrinth: finding modpacks and their versions (https://docs.modrinth.com/api/). No account or key is needed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .net import DownloadError, ProgressCallback, download, get_json, new_client, require_https

API = "https://api.modrinth.com/v2"
# The only hosts a modpack may download from (Modrinth's own rule for .mrpack files).
ALLOWED_DOWNLOAD_HOSTS = frozenset({"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com"})
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
IMAGE_HOSTS = frozenset({"cdn.modrinth.com"})
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})  # no SVG: it could carry script
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def parse_project_reference(text: str) -> str:
    """A project id or slug from whatever was pasted: an id, a slug or a modrinth.com link."""
    text = text.strip()
    if "://" in text or text.startswith("modrinth.com"):
        parts = urlsplit(text if "://" in text else f"https://{text}")
        if (parts.hostname or "").lower() not in ("modrinth.com", "www.modrinth.com"):
            raise DownloadError("That is not a Modrinth link.")
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2 or segments[0] != "modpack":
            raise DownloadError("That Modrinth link is not a modpack page (it should look like modrinth.com/modpack/<name>).")
        text = segments[1]
    if not _IDENT_RE.match(text):
        raise DownloadError("A Modrinth modpack is named by its address (modrinth.com/modpack/<name>) or its short name.")
    return text


def pack_file(version: dict[str, Any]) -> dict[str, Any] | None:
    """The .mrpack of a version (its primary file if it has one)."""
    packs = [f for f in version.get("files", []) if str(f.get("filename", "")).lower().endswith(".mrpack")]
    return next((f for f in packs if f.get("primary")), packs[0] if packs else None)


def summarize_version(version: dict[str, Any]) -> dict[str, Any]:
    file = pack_file(version) or {}
    return {
        "id": version["id"], "number": version.get("version_number"), "name": version.get("name"),
        "type": version.get("version_type", "release"), "published": version.get("date_published"),
        "minecraft": list(version.get("game_versions", [])), "loaders": list(version.get("loaders", [])), "size": file.get("size", 0),
    }


class ModrinthClient:
    def __init__(self, client: httpx.AsyncClient | None = None, retry_delay: float = 1.5):
        self._client = client or new_client()
        self._own_client = client is None
        self._delay = retry_delay

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def search(self, query: str, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Modpacks that can run on a server, most downloaded first. (Client-only packs are left out.)"""
        facets = '[["project_type:modpack"],["server_side:required","server_side:optional"]]'
        params: dict[str, Any] = {"query": query[:100], "facets": facets, "limit": max(1, min(limit, 50)), "offset": max(0, offset)}
        params["index"] = "relevance" if query.strip() else "downloads"
        data = await get_json(self._client, f"{API}/search", params=params, what="Modrinth", delay=self._delay)
        hits = [
            {
                "id": h["project_id"], "slug": h.get("slug"), "title": h.get("title"), "description": h.get("description"),
                "icon_url": h.get("icon_url"), "downloads": h.get("downloads", 0), "minecraft": list(h.get("versions", []))[-3:],
                "loaders": [c for c in h.get("categories", []) if c in ("fabric", "forge", "neoforge", "quilt")],
                "server_side": h.get("server_side"),
            }
            for h in data.get("hits", [])
        ]
        return {"hits": hits, "total": data.get("total_hits", len(hits))}

    async def project(self, reference: str) -> dict[str, Any]:
        ident = parse_project_reference(reference)
        project = await get_json(self._client, f"{API}/project/{quote(ident)}", what="Modrinth", delay=self._delay)
        if project.get("project_type") != "modpack":
            raise DownloadError(f"'{project.get('title', ident)}' is not a modpack.")
        if project.get("server_side") == "unsupported":
            raise DownloadError(f"'{project.get('title', ident)}' is a client-only modpack and cannot run on a server.")
        return project

    async def versions(self, reference: str) -> list[dict[str, Any]]:
        """All versions of a project that have an .mrpack, newest first."""
        ident = parse_project_reference(reference)
        data = await get_json(
            self._client, f"{API}/project/{quote(ident)}/version", params={"include_changelog": "false"}, what="Modrinth", delay=self._delay,
        )
        listed = [v for v in data if v.get("status", "listed") in ("listed", "archived") and pack_file(v)]
        return sorted(listed, key=lambda v: v.get("date_published", ""), reverse=True)

    async def server_support(self, sha1s: list[str]) -> dict[str, str]:
        """What the projects behind files say about running on a server: {sha1: required | optional | unsupported}.

        Only files Modrinth knows by their SHA-1 are in the answer. Packs exported from a client launcher mark almost every
        mod "required" on the server, including the many that only work on a client, so the project's own word is what counts."""
        project_of: dict[str, str] = {}
        unique = sorted({s.lower() for s in sha1s})
        for start in range(0, len(unique), 400):
            found = await get_json(
                self._client, f"{API}/version_files", body={"hashes": unique[start:start + 400], "algorithm": "sha1"},
                what="Modrinth", delay=self._delay,
            )
            if isinstance(found, dict):
                for sha1, version in found.items():
                    project = version.get("project_id") if isinstance(version, dict) else None
                    if isinstance(project, str) and _PROJECT_ID_RE.match(project):
                        project_of[str(sha1).lower()] = project
        side: dict[str, str] = {}
        ids = sorted(set(project_of.values()))
        for start in range(0, len(ids), 100):
            projects = await get_json(
                self._client, f"{API}/projects", params={"ids": json.dumps(ids[start:start + 100], separators=(",", ":"))},
                what="Modrinth", delay=self._delay,
            )
            for project in projects if isinstance(projects, list) else []:
                if isinstance(project, dict) and project.get("server_side") in ("required", "optional", "unsupported"):
                    side[str(project.get("id"))] = project["server_side"]
        return {sha1: side[project] for sha1, project in project_of.items() if project in side}

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """A modpack's icon (from Modrinth's image host only), so the dashboard never has to contact another site itself."""
        require_https(url, IMAGE_HOSTS)
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise DownloadError(f"The icon is not available (HTTP {resp.status_code}).")
                kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in IMAGE_TYPES:
                    raise DownloadError("That address is not a picture.")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise DownloadError("The icon is unreasonably large.")
        except httpx.HTTPError as exc:
            raise DownloadError(f"The icon could not be loaded: {exc}") from exc
        return bytes(data), kind

    async def version(self, version_id: str) -> dict[str, Any]:
        if not _IDENT_RE.match(version_id):
            raise DownloadError("That is not a valid Modrinth version id.")
        return await get_json(self._client, f"{API}/version/{quote(version_id)}", what="Modrinth", delay=self._delay)

    async def download_pack(self, version: dict[str, Any], dest: Path, progress: ProgressCallback | None = None) -> None:
        file = pack_file(version)
        if file is None:
            raise DownloadError("That version has no .mrpack file.")
        hashes = file.get("hashes") or {}
        if not hashes.get("sha512"):
            raise DownloadError("Modrinth did not give a checksum for that modpack file, so it cannot be verified.")
        await download(
            self._client, file["url"], dest, size=file.get("size"), sha1=hashes.get("sha1"), sha512=hashes["sha512"],
            progress=progress, allowed_hosts=ALLOWED_DOWNLOAD_HOSTS, what="the modpack file", delay=self._delay,
        )
