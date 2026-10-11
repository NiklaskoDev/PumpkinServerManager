"""A fake internet for the Java side: Adoptium, Mojang, Modrinth, the loaders' download sites.

Everything is served from memory through ``httpx.MockTransport``, so tests (and the local dev dashboard) can create
modpack servers without touching the network. The services take an ``httpx.AsyncClient``, so they cannot tell the
difference.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

JRE_RELEASE = "jdk-21.0.12.1+1"
PNG_HEADER = bytes([0x89]) + b"PNG\r\n" + bytes([0x1A, 0x0A])  # what every PNG file starts with


def jre_archive(kind: str = "tar.gz", release: str = JRE_RELEASE, *, evil_member: str | None = None) -> bytes:
    """What an Adoptium JRE archive looks like inside: one folder with bin/java (and java.exe, so any OS can use it)."""
    top = f"{release}-jre"
    files = {
        f"{top}/bin/java": b"#!/bin/sh\necho fake java\n", f"{top}/bin/java.exe": b"MZ fake java",
        f"{top}/release": b'JAVA_VERSION="21.0.12"\n', f"{top}/lib/modules": b"\0" * 2048,
    }
    if evil_member:
        files[evil_member] = b"gotcha"
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if "/bin/" in name else 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeJava:
    """Stands in for ``java -jar <installer> ...``: leaves behind what the real installers leave (as seen in their documentation)."""

    def __init__(self, exit_code: int = 0, output: tuple[str, ...] = ("Downloading libraries...", "Done")) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.exit_code = exit_code
        self.output = output

    async def __call__(self, argv: list[str], cwd, on_line) -> tuple[int, list[str]]:
        self.calls.append((list(argv), str(cwd)))
        for line in self.output:
            on_line(line)
        if self.exit_code:
            return self.exit_code, list(self.output)
        jar = argv[2] if len(argv) > 2 and argv[1] == "-jar" else ""
        if jar.startswith("forge-installer"):
            name = "1.20.1-47.2.0"  # the tests install this one; legacy layout below for old Minecraft versions
            legacy = (cwd / ".legacy").exists()
            if legacy:
                (cwd / "forge-1.12.2-14.23.5.2860.jar").write_bytes(b"PK\x03\x04forge")
            else:
                self._argfiles(cwd, f"net/minecraftforge/forge/{name}")
        elif jar.startswith("neoforge-installer"):
            self._argfiles(cwd, "net/neoforged/neoforge/21.1.77")
        elif jar.startswith("quilt-installer"):
            (cwd / "quilt-server-launch.jar").write_bytes(b"PK\x03\x04quilt")
        return 0, list(self.output)

    @staticmethod
    def _argfiles(cwd, folder: str) -> None:
        target = cwd / "libraries" / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / "unix_args.txt").write_text("-p libraries/x.jar --launchTarget server\n")
        (target / "win_args.txt").write_text("-p libraries\\x.jar --launchTarget server\n")
        (cwd / "user_jvm_args.txt").write_text("# -Xmx4G\n")
        (cwd / "run.sh").write_text("java @user_jvm_args.txt @... \"$@\"\n")


@dataclass
class FakeMod:
    path: str
    content: bytes
    server: str = "required"
    client: str = "required"
    host: str = "cdn.modrinth.com"

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.content).hexdigest()

    @property
    def sha512(self) -> str:
        return hashlib.sha512(self.content).hexdigest()

    @property
    def url(self) -> str:
        return f"https://{self.host}/data/{self.sha1[:8]}/versions/{self.sha1[8:16]}/{self.filename}"


def mrpack_bytes(
    *, name: str = "Cool Pack", version_id: str = "1.0.0", minecraft: str = "1.21.1", loader: tuple[str, str] = ("neoforge", "21.1.77"),
    mods: tuple[FakeMod, ...] | list[FakeMod] = (), overrides: dict[str, bytes] | None = None,
    server_overrides: dict[str, bytes] | None = None, index_patch: dict[str, Any] | None = None, raw_entries: dict[str, bytes] | None = None,
    file_patch: Callable[[dict[str, Any]], None] | None = None,
) -> bytes:
    """A real .mrpack: the zip with modrinth.index.json and the override folders, as Modrinth's own launcher writes it."""
    dependency = {"fabric": "fabric-loader", "quilt": "quilt-loader"}.get(loader[0], loader[0])
    files = []
    for mod in mods:
        entry = {
            "path": mod.path, "hashes": {"sha1": mod.sha1, "sha512": mod.sha512}, "env": {"client": mod.client, "server": mod.server},
            "downloads": [mod.url], "fileSize": len(mod.content),
        }
        if file_patch:
            file_patch(entry)
        files.append(entry)
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": version_id, "name": name, "summary": f"{name} for tests",
        "files": files, "dependencies": {"minecraft": minecraft, dependency: loader[1]},
    }
    index.update(index_patch or {})
    entries: dict[str, bytes] = {"modrinth.index.json": json.dumps(index).encode()}
    entries.update({f"overrides/{path}": data for path, data in (overrides or {}).items()})
    entries.update({f"server-overrides/{path}": data for path, data in (server_overrides or {}).items()})
    entries.update(raw_entries or {})
    return _zip_bytes(entries)


