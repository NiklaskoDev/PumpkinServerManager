"""CurseForge: finding modpacks and turning them into something the installer can use (https://docs.curseforge.com/rest-api/).

CurseForge needs an API key (CURSEFORGE_API_KEY, or Settings in the dashboard): CurseForge hands them out on request and
its terms say a key is personal and not to be shared, so the manager never ships one. Three things differ from Modrinth:

* A CurseForge modpack zip only lists (project, file) number pairs. Their download addresses and checksums come from the API,
  in bulk. A mod whose author does not allow automatic downloads has no address; it is never fetched around the API but
  reported, with its page, so a person can add it by hand.
* Many packs come with the author's own server pack: a ready set of mods and configuration. When there is one it is the
  better source (the author chose it, and it does not depend on every mod being downloadable one by one).
* The API's terms ask for no cache of what it hands out: nothing from it is stored beyond what the server itself needs
  (the lock file with the installed files), and downloaded files are only held while they are installed.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from . import archive
from .loaders import LoaderError, LoaderSpec
from .mrpack import CURSEFORGE_DOWNLOAD_HOSTS, MAX_FILES, PackError, PackFile, PackIndex, safe_relative_path
from .net import DownloadError, ProgressCallback, download, get_json

log = logging.getLogger(__name__)

API = "https://api.curseforge.com/v1"
GAME_ID = 432  # Minecraft
CLASS_MODPACKS = 4471
CLASS_MODS = 6
RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
LOADER_TYPES = {1: "forge", 4: "fabric", 5: "quilt", 6: "neoforge"}
LOADER_NAMES = {"forge": "forge", "fabric": "fabric", "quilt": "quilt", "neoforge": "neoforge"}
IMAGE_HOSTS = frozenset({"media.forgecdn.net"})
PAGE_HOSTS = frozenset({"www.curseforge.com", "curseforge.com"})
# CurseForge's download servers want the API key (it tells them whose download it is, which the mod authors see in their statistics).
KEY_HOSTS = frozenset({"edge.forgecdn.net", "mediafilez.forgecdn.net"})
MANIFEST_NAME = "manifest.json"
BULK = 200  # ids per request to the bulk endpoints
MAX_MANIFEST_BYTES = 20 * 1024**2
_MC_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")
_FILE_NAME = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]{1,200}$")
_LOADER_ID = re.compile(r"^(forge|neoforge|fabric|quilt)-([0-9A-Za-z][0-9A-Za-z._+-]{0,63})$")
_LINK = re.compile(r"curseforge\.com/minecraft/modpacks/([a-z0-9][a-z0-9_-]*)", re.IGNORECASE)
KEY_HELP = (
    "CurseForge did not accept the API key. It has to be a key for the CurseForge API (it starts with $2a$10$ and is shown in "
    "console.curseforge.com), not an upload token. A new key can take a few minutes before it works."
)


class CurseForgeError(DownloadError):
    """CurseForge could not be asked, or did not accept the request; the message is safe to show to users."""


class SearchUnavailable(CurseForgeError):
    """This API key may not use CurseForge's search (it can still look up a pack by its project number)."""


def parse_project_reference(text: str) -> tuple[str, str]:
    """('id', '715572') or ('slug', 'all-the-mods-9') from a project number or a link to the modpack's page."""
    text = text.strip()
    if text.isdigit():
        return "id", text
    found = _LINK.search(text)
    if found:
        return "slug", found.group(1).lower()
    raise CurseForgeError(
        "Enter the modpack's CurseForge project number (the Project ID in the box on its CurseForge page) or a link to its page."
    )


def _hash(file: dict[str, Any], algo: int) -> str:
    return next((str(h.get("value", "")).lower() for h in file.get("hashes") or [] if h.get("algo") == algo), "")