class FakeInternet:
    """Routes ``https://host/path`` to handlers; unknown addresses answer 404 like a real server would."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[str] = []
        self.offline = False
        self.java_downloads = 0
        self.mod_downloads: dict[str, int] = {}
        self.modrinth_projects: list[FakeModrinth] = []
        # What the projects behind mod files say about servers: sha1 -> (project id, required | optional | unsupported)
        self.mod_projects: dict[str, tuple[str, str]] = {}
        self.lookup_status = 200  # the status the "which mods work on a server" lookups answer with
        self.route("https://api.modrinth.com/v2/version_files", self._version_files)
        self.route("https://api.modrinth.com/v2/projects", self._projects)

    # ----------------------------------------------------------------- plumbing
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), follow_redirects=True, headers={"User-Agent": "tests"})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.offline:
            raise httpx.ConnectError("simulated outage", request=request)
        handler = self.routes.get((request.url.host, request.url.path))
        return handler(request) if handler else httpx.Response(404, json={"error": "not found"})

    def route(self, url: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        parts = urlsplit(url)
        self.routes[(parts.hostname or "", parts.path)] = handler

    def json(self, url: str, data: Any) -> None:
        self.route(url, lambda request: httpx.Response(200, json=data))

    # ----------------------------------------------------- what Modrinth says about mods
    def mod_sides(self, *pairs: tuple[FakeMod, str]) -> None:
        """Say what Modrinth knows about these mod files: ``(mod, 'unsupported')`` is a mod that only works on a client."""
        for mod, side in pairs:
            self.mod_projects[mod.sha1] = (f"P{len(self.mod_projects) + 1:07d}", side)

    def _version_files(self, request: httpx.Request) -> httpx.Response:
        if self.lookup_status != 200:
            return httpx.Response(self.lookup_status, json={"error": "unavailable"})
        hashes = json.loads(request.content)["hashes"]
        return httpx.Response(200, json={h: {"id": f"V{h[:7]}", "project_id": self.mod_projects[h][0]} for h in hashes if h in self.mod_projects})

    def _projects(self, request: httpx.Request) -> httpx.Response:
        if self.lookup_status != 200:
            return httpx.Response(self.lookup_status, json={"error": "unavailable"})
        ids = json.loads(parse_qs(request.url.query.decode())["ids"][0])
        sides = dict(self.mod_projects.values())
        return httpx.Response(200, json=[{"id": pid, "server_side": sides[pid]} for pid in ids if pid in sides])

    def blob(self, url: str, content: bytes, content_type: str = "application/octet-stream") -> None:
        self.route(url, lambda request: httpx.Response(200, content=content, headers={"Content-Type": content_type}))

    # ----------------------------------------------------------------- Adoptium
    def adoptium(self, major: int = 21, *, kind: str = "tar.gz", os_name: str | None = None, arch: str = "x64", release: str = JRE_RELEASE,
                 corrupt: bool = False, evil_member: str | None = None) -> bytes:
        """Offer a JRE; ``os_name`` limits it to one platform (any when None). Returns the archive bytes."""
        archive = jre_archive(kind, release, evil_member=evil_member)
        name = f"OpenJDK{major}U-jre_{arch}_{os_name or 'any'}_hotspot_{release.replace('jdk-', '').replace('+', '_')}.{kind}"
        link = f"https://github.com/adoptium/temurin{major}-binaries/releases/download/{release}/{name}"
        checksum = hashlib.sha256(archive).hexdigest()
        if corrupt:
            checksum = "0" * 64

        def listing(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if (os_name and query.get("os") != [os_name]) or query.get("architecture") != [arch] or query.get("image_type") != ["jre"]:
                return httpx.Response(200, json=[])
            entry = {"release_name": release, "binary": {"package": {"name": name, "link": link, "checksum": checksum, "size": len(archive)}}}
            return httpx.Response(200, json=[entry])

        def serve(request: httpx.Request) -> httpx.Response:
            self.java_downloads += 1
            return httpx.Response(200, content=archive)

        self.route(f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot", listing)
        self.route(link, serve)
        return archive

    # ------------------------------------------------------------------ loaders
    def fabric(self, minecraft: str, loader: str = "0.16.9", installer: str = "1.0.1") -> None:
        self.json("https://meta.fabricmc.net/v2/versions/installer", [
            {"version": "1.1.0-beta", "stable": False}, {"version": installer, "stable": True},
        ])
        self.blob(f"https://meta.fabricmc.net/v2/versions/loader/{minecraft}/{loader}/{installer}/server/jar", _zip_bytes({"fabric": b"launcher"}))

    def quilt(self, installer: str = "0.15.1", *, wrong_checksum: bool = False) -> None:
        """Quilt's installer. Like the real service, the metadata lists a hash that does not match the file (a real run caught
        that); the checksum Maven publishes next to the file is the one that can be trusted."""
        jar = _zip_bytes({"quilt": b"installer"})
        url = f"https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/{installer}/quilt-installer-{installer}.jar"
        self.json("https://meta.quiltmc.org/v3/versions/installer", [
            {"version": installer, "url": url, "file_size": len(jar), "hashes": {"sha256": "f" * 64}},
        ])
        self.blob(url, jar)
        digest = "0" * 64 if wrong_checksum else hashlib.sha256(jar).hexdigest()
        self.route(url + ".sha256", lambda request: httpx.Response(200, text=digest))

    def maven_installer(self, kind: str, minecraft: str, version: str, *, publish_checksum: bool = True, wrong_checksum: bool = False,
                        suffixed: bool = False) -> bytes:
        """Serve a Forge / NeoForge installer jar (and the .sha256 next to it) from where the real Maven repositories keep it."""
        jar = _zip_bytes({kind: b"installer"})
        if kind == "forge":
            name = f"{minecraft}-{version}-{minecraft}" if suffixed else f"{minecraft}-{version}"
            url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{name}/forge-{name}-installer.jar"
        elif minecraft == "1.20.1":
            url = f"https://maven.neoforged.net/releases/net/neoforged/forge/{minecraft}-{version}/forge-{minecraft}-{version}-installer.jar"
        else:
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{version}/neoforge-{version}-installer.jar"
        self.blob(url, jar)
        if publish_checksum:
            digest = "0" * 64 if wrong_checksum else hashlib.sha256(jar).hexdigest()
            self.route(url + ".sha256", lambda request: httpx.Response(200, text=digest))
        return jar

    # ------------------------------------------------------------------- Mojang
    def minecraft(self, versions: dict[str, int | None]) -> None:
        """Register Minecraft versions with the Java major version Mojang's metadata names for them (None: no javaVersion)."""
        entries = []
        for version, java in versions.items():
            url = f"https://piston-meta.mojang.com/v1/packages/{hashlib.sha1(version.encode()).hexdigest()}/{version}.json"
            entries.append({"id": version, "type": "release", "url": url, "sha1": "0" * 40, "releaseTime": "2026-01-01T00:00:00+00:00"})
            meta: dict[str, Any] = {"id": version, "downloads": {"server": {"sha1": "1" * 40, "size": 50_000_000, "url": f"https://piston-data.mojang.com/{version}/server.jar"}}}
            if java is not None:
                meta["javaVersion"] = {"component": "java-runtime", "majorVersion": java}
            self.json(url, meta)
        self.json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json", {"latest": {"release": next(iter(versions), "")}, "versions": entries})