def game_versions(file: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(Minecraft versions, mod loaders) a file is tagged with."""
    tags = [str(t) for t in file.get("gameVersions") or []]
    return [t for t in tags if _MC_VERSION.match(t)], [t.lower() for t in tags if t.lower() in LOADER_NAMES]


def version_name(pack: str, display: Any) -> str:
    """What to call a version: its file's display name without the ``.zip`` behind it and the pack's own name in front of it
    ("RLCraft 1.12.2 - Release v2.9.3.zip" of the pack "RLCraft" is "1.12.2 - Release v2.9.3"), as the dashboard shows the pack's
    name next to it anyway."""
    text = re.sub(r"\.zip$", "", str(display or "").strip(), flags=re.IGNORECASE)
    wanted = "".join(c for c in str(pack or "").lower() if c.isalnum())
    seen = end = 0
    if not wanted:
        return text
    for index, char in enumerate(text):
        if seen == len(wanted):
            break
        if char.isalnum():
            if char.lower() != wanted[seen]:
                return text  # it does not start with the pack's name
            seen += 1
        end = index + 1
    if seen < len(wanted):
        return text
    return text[end:].lstrip(" -_:|])}>–—").strip() or text  # what is left of the name's own punctuation goes too


def summarize_file(file: dict[str, Any], pack: str = "") -> dict[str, Any]:
    """A pack file in the shape the dashboard shows versions in (the same as for Modrinth)."""
    minecraft, loaders = game_versions(file)
    return {
        "id": str(file["id"]), "number": version_name(pack, file.get("displayName") or file.get("fileName")), "name": file.get("displayName"),
        "type": RELEASE_TYPES.get(file.get("releaseType"), "release"), "published": file.get("fileDate"), "minecraft": minecraft,
        "loaders": loaders, "size": file.get("fileLength") or 0, "server_pack": bool(file.get("serverPackFileId")),
    }


def summarize_project(mod: dict[str, Any]) -> dict[str, Any]:
    """A search hit / popular pack, shaped like the Modrinth ones."""
    indexes = mod.get("latestFilesIndexes") or []
    minecraft = sorted({str(i["gameVersion"]) for i in indexes if _MC_VERSION.match(str(i.get("gameVersion", "")))},
                       key=lambda v: [int(p) for p in v.split(".")])
    loaders = sorted({LOADER_TYPES[i["modLoader"]] for i in indexes if i.get("modLoader") in LOADER_TYPES})
    logo = mod.get("logo") or {}
    return {
        "id": str(mod["id"]), "slug": mod.get("slug"), "title": mod.get("name"), "description": mod.get("summary"),
        "icon_url": logo.get("thumbnailUrl") or logo.get("url"), "downloads": mod.get("downloadCount", 0), "minecraft": minecraft[-3:],
        "loaders": loaders, "server_side": None, "source": "curseforge",
    }


@dataclass
class CfManifest:
    """What a CurseForge modpack zip says: its name, the Minecraft and loader versions, and the files as (project, file) pairs."""

    name: str
    version: str
    author: str
    minecraft: str
    loader: LoaderSpec
    files: list[tuple[int, int]]
    overrides: str


def read_manifest(pack: Path) -> CfManifest:
    """Parse and validate ``manifest.json`` of a CurseForge modpack zip (untrusted input)."""
    try:
        with zipfile.ZipFile(pack) as zf:
            try:
                info = zf.getinfo(MANIFEST_NAME)
            except KeyError:
                raise PackError("That file is not a CurseForge modpack (it has no manifest.json).") from None
            if info.file_size > MAX_MANIFEST_BYTES:
                raise PackError("The modpack's manifest is unreasonably large.")
            data = json.loads(zf.read(info).decode("utf-8-sig"))
    except zipfile.BadZipFile:
        raise PackError("That file is not a CurseForge modpack (it is not a valid zip file).") from None
    except (ValueError, UnicodeDecodeError):
        raise PackError("The modpack's manifest.json cannot be read.") from None
    if not isinstance(data, dict) or data.get("manifestType") != "minecraftModpack" or data.get("manifestVersion") != 1:
        raise PackError("This is not a Minecraft modpack in a format this manager understands (manifestType minecraftModpack, version 1).")
    minecraft_info = data.get("minecraft") if isinstance(data.get("minecraft"), dict) else {}
    minecraft = str(minecraft_info.get("version") or "")
    loaders = [m for m in minecraft_info.get("modLoaders") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
    chosen = next((m for m in loaders if m.get("primary")), loaders[0] if loaders else None)
    if not minecraft or chosen is None:
        raise PackError("The modpack must name one Minecraft version and one mod loader (Forge, NeoForge, Fabric or Quilt).")
    found = _LOADER_ID.match(chosen["id"])
    if not found:
        raise PackError(f"The modpack uses a mod loader this manager cannot install ({chosen['id']!r}); it supports Forge, NeoForge, Fabric and Quilt.")
    try:
        loader = LoaderSpec(found.group(1), found.group(2), minecraft)
    except LoaderError as exc:
        raise PackError(str(exc)) from exc
    raw_files = data.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
        raise PackError(f"The modpack lists no files or more than {MAX_FILES}.")
    files: list[tuple[int, int]] = []
    for item in raw_files:
        project, file = (item.get("projectID"), item.get("fileID")) if isinstance(item, dict) else (None, None)
        if not all(isinstance(n, int) and not isinstance(n, bool) and 0 < n < 2**31 for n in (project, file)):
            raise PackError("The modpack's file list is damaged.")
        files.append((project, file))
    overrides = data.get("overrides") if isinstance(data.get("overrides"), str) else "overrides"
    overrides = safe_relative_path(overrides, what="The modpack's configuration folder")
    return CfManifest(
        name=str(data.get("name") or "Modpack")[:120], version=str(data.get("version") or "")[:120],
        author=str(data.get("author") or "")[:120], minecraft=minecraft, loader=loader, files=files, overrides=overrides,
    )


def is_curseforge_pack(pack: Path) -> bool:
    """Whether a zip is a CurseForge modpack (it has a manifest.json and no Modrinth index)."""
    try:
        with zipfile.ZipFile(pack) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return MANIFEST_NAME in names and "modrinth.index.json" not in names


def summary_of_upload(pack: Path) -> dict[str, Any]:
    """What the wizard shows about an uploaded CurseForge zip (the files themselves are looked up when the server is created)."""
    manifest = read_manifest(pack)
    return {
        "name": manifest.name, "version": manifest.version, "summary": "", "minecraft": manifest.minecraft,
        "loader": {"kind": manifest.loader.kind, "version": manifest.loader.version, "label": manifest.loader.label},
        "mods": len(manifest.files), "download_bytes": 0, "manual": 0, "source": "curseforge",
    }


class CurseForgeClient:
    def __init__(self, client: httpx.AsyncClient, key_getter: Callable[[], str | None], retry_delay: float = 1.5):
        self._client = client
        self._key = key_getter
        self._delay = retry_delay
        self._search_allowed: bool | None = None  # learned from the first search: some keys may not use it
        self._searched_with: str | None = None

    # ----------------------------------------------------------------------------------------------------- plumbing
    @property
    def enabled(self) -> bool:
        return bool(self._key())

    @property
    def search_allowed(self) -> bool | None:
        """True or False once known, None before the first search."""
        return self._search_allowed if self._searched_with == self._key() else None

    def _headers(self, key: str | None = None) -> dict[str, str]:
        key = key or self._key()
        if not key:
            raise CurseForgeError(
                "CurseForge packs need a CurseForge API key. Add yours in Settings (or set CURSEFORGE_API_KEY); the README says how to get one."
            )
        return {"x-api-key": key, "Accept": "application/json"}  # sent with these requests only, never to a download address

    async def _request(self, path: str, *, params: dict[str, Any] | None = None, body: Any = None, key: str | None = None) -> Any:
        """One call to the API; CurseForge's answer as it is, and its refusals explained."""
        try:
            return await get_json(
                self._client, f"{API}{path}", params=params, body=body, headers=self._headers(key), what="CurseForge", delay=self._delay,
            )
        except DownloadError as exc:
            if exc.status in (401, 403):
                raise CurseForgeError(KEY_HELP) from None
            if exc.status == 404:
                raise CurseForgeError("CurseForge does not know that modpack or file.") from None
            raise CurseForgeError(str(exc)) from None

    async def _call(self, path: str, *, params: dict[str, Any] | None = None, body: Any = None, key: str | None = None) -> Any:
        data = await self._request(path, params=params, body=body, key=key)
        return data.get("data") if isinstance(data, dict) else None

    def download_headers(self, url: str) -> dict[str, str]:
        """What to send with a download from ``url``: the key to CurseForge's own download servers, nothing to any other address."""
        key = self._key()
        return {"x-api-key": key} if key and (urlsplit(url).hostname or "").lower() in KEY_HOSTS else {}

    async def check_key(self, key: str | None = None) -> None:
        """Ask for something small to see whether CurseForge accepts the key (a new one when ``key`` is given)."""
        await self._call(f"/games/{GAME_ID}", key=key)

    # --------------------------------------------------------------------------------------------------------- browse
    async def search(self, query: str, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """Modpacks matching a text, most popular first. Raises SearchUnavailable when the key may not search."""
        params = {
            "gameId": GAME_ID, "classId": CLASS_MODPACKS, "searchFilter": query[:100], "sortField": 2, "sortOrder": "desc",
            "index": max(0, offset), "pageSize": max(1, min(limit, 50)),
        }
        try:
            data = await get_json(self._client, f"{API}/mods/search", params=params, headers=self._headers(), what="CurseForge", delay=self._delay)
        except DownloadError as exc:
            if exc.status in (401, 403):
                self._search_allowed, self._searched_with = False, self._key()
                raise SearchUnavailable(
                    "CurseForge does not let this API key search by name, so packs are added by their project number "
                    "(the Project ID in the box on the pack's CurseForge page)."
                ) from None
            raise CurseForgeError(str(exc)) from None
        self._search_allowed, self._searched_with = True, self._key()
        hits = [summarize_project(m) for m in (data or {}).get("data", []) if isinstance(m, dict) and m.get("classId") == CLASS_MODPACKS]
        return {"hits": hits, "total": ((data or {}).get("pagination") or {}).get("totalCount", len(hits))}

    async def featured(self) -> list[dict[str, Any]]:
        """The modpacks CurseForge itself features or lists as popular or recently updated (a short list, but it needs no search)."""
        data = await self._call("/mods/featured", body={"gameId": GAME_ID, "excludedModIds": [], "gameVersionTypeId": None})
        seen: dict[int, dict[str, Any]] = {}
        for group in ("featured", "popular", "recentlyUpdated"):
            for mod in (data or {}).get(group, []) if isinstance(data, dict) else []:
                if isinstance(mod, dict) and mod.get("classId") == CLASS_MODPACKS and mod["id"] not in seen:
                    seen[mod["id"]] = mod
        return [summarize_project(m) for m in sorted(seen.values(), key=lambda m: m.get("downloadCount", 0), reverse=True)]

    async def project(self, reference: str) -> dict[str, Any]:
        """A modpack by project number, or by the address of its page (which needs the search the key may not have)."""
        kind, value = parse_project_reference(reference)
        if kind == "slug":
            params = {"gameId": GAME_ID, "classId": CLASS_MODPACKS, "slug": value}
            try:
                found = await get_json(self._client, f"{API}/mods/search", params=params, headers=self._headers(), what="CurseForge", delay=self._delay)
            except DownloadError as exc:
                if exc.status in (401, 403):
                    raise SearchUnavailable(
                        "CurseForge does not let this API key look a pack up by its page address. Enter the Project ID instead: it is "
                        "in the box on the right of the pack's CurseForge page."
                    ) from None
                raise CurseForgeError(str(exc)) from None
            mods = [m for m in (found or {}).get("data", []) if isinstance(m, dict) and m.get("slug") == value]
            if not mods:
                raise CurseForgeError("CurseForge has no modpack at that address.")
            mod = mods[0]
        else:
            mod = await self._call(f"/mods/{int(value)}")
        if not isinstance(mod, dict) or mod.get("classId") != CLASS_MODPACKS:
            raise CurseForgeError(f"'{(mod or {}).get('name', value)}' is not a modpack.")
        return mod

    async def files(self, mod_id: int | str) -> list[dict[str, Any]]:
        """Every file of a modpack that can be installed (the server packs that come with them are left out), newest first."""
        found: list[dict[str, Any]] = []
        index = 0
        while index < 500:
            page = await self._request(f"/mods/{int(mod_id)}/files", params={"index": index, "pageSize": 50})
            batch = [f for f in (page or {}).get("data", []) if isinstance(f, dict)]
            found += batch
            total = ((page or {}).get("pagination") or {}).get("totalCount", len(found))
            index += 50
            if not batch or index >= total:
                break
        usable = [f for f in found if not f.get("isServerPack") and f.get("isAvailable", True)]
        return sorted(usable, key=lambda f: f.get("fileDate", ""), reverse=True)

    async def file(self, mod_id: int | str, file_id: int | str) -> dict[str, Any]:
        data = await self._call(f"/mods/{int(mod_id)}/files/{int(file_id)}")
        if not isinstance(data, dict):
            raise CurseForgeError("CurseForge sent something unexpected for that file.")
        return data

    async def resolve_files(self, file_ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for start in range(0, len(file_ids), BULK):
            data = await self._call("/mods/files", body={"fileIds": file_ids[start:start + BULK]})
            for file in data or []:
                if isinstance(file, dict) and isinstance(file.get("id"), int):
                    out[file["id"]] = file
        return out

    async def resolve_projects(self, mod_ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for start in range(0, len(mod_ids), BULK):
            data = await self._call("/mods", body={"modIds": mod_ids[start:start + BULK], "filterPcOnly": True})
            for mod in data or []:
                if isinstance(mod, dict) and isinstance(mod.get("id"), int):
                    out[mod["id"]] = mod
        return out

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """A modpack's logo (from CurseForge's image host only), so the dashboard never has to contact another site itself."""
        from .modrinth import IMAGE_TYPES, MAX_IMAGE_BYTES
        from .net import require_https

        require_https(url, IMAGE_HOSTS)
        try:
            async with self._client.stream("GET", url) as resp:  # no key goes along
                if resp.status_code != 200:
                    raise CurseForgeError(f"The logo is not available (HTTP {resp.status_code}).")
                kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in IMAGE_TYPES:
                    raise CurseForgeError("That address is not a picture.")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise CurseForgeError("The logo is unreasonably large.")
        except httpx.HTTPError as exc:
            raise CurseForgeError(f"The logo could not be loaded: {exc}") from exc
        return bytes(data), kind

    # ------------------------------------------------------------------------------------------------------ download
    async def download(self, file: dict[str, Any], dest: Path, *, what: str, progress: ProgressCallback | None = None) -> None:
        """Download a file CurseForge describes, checked against the checksum it gave."""
        url = file.get("downloadUrl")
        if not isinstance(url, str) or not url:
            raise CurseForgeError(
                f"The author of {what} does not allow it to be downloaded automatically. Download it from CurseForge by hand and "
                "upload it here instead."
            )
        sha1 = _hash(file, 1)
        if not sha1:
            raise CurseForgeError(f"CurseForge gave no checksum for {what}, so it cannot be verified.")
        declared = int(file.get("fileLength") or 0)
        ceiling = None if declared <= 0 else min(archive.MAX_SERVER_PACK_BYTES, max(2 * declared, 64 * 1024**2))
        try:
            await download(
                self._client, url, dest, sha1=sha1, progress=progress, allowed_hosts=CURSEFORGE_DOWNLOAD_HOSTS, what=what,
                max_size=ceiling, delay=self._delay, headers_for=self.download_headers,
            )
        except DownloadError as exc:
            if exc.status in (401, 403):
                raise CurseForgeError(f"CurseForge refused the download of {what}. Its download servers want the API key: check the key in Settings.") from None
            raise

    # -------------------------------------------------------------------------------------------------- the pack index
    async def build_index(
        self, manifest: CfManifest, info: dict[str, Any], *, server_pack: Path | None, say: Callable[[str], None],
    ) -> PackIndex:
        """A PackIndex for the pack: from the author's server pack when there is one, otherwise from the list of files."""
        index = PackIndex(
            name=str(info.get("title") or manifest.name), version_id=info.get("version_number") or manifest.version, summary="", minecraft=manifest.minecraft,
            loader=manifest.loader, source="curseforge", override_prefixes=(manifest.overrides.rstrip("/") + "/",),
        )
        if server_pack is not None:
            pack = archive.ServerPack(server_pack)
            if pack.usable:
                index.archive = pack
                index.archive_notes = dict(pack.skipped)
                say(f"Using the pack's own server files ({pack.mod_count} mods)")
                return index
            say("The pack's server files hold no mods, so the list of mods is used instead")
        say(f"Looking up the {len(manifest.files)} files of the pack on CurseForge")
        index.files, skipped = await self._files_of(manifest)
        if skipped:
            say(f"{skipped} files are not mods (resource packs, shaders, ...) and are left out")
        return index

    async def _files_of(self, manifest: CfManifest) -> tuple[list[PackFile], int]:
        files = await self.resolve_files([f for _, f in manifest.files])
        projects = await self.resolve_projects(sorted({p for p, _ in manifest.files}))
        gone = [f for _, f in manifest.files if f not in files]
        if gone:
            raise PackError(
                f"{len(gone)} files of the modpack no longer exist on CurseForge (for example file {gone[0]}), so it cannot be installed."
            )
        out: list[PackFile] = []
        seen: set[str] = set()
        skipped = 0
        for project_id, file_id in manifest.files:
            file, mod = files[file_id], projects.get(project_id) or {}
            if mod.get("classId") not in (CLASS_MODS, None):
                skipped += 1  # resource packs, shaders, worlds: nothing a server needs
                continue
            made = pack_file_of(file, mod, project_id)
            if made is None:
                skipped += 1
                continue
            if made.path.lower() in seen:
                raise PackError(f"The modpack lists {made.path} twice.")
            seen.add(made.path.lower())
            out.append(made)
        return out, skipped


def clean_page(address: Any) -> str:
    """An address people may be sent to, as long as it really is a page on CurseForge (addresses from packs and from the API are not
    trusted): the same address without anything that could surprise, or "" for any other."""
    if not isinstance(address, str):
        return ""
    try:
        parts = urlsplit(address.strip())
        plain = not (parts.username or parts.password or parts.port)
    except ValueError:
        return ""
    if parts.scheme != "https" or (parts.hostname or "").lower() not in PAGE_HOSTS or not plain:
        return ""
    return f"https://{parts.hostname.lower()}{quote(parts.path.rstrip('/'), safe='/-_.~%')}"


def mod_page(site: Any, file_id: int) -> str:
    """The page of one file of a mod on CurseForge, for people to open (from the mod's page address that the API gives)."""
    page = clean_page(site)
    return f"{page}/files/{file_id}" if page else ""


def pack_file_of(file: dict[str, Any], mod: dict[str, Any], project_id: int) -> PackFile | None:
    """One CurseForge file as a file of a pack; None when it is no mod jar. Everything in it is untrusted."""
    name = str(file.get("fileName") or "")
    if not name.lower().endswith(".jar"):
        return None
    if not _FILE_NAME.match(name):
        raise PackError(f"CurseForge lists a mod file with a name that cannot be used ({name[:60]!r}), so the modpack was refused.")
    sha1 = _hash(file, 1)
    if not sha1:
        raise PackError(f"CurseForge gave no checksum for {name}, so it could not be verified.")
    path = safe_relative_path(f"mods/{name}", what="A mod in the modpack")
    url = file.get("downloadUrl")
    allowed = isinstance(url, str) and urlsplit(url).scheme == "https" and (urlsplit(url).hostname or "").lower() in CURSEFORGE_DOWNLOAD_HOSTS
    tags = [str(t) for t in file.get("gameVersions") or []]
    return PackFile(
        path=path, sha1=sha1, sha512="", size=int(file.get("fileLength") or 0), urls=[url] if allowed else [], server="required",
        project=f"cf:{project_id}",
        requires=tuple(f"cf:{d['modId']}" for d in file.get("dependencies") or [] if isinstance(d, dict) and d.get("relationType") == 3),
        client_hint="Client" in tags and "Server" not in tags,
        name=str(mod.get("name") or name)[:120], page=mod_page((mod.get("links") or {}).get("websiteUrl"), int(file["id"])), manual=not allowed,
    )


def page_link(slug: str | None, project_id: str) -> str:
    """Where the pack lives on CurseForge (for the dashboard)."""
    return f"https://www.curseforge.com/minecraft/modpacks/{quote(slug)}" if slug else f"https://www.curseforge.com/projects/{quote(project_id)}"