class FakeModrinth:
    """One modpack project on the fake Modrinth: search hit, project page, versions, the .mrpack files and their mods."""

    def __init__(self, net: FakeInternet, *, slug: str = "cool-pack", title: str = "Cool Pack", project_id: str = "AbCdEf12",
                 server_side: str = "required", project_type: str = "modpack") -> None:
        self.net, self.slug, self.title, self.id = net, slug, title, project_id
        self.server_side, self.project_type = server_side, project_type
        self.versions: list[dict[str, Any]] = []
        self.packs: dict[str, bytes] = {}
        net.modrinth_projects.append(self)
        net.blob(f"https://cdn.modrinth.com/data/{project_id}/icon.png", PNG_HEADER + b"0" * 32, "image/png")
        for ident in (slug, project_id):
            net.route(f"https://api.modrinth.com/v2/project/{ident}", lambda request: httpx.Response(200, json=self.project()))
            net.route(f"https://api.modrinth.com/v2/project/{ident}/version", lambda request: httpx.Response(200, json=self.versions))
        net.route("https://api.modrinth.com/v2/search", self._search)

    def project(self) -> dict[str, Any]:
        return {
            "id": self.id, "slug": self.slug, "title": self.title, "description": f"{self.title}: a pack for tests",
            "project_type": self.project_type, "server_side": self.server_side, "client_side": "required",
            "icon_url": f"https://cdn.modrinth.com/data/{self.id}/icon.png", "downloads": 1234,
            "game_versions": sorted({v for ver in self.versions for v in ver["game_versions"]}),
            "loaders": sorted({loader for ver in self.versions for loader in ver["loaders"]}),
        }

    def add_version(self, number: str, *, minecraft: str = "1.21.1", loader: tuple[str, str] = ("neoforge", "21.1.77"),
                    mods: tuple[FakeMod, ...] | list[FakeMod] = (), version_type: str = "release", published: str | None = None,
                    **pack_options: Any) -> dict[str, Any]:
        vid = f"V{len(self.versions) + 1:07d}"
        pack = mrpack_bytes(name=self.title, version_id=number, minecraft=minecraft, loader=loader, mods=mods, **pack_options)
        filename = f"{self.slug}-{number}.mrpack"
        url = f"https://cdn.modrinth.com/data/{self.id}/versions/{vid}/{filename}"
        version = {
            "id": vid, "project_id": self.id, "name": f"{self.title} {number}", "version_number": number, "version_type": version_type,
            "date_published": published or f"2026-09-{len(self.versions) + 1:02d}T12:00:00Z", "game_versions": [minecraft],
            "loaders": [loader[0]], "status": "listed",
            "files": [{"hashes": {"sha1": hashlib.sha1(pack).hexdigest(), "sha512": hashlib.sha512(pack).hexdigest()}, "url": url,
                       "filename": filename, "primary": True, "size": len(pack)}],
        }
        self.versions.insert(0, version)
        self.packs[vid] = pack
        self.net.blob(url, pack)
        self.net.route(f"https://api.modrinth.com/v2/version/{vid}", lambda request, v=version: httpx.Response(200, json=v))
        for mod in mods:
            self._serve_mod(mod)
        return version

    def _serve_mod(self, mod: FakeMod) -> None:
        def serve(request: httpx.Request) -> httpx.Response:
            self.net.mod_downloads[mod.url] = self.net.mod_downloads.get(mod.url, 0) + 1
            return httpx.Response(200, content=mod.content)

        self.net.route(mod.url, serve)

    def _search(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        text = (query.get("query") or [""])[0].lower()
        facets = (query.get("facets") or [""])[0]
        hits = []
        for project in self.net.modrinth_projects:
            if project.project_type != "modpack" or (text and text not in project.title.lower()):
                continue
            if "server_side:required" in facets and project.server_side not in ("required", "optional"):
                continue
            hits.append({
                "project_id": project.id, "slug": project.slug, "title": project.title, "description": f"{project.title}: a pack for tests",
                "icon_url": f"https://cdn.modrinth.com/data/{project.id}/icon.png", "downloads": 1234, "server_side": project.server_side,
                "versions": project.project()["game_versions"], "categories": project.project()["loaders"],
            })
        return httpx.Response(200, json={"hits": hits, "total_hits": len(hits), "offset": 0, "limit": 20})
