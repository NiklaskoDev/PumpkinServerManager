"""A fake internet for the Java side: Adoptium, Mojang, Modrinth, the loaders' download sites.

Everything is served from memory through ``httpx.MockTransport``, so tests (and the local dev dashboard) can create
modpack servers without touching the network. The services take an ``httpx.AsyncClient``, so they cannot tell the
difference.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    """A zip with fixed timestamps: the same files always give the same bytes, so a test can compare a jar with what it should be."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def server_jar(version: str) -> bytes:
    """What Mojang's server.jar of a Minecraft version looks like to the manager: a jar (a zip) that is different for every version."""
    return _zip_bytes({"version.json": json.dumps({"id": version}).encode(), "net/minecraft/server/Main.class": version.encode() * 40})


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
        self.patterns: list[tuple[str, re.Pattern[str], Callable[[httpx.Request], httpx.Response]]] = []
        self.requests: list[str] = []
        self.keys_sent_to: set[str] = set()  # the hosts that were sent an x-api-key
        self.curseforge: FakeCurseForge | None = None
        self.offline = False
        self.java_downloads = 0
        self.server_jar_downloads = 0  # Minecraft server jars fetched from Mojang
        self.mod_downloads: dict[str, int] = {}
        self.modrinth_projects: list[FakeModrinth] = []
        # What the projects behind mod files say about servers: sha1 -> (project id, required | optional | unsupported)
        self.mod_projects: dict[str, tuple[str, str]] = {}
        self.mod_dependencies: dict[str, list[str]] = {}  # sha1 -> the sha1s of the mods its version requires
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
        if "x-api-key" in request.headers:
            self.keys_sent_to.add(request.url.host)
        if self.offline:
            raise httpx.ConnectError("simulated outage", request=request)
        handler = self.routes.get((request.url.host, request.url.path))
        if handler is None:
            handler = next((h for host, pattern, h in self.patterns if host == request.url.host and pattern.fullmatch(request.url.path)), None)
        return handler(request) if handler else httpx.Response(404, json={"error": "not found"})

    def route_pattern(self, host: str, pattern: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.patterns.append((host, re.compile(pattern), handler))

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

    def mod_requires(self, mod: FakeMod, *needed: FakeMod) -> None:
        """Say that this mod's Modrinth version lists these mods as required dependencies."""
        self.mod_dependencies[mod.sha1] = [m.sha1 for m in needed]

    def _version_files(self, request: httpx.Request) -> httpx.Response:
        if self.lookup_status != 200:
            return httpx.Response(self.lookup_status, json={"error": "unavailable"})
        hashes = json.loads(request.content)["hashes"]
        known = {}
        for h in hashes:
            if h not in self.mod_projects:
                continue
            needs = [{"project_id": self.mod_projects[n][0], "dependency_type": "required", "version_id": None}
                     for n in self.mod_dependencies.get(h, []) if n in self.mod_projects]
            known[h] = {"id": f"V{h[:7]}", "project_id": self.mod_projects[h][0], "dependencies": needs}
        return httpx.Response(200, json=known)

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

    def adoptium_release(self, major: int = 8, release: str = "jdk8u312-b07", *, kind: str = "tar.gz", os_name: str | None = None,
                         arch: str = "x64", corrupt: bool = False) -> bytes:
        """Offer one exact release, what a pin asks for. Like the real service it answers 404 where it has no such build."""
        archive = jre_archive(kind, release)
        name = f"OpenJDK{major}U-jre_{arch}_{os_name or 'any'}_hotspot_{release.replace('jdk', '').replace('-', '')}.{kind}"
        link = f"https://github.com/adoptium/temurin{major}-binaries/releases/download/{release}/{name}"
        checksum = "0" * 64 if corrupt else hashlib.sha256(archive).hexdigest()

        def listing(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if (os_name and query.get("os") != [os_name]) or query.get("architecture") != [arch] or query.get("image_type") != ["jre"]:
                return httpx.Response(404)
            package = {"name": name, "link": link, "checksum": checksum, "size": len(archive)}
            return httpx.Response(200, json={"release_name": release, "binaries": [{"package": package}]})

        def serve(request: httpx.Request) -> httpx.Response:
            self.java_downloads += 1
            return httpx.Response(200, content=archive)

        self.route(f"https://api.adoptium.net/v3/assets/release_name/eclipse/{release}", listing)
        self.route(link, serve)
        return archive

    def azul(self, version: str = "8.0.312", *, distro: str = "8.58.0.13", kind: str = "tar.gz", os_name: str = "alpine-linux",
             arch: str = "x64", corrupt: bool = False, missing_hash: bool = False) -> bytes:
        """Offer one Zulu JRE (as Azul's metadata service lists it, with the hash only when asked for) for one platform."""
        wanted_os = {"alpine-linux": "linux-musl", "linux": "linux-glibc"}.get(os_name, os_name)
        folder = f"zulu{distro}-ca-jre{version}-{wanted_os.replace('-', '_')}_{arch}"
        archive = jre_archive(kind, folder)
        name = f"{folder}.{kind}"
        link = f"https://cdn.azul.com/zulu/bin/{name}"
        checksum = "0" * 64 if corrupt else hashlib.sha256(archive).hexdigest()

        def listing(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if query.get("java_version") != [version] or query.get("os") != [wanted_os] or query.get("arch") != [arch]:
                return httpx.Response(200, json=[])
            entry: dict[str, Any] = {
                "name": name, "download_url": link, "java_version": [int(n) for n in version.split(".")],
                "distro_version": [int(n) for n in distro.split(".")],
                "size": len(archive) + 100 - len(archive) % 100,  # the real service rounds up to the next hundred (43830592 -> 43830600)
            }
            if not missing_hash and "sha256_hash" in query.get("include_fields", [""])[0]:
                entry["sha256_hash"] = checksum
            return httpx.Response(200, json=[entry])

        def serve(request: httpx.Request) -> httpx.Response:
            self.java_downloads += 1
            return httpx.Response(200, content=archive)

        self.route("https://api.azul.com/metadata/v1/zulu/packages/", listing)
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
    def minecraft(self, versions: dict[str, int | None], *, snapshots: dict[str, int | None] | None = None, no_server: tuple[str, ...] = ()) -> None:
        """Register Minecraft versions, newest first, with the Java major version Mojang's metadata names for them (None: no
        javaVersion). Every version has a server jar with its real size and SHA-1 (except those in ``no_server``). ``snapshots`` are
        newer than all the releases."""
        entries = []
        base = datetime(2026, 9, 1, 12, tzinfo=UTC)
        ordered = [(v, j, "snapshot") for v, j in (snapshots or {}).items()] + [(v, j, "release") for v, j in versions.items()]
        for index, (version, java, kind) in enumerate(ordered):
            released = (base - timedelta(days=20 * index)).isoformat()
            url = f"https://piston-meta.mojang.com/v1/packages/{hashlib.sha1(version.encode()).hexdigest()}/{version}.json"
            entries.append({"id": version, "type": kind, "url": url, "sha1": "0" * 40, "releaseTime": released})
            jar = server_jar(version)
            sha1 = hashlib.sha1(jar).hexdigest()
            jar_url = f"https://piston-data.mojang.com/v1/objects/{sha1}/server.jar"
            meta: dict[str, Any] = {"id": version, "type": kind, "releaseTime": released, "downloads": {}}
            if version not in no_server:
                meta["downloads"]["server"] = {"sha1": sha1, "size": len(jar), "url": jar_url}
                self.route(jar_url, lambda request, data=jar: self._serve_server_jar(data))
            if java is not None:
                meta["javaVersion"] = {"component": "java-runtime", "majorVersion": java}
            self.json(url, meta)
        latest_release = next((v for v, _, kind in ordered if kind == "release"), "")
        latest_snapshot = next((v for v, _, kind in ordered if kind == "snapshot"), latest_release)
        self.json(
            "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
            {"latest": {"release": latest_release, "snapshot": latest_snapshot}, "versions": entries},
        )

    def _serve_server_jar(self, data: bytes) -> httpx.Response:
        self.server_jar_downloads += 1
        return httpx.Response(200, content=data)


class FakePaper:
    """PaperMC's download service (Fill v3): the Minecraft versions it builds (release candidates too, like the real one), their
    builds with a channel, what it says about each version, and the jars, which are found by their SHA-256 on another host."""

    API = "fill.papermc.io"
    DATA = "fill-data.papermc.io"

    def __init__(self, net: FakeInternet) -> None:
        self.net = net
        self.versions: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}  # sha256 -> the jar
        self.downloads = 0
        self.api_calls: list[str] = []
        net.route_pattern(self.API, r"/v3/projects/paper", self._project)
        net.route_pattern(self.API, r"/v3/projects/paper/versions/[^/]+", self._version)
        net.route_pattern(self.API, r"/v3/projects/paper/versions/[^/]+/builds", self._builds)
        net.route_pattern(self.DATA, r"/v1/objects/[0-9a-f]{64}/.+", self._jar)

    def add_version(self, version: str, *, java: int = 21, flags: tuple[str, ...] = ("-XX:+UseG1GC", "-XX:MaxGCPauseMillis=200"),
                    support: str = "SUPPORTED", end: str | None = None) -> None:
        self.versions[version] = {"java": java, "flags": list(flags), "support": {"status": support, **({"end": end} if end else {})}, "builds": []}

    def add_build(self, version: str, build: int, channel: str = "STABLE", *, broken: bool = False) -> dict[str, Any]:
        """Publish a build. A ``broken`` one has a jar that the fake Java server refuses to run (see ``crash_if_jar_contains``)."""
        files = {"paperclip.json": json.dumps({"version": version, "build": build}).encode(), "meta/versions.list": f"{version}-{build}".encode() * 30}
        if broken:
            files["BROKEN"] = b"this build does not start"
        jar = _zip_bytes(files)
        sha256 = hashlib.sha256(jar).hexdigest()
        self.blobs[sha256] = jar
        entry = {
            "id": build, "time": f"2026-09-{(build % 27) + 1:02d}T10:00:00.000Z", "channel": channel.upper(),
            "commits": [{"sha": "0" * 40, "time": "2026-09-01T00:00:00Z", "message": "A change"}],
            "downloads": {"server:default": {
                "name": f"paper-{version}-{build}.jar", "checksums": {"sha256": sha256}, "size": len(jar),
                "url": f"https://{self.DATA}/v1/objects/{sha256}/paper-{version}-{build}.jar",
            }},
        }
        self.versions[version]["builds"].insert(0, entry)
        self.versions[version]["builds"].sort(key=lambda b: b["id"], reverse=True)
        return entry

    def _missing(self) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "error": "version_not_found", "message": "Version not found"})

    def _project(self, request: httpx.Request) -> httpx.Response:
        self.api_calls.append(request.url.path)
        groups: dict[str, list[str]] = {}
        for version in sorted(self.versions, key=lambda v: [int(n) if n.isdigit() else 0 for n in re.split(r"[.-]", v)], reverse=True):
            groups.setdefault(".".join(version.split(".")[:2]).split("-")[0], []).append(version)
        return httpx.Response(200, json={"project": {"id": "paper", "name": "Paper"}, "versions": groups})

    def _version(self, request: httpx.Request) -> httpx.Response:
        self.api_calls.append(request.url.path)
        version = request.url.path.rsplit("/", 1)[-1]
        known = self.versions.get(version)
        if known is None:
            return self._missing()
        return httpx.Response(200, json={
            "version": {"id": version, "support": known["support"], "java": {"version": {"minimum": known["java"]}, "flags": {"recommended": known["flags"]}}},
            "builds": [b["id"] for b in known["builds"]],
        })

    def _builds(self, request: httpx.Request) -> httpx.Response:
        self.api_calls.append(request.url.path)
        known = self.versions.get(request.url.path.split("/")[-2])
        return httpx.Response(200, json=known["builds"]) if known is not None else self._missing()

    def _jar(self, request: httpx.Request) -> httpx.Response:
        jar = self.blobs.get(request.url.path.split("/")[3])
        if jar is None:
            return httpx.Response(404)
        self.downloads += 1
        return httpx.Response(200, content=jar)


class FakeFabric:
    """Fabric's metadata service: the Minecraft versions it supports, the loader versions for them, and the server launcher jars."""

    HOST = "meta.fabricmc.net"

    def __init__(self, net: FakeInternet, installer: str = "1.0.1") -> None:
        self.net = net
        self.installer = installer
        self.games: dict[str, bool] = {}  # Minecraft version -> stable
        self.loaders: list[tuple[str, bool]] = []  # (version, stable), newest first
        self.broken: set[tuple[str, str]] = set()  # (Minecraft version, loader version) of launchers the fake Java server refuses to run
        self.downloads = 0
        net.route_pattern(self.HOST, r"/v2/versions/game", self._games)
        net.route_pattern(self.HOST, r"/v2/versions/loader/[^/]+", self._loaders)
        net.route_pattern(self.HOST, r"/v2/versions/loader/[^/]+/[^/]+/[^/]+/server/jar", self._launcher)

    def add_game(self, version: str, stable: bool = True) -> None:
        self.games = {version: stable, **self.games}  # newest first

    def add_loader(self, version: str, stable: bool = True) -> None:
        self.loaders.insert(0, (version, stable))

    def _games(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"version": v, "stable": s} for v, s in self.games.items()])

    def _loaders(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.rsplit("/", 1)[-1] not in self.games:
            return httpx.Response(400, json=[])  # what the real service answers for a Minecraft version it does not know
        return httpx.Response(200, json=[
            {"loader": {"version": v, "stable": s, "separator": ".", "build": 1, "maven": f"net.fabricmc:fabric-loader:{v}"}, "intermediary": {}, "launcherMeta": {}}
            for v, s in self.loaders
        ])

    def _launcher(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        self.downloads += 1
        marker = {"fabric-launcher": f"{parts[4]}-{parts[5]}-{parts[6]}"}
        if (parts[4], parts[5]) in self.broken:
            marker["BROKEN"] = "this loader does not start"
        return httpx.Response(200, content=_zip_bytes({k: v.encode() for k, v in marker.items()}), headers={"Content-Type": "application/java-archive"})


class FakeFabricApi:
    """The Fabric API mod on Modrinth: versions for Minecraft versions, found with the loader and game version filters like the real one."""

    PROJECT = "P7dR8mSH"

    def __init__(self, net: FakeInternet) -> None:
        self.net = net
        self.versions: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.downloads = 0
        net.route("https://api.modrinth.com/v2/project/fabric-api/version", self._versions)

    def add_version(self, number: str, minecraft: str, version_type: str = "release") -> dict[str, Any]:
        vid = f"FA{len(self.versions) + 1:06d}"
        content = _zip_bytes({"fabric.mod.json": json.dumps({"id": "fabric-api", "version": number}).encode()})
        filename = f"fabric-api-{number}.jar"
        url = f"https://cdn.modrinth.com/data/{self.PROJECT}/versions/{vid}/{filename}"
        version = {
            "id": vid, "project_id": self.PROJECT, "name": f"Fabric API {number}", "version_number": number, "version_type": version_type,
            "date_published": f"2026-09-{len(self.versions) + 1:02d}T12:00:00Z", "game_versions": [minecraft], "loaders": ["fabric"], "status": "listed",
            "files": [{"hashes": {"sha1": hashlib.sha1(content).hexdigest(), "sha512": hashlib.sha512(content).hexdigest()}, "url": url,
                       "filename": filename, "primary": True, "size": len(content)}],
        }
        self.versions.insert(0, version)
        self.files[vid] = content
        self.net.route(url, lambda request, data=content: self._serve(data))
        return version

    def _serve(self, data: bytes) -> httpx.Response:
        self.downloads += 1
        return httpx.Response(200, content=data)

    def _versions(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        loaders = json.loads((query.get("loaders") or ["[]"])[0])
        games = json.loads((query.get("game_versions") or ["[]"])[0])
        found = [v for v in self.versions if (not loaders or set(loaders) & set(v["loaders"])) and (not games or set(games) & set(v["game_versions"]))]
        return httpx.Response(200, json=found)


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


class FakeCurseForge:
    """CurseForge: its API (which wants a key), its projects and files, and the CDN that serves them. Like the real one, the
    list of a project's files leaves out the server packs that belong to its files; a file is found by its number."""

    HOST = "api.curseforge.com"

    def __init__(self, net: FakeInternet, *, key: str = "cf-test-key", search: bool = True) -> None:
        self.net, self.key, self.search_enabled = net, key, search
        self.mods: dict[int, dict[str, Any]] = {}
        self.files: dict[int, dict[str, Any]] = {}
        self.blobs: dict[int, bytes] = {}  # what each file holds
        self.api_calls: list[str] = []
        net.curseforge = self
        net.route_pattern(self.HOST, r"/v1/games/432", self._game)
        net.route_pattern(self.HOST, r"/v1/mods/search", self._search)
        net.route_pattern(self.HOST, r"/v1/mods/featured", self._featured)
        net.route_pattern(self.HOST, r"/v1/mods/files", self._files_bulk)
        net.route_pattern(self.HOST, r"/v1/mods/(\d+)/files/(\d+)", self._file)
        net.route_pattern(self.HOST, r"/v1/mods/(\d+)/files", self._files_of)
        net.route_pattern(self.HOST, r"/v1/mods/(\d+)", self._mod)
        net.route_pattern(self.HOST, r"/v1/mods", self._mods_bulk)

    # ------------------------------------------------------------------- building the world
    def add_project(self, project_id: int, name: str, *, class_id: int = 6, slug: str | None = None, allow_distribution: bool = True) -> dict[str, Any]:
        slug = slug or name.lower().replace(" ", "-")
        kind = "modpacks" if class_id == 4471 else "mc-mods"
        logo = f"https://media.forgecdn.net/avatars/thumbnails/{project_id}/256/256/logo.png"
        self.net.blob(logo, PNG_HEADER + b"1" * 32, "image/png")
        mod = {
            "id": project_id, "gameId": 432, "name": name, "slug": slug, "classId": class_id, "summary": f"{name}, for tests",
            "links": {"websiteUrl": f"https://www.curseforge.com/minecraft/{kind}/{slug}"}, "downloadCount": 1000 + project_id,
            "logo": {"thumbnailUrl": logo, "url": logo}, "latestFilesIndexes": [], "allowModDistribution": allow_distribution,
        }
        self.mods[project_id] = mod
        return mod

    def _register(
        self, mod_id: int, file_id: int, filename: str, content: bytes, *, display: str | None = None, release: int = 1,
        game_versions: tuple[str, ...] = ("1.21.1", "NeoForge"), downloadable: bool = True, requires: tuple[int, ...] = (),
        server_pack_of: int | None = None, date: str | None = None,
    ) -> dict[str, Any]:
        url = f"https://edge.forgecdn.net/files/{file_id // 1000}/{file_id % 1000}/{filename}"
        file = {
            "id": file_id, "gameId": 432, "modId": mod_id, "isAvailable": True, "displayName": display or filename, "fileName": filename,
            "releaseType": release, "fileStatus": 4,
            "hashes": [{"value": hashlib.sha1(content).hexdigest(), "algo": 1}, {"value": hashlib.md5(content).hexdigest(), "algo": 2}],
            "fileDate": date or f"2026-09-{(file_id % 27) + 1:02d}T12:00:00.000Z", "fileLength": len(content),
            "downloadUrl": url if downloadable else None, "gameVersions": list(game_versions),
            "dependencies": [{"modId": m, "relationType": 3} for m in requires], "isServerPack": server_pack_of is not None,
            "serverPackFileId": None, "parentProjectFileId": server_pack_of,
        }
        self.files[file_id] = file
        self.blobs[file_id] = content
        if downloadable:
            self.net.blob(url, content)
        return file

    def add_mod(
        self, project_id: int, file_id: int, mod: FakeMod, *, name: str | None = None, client_only: bool = False, downloadable: bool = True,
        requires: tuple[int, ...] = (), class_id: int = 6,
    ) -> dict[str, Any]:
        """A mod project with one file whose content is ``mod``'s. Returns the file as the API shows it."""
        project = self.add_project(project_id, name or mod.filename.removesuffix(".jar"), class_id=class_id, allow_distribution=downloadable)
        tags = ("1.21.1", "NeoForge", "Client") if client_only else ("1.21.1", "NeoForge")
        return self._register(project["id"], file_id, mod.filename, mod.content, game_versions=tags, downloadable=downloadable, requires=requires)

    def add_pack_version(
        self, project_id: int, file_id: int, number: str, *, mods: tuple[tuple[int, int], ...] = (), minecraft: str = "1.21.1",
        loader: str = "neoforge-21.1.77", overrides: dict[str, bytes] | None = None, server_pack: dict[str, bytes] | None = None,
        wrapper: str | None = None, release: int = 1, date: str | None = None, server_pack_downloadable: bool = True,
    ) -> dict[str, Any]:
        """A version of a modpack: the zip a launcher would download (manifest and overrides) and, when ``server_pack`` is given, the
        author's server pack (a zip of those files, inside ``wrapper`` when that is given)."""
        pack = self.mods[project_id]
        manifest = {
            "minecraft": {"version": minecraft, "modLoaders": [{"id": loader, "primary": True}]}, "manifestType": "minecraftModpack",
            "manifestVersion": 1, "name": pack["name"], "version": number, "author": "tests", "overrides": "overrides",
            "files": [{"projectID": p, "fileID": f, "required": True} for p, f in mods],
        }
        entries = {"manifest.json": json.dumps(manifest).encode(), **{f"overrides/{k}": v for k, v in (overrides or {}).items()}}
        file = self._register(
            project_id, file_id, f"{pack['slug']}-{number}.zip", _zip_bytes(entries), display=f"{pack['name']} {number}",
            game_versions=(minecraft, loader.split("-")[0].capitalize() if loader.split("-")[0] != "neoforge" else "NeoForge"),
            release=release, date=date,
        )
        if server_pack is not None:
            prefix = f"{wrapper.rstrip('/')}/" if wrapper else ""
            content = _zip_bytes({prefix + name: data for name, data in server_pack.items()})
            server = self._register(
                project_id, file_id + 1, f"Server-Files-{number}.zip", content, display=f"Server-Files-{number}", release=release,
                downloadable=server_pack_downloadable, server_pack_of=file_id, date=date,
            )
            file["serverPackFileId"] = server["id"]
        pack["latestFilesIndexes"] = [{"gameVersion": minecraft, "fileId": file_id, "filename": file["fileName"], "releaseType": release,
                                        "modLoader": {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}[loader.split("-")[0]]}]
        return file

    # ------------------------------------------------------------------------------ the API
    def _auth(self, request: httpx.Request) -> httpx.Response | None:
        self.api_calls.append(f"{request.method} {request.url.path}")
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(403, text="Forbidden: API Key missing or invalid")
        return None

    @staticmethod
    def _page(items: list[dict[str, Any]], request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        index, size = int((query.get("index") or ["0"])[0]), int((query.get("pageSize") or ["50"])[0])
        shown = items[index:index + size]
        return httpx.Response(200, json={"data": shown, "pagination": {"index": index, "pageSize": size, "resultCount": len(shown), "totalCount": len(items)}})

    def _game(self, request: httpx.Request) -> httpx.Response:
        return self._auth(request) or httpx.Response(200, json={"data": {"id": 432, "name": "Minecraft", "slug": "minecraft"}})

    def _search(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        if not self.search_enabled:
            return httpx.Response(403, text="Forbidden: API Key missing or invalid")  # a key without the right to search looks like this
        query = parse_qs(request.url.query.decode())
        text = (query.get("searchFilter") or [""])[0].lower()
        slug = (query.get("slug") or [""])[0]
        found = [m for m in self.mods.values() if m["classId"] == int((query.get("classId") or ["4471"])[0]) and text in m["name"].lower() and (not slug or m["slug"] == slug)]
        return self._page(sorted(found, key=lambda m: -m["downloadCount"]), request)

    def _featured(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        packs = [m for m in self.mods.values() if m["classId"] == 4471]
        return httpx.Response(200, json={"data": {"featured": packs[:2], "popular": packs, "recentlyUpdated": packs[-1:]}})

    def _mod(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        mod = self.mods.get(int(request.url.path.rsplit("/", 1)[1]))
        return httpx.Response(200, json={"data": mod}) if mod else httpx.Response(404, json={})

    def _files_of(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        mod_id = int(request.url.path.split("/")[3])
        listed = [f for f in self.files.values() if f["modId"] == mod_id and not f["isServerPack"]]
        return self._page(sorted(listed, key=lambda f: f["fileDate"], reverse=True), request)

    def _file(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        parts = request.url.path.split("/")
        file = self.files.get(int(parts[5]))
        return httpx.Response(200, json={"data": file}) if file and file["modId"] == int(parts[3]) else httpx.Response(404, json={})

    def _files_bulk(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        wanted = json.loads(request.content)["fileIds"]
        return httpx.Response(200, json={"data": [self.files[i] for i in wanted if i in self.files]})

    def _mods_bulk(self, request: httpx.Request) -> httpx.Response:
        denied = self._auth(request)
        if denied:
            return denied
        wanted = json.loads(request.content)["modIds"]
        return httpx.Response(200, json={"data": [self.mods[i] for i in wanted if i in self.mods]})


SERVERUTILITIES_CFG = b"""# Configuration file

afk {
    # Enables afk timer. [default: true]
    B:enabled=true
}


backups {
    # Path to backups folder. [default: ./backups/]
    S:backup_folder_path=./backups/

    # Time between backups in hours. [default: 0.5]
    S:backup_timer=0.5

    # Enables backups. [default: true]
    B:enable_backups=true

    # Backups won't run if no players are online. [default: true]
    B:need_online_players=true
}


chat {
    B:add_nickname_tilde=false
}
"""
GTNH_PROPERTIES = (
    b"generator-settings=\nop-permission-level=2\nallow-nether=true\nlevel-name=World\nallow-flight=true\nserver-port=25565\nlevel-type=rwg\n"
    b"enable-rcon=false\nwhite-list=true\nonline-mode=true\ndifficulty=3\nenable-command-block=true\nmax-players=20\nmotd=GT:New Horizons\n"
)
GTNH_HOST = "downloads.gtnewhorizons.com"


def gtnh_server_files(version: str, *, modern: bool = True, changes: dict[str, bytes | None] | None = None) -> dict[str, bytes]:
    """What a GT New Horizons server zip holds (the real 2.8.4 one, shrunk): the mods, the configuration, the libraries, the Forge files and
    the scripts of the pack. ``changes`` adds or replaces files; a value of None removes one."""
    files: dict[str, bytes] = {
        "mods/gregtech-5.09.jar": f"gregtech {version}".encode() * 30, "mods/NewHorizonsCoreMod.jar": f"core {version}".encode() * 30,
        "mods/hodgepodge.jar": b"hodgepodge" * 30, "mods/gtnhlib.jar": b"gtnhlib" * 30,
        "config/GregTech/GregTech.cfg": f"B:Something=true\n# the pack's {version} version\n".encode(), "config/hodgepodge.cfg": b"I:fixes=1\n",
        "config/JourneyMapServer/world.cfg": b"uuid=the-maps-of-the-players\n",
        "libraries/org/ow2/asm/asm-all/5.0.3/asm-all-5.0.3.jar": b"asm", "libraries/net/minecraft/launchwrapper/1.12/launchwrapper-1.12.jar": b"lw",
        "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar": b"forge", "minecraft_server.1.7.10.jar": b"vanilla",
        "serverutilities/serverutilities.cfg": SERVERUTILITIES_CFG, "serverutilities/server/ranks.txt": b"# ranks\n",
        "journeymap/config/5.2/journeymap.core.config": b"mapstyle=1\n", "server-icon.png": PNG_HEADER + b"icon",
        "server.properties": GTNH_PROPERTIES, "eula.txt": b"eula=false\n", f"changelog to {version}.md": b"# what changed\n",
        "ops.json": b"[]", "whitelist.json": b"[]", "banned-players.json": b"[]", "banned-ips.json": b"[]", "usercache.json": b"[]",
    }
    if modern:
        files.update({
            "lwjgl3ify-forgePatches.jar": f"patches {version}".encode(), "java9args.txt": b"-Dfile.encoding=UTF-8\n--add-opens\njava.base/java.io=ALL-UNNAMED\n",
            "startserver-java9.sh": b"#!/bin/bash\njava @java9args.txt -jar lwjgl3ify-forgePatches.jar nogui\n", "startserver-java9.bat": b"java @java9args.txt\n",
        })
    else:
        files.update({"startserver.sh": b"#!/bin/bash\njava -jar forge-1.7.10-10.13.4.1614-1.7.10-universal.jar nogui\n", "startserver.bat": b"java -jar x\n"})
    for name, data in (changes or {}).items():
        if data is None:
            files.pop(name, None)
        else:
            files[name] = data
    return files


class FakeGtnh:
    """GT New Horizons' website (its version history page) and its download site (the zips of complete servers)."""

    PAGE = "https://www.gtnewhorizons.com/version-history/"

    def __init__(self, net: FakeInternet) -> None:
        self.net = net
        self.releases: list[dict[str, Any]] = []  # newest first, like the page
        self.page_status = 200
        self.downloads: list[str] = []
        net.route(self.PAGE, self._page)

    def add_release(
        self, version: str, *, beta: bool = False, date: str = "2025/12/23", java_max: int = 25, modern: bool = True, legacy: bool = True,
        changes: dict[str, bytes | None] | None = None, legacy_changes: dict[str, bytes | None] | None = None, redirect_to: str | None = None,
        first: bool = False,
    ) -> dict[str, Any]:
        """Offer a version. ``first`` puts it on top of the page (the newest), otherwise below the ones already there."""
        release = {"version": version, "beta": beta, "date": date, "java_max": java_max, "modern": modern, "legacy": legacy}
        if first:
            self.releases.insert(0, release)
        else:
            self.releases.append(release)
        folder = "betas/" if beta else ""
        for variant, wanted, is_modern, extra in ((f"17-{java_max}", modern, True, changes), ("8", legacy, False, legacy_changes or changes)):
            if not wanted:
                continue
            data = _zip_bytes(gtnh_server_files(version, modern=is_modern, changes=extra))
            path = f"/ServerPacks/{folder}GT_New_Horizons_{version}_Server_Java_{variant}.zip"
            release[variant] = data
            self.net.route(f"https://{GTNH_HOST}{path}", self._serve(data, path, redirect_to))
        return release

    def _serve(self, data: bytes, path: str, redirect_to: str | None) -> Callable[[httpx.Request], httpx.Response]:
        def serve(request: httpx.Request) -> httpx.Response:
            self.downloads.append(path)
            if redirect_to:
                return httpx.Response(302, headers={"Location": redirect_to})
            return httpx.Response(200, content=data, headers={"Content-Type": "application/zip"})

        return serve

    def page(self) -> str:
        """The version history the way the real page is built: one folding block per version, with its date and its download links."""
        blocks = []
        for r in self.releases:
            folder = "betas/" if r["beta"] else ""
            base = f"https://{GTNH_HOST}"
            server = "".join(
                f'<a href="{base}/ServerPacks/{folder}GT_New_Horizons_{r["version"]}_Server_Java_{variant}.zip" target="_blank"><svg></svg><span>{label} ZIP</span></a>'
                for variant, label, wanted in ((f"17-{r['java_max']}", f"Java 17-{r['java_max']}", r["modern"]), ("8", "Java 8", r["legacy"])) if wanted
            )
            blocks.append(
                f'<details class="group"><summary class="flex"><div><span class="v">{r["version"]}</span><span>{"Beta" if r["beta"] else "Stable"} release</span></div>'
                f'<div><span><svg></svg><span>{r["date"]}</span></span></div></summary><div><p><a href="https://github.com/GTNewHorizons/DreamAssemblerXXL/blob/master/releases/changelogs/x.md">'
                f'Click here to get the changelog</a></p><div><div><h3>MultiMC/Prism ZIPs</h3></div><div><a href="{base}/Multi_mc_downloads/{folder}GT_New_Horizons_{r["version"]}_Java_8.zip">'
                f'<span>Java 8 ZIP</span></a></div></div><div><div><h3>Server ZIPs</h3></div><div>{server}</div></div></div></details>'
            )
        return f"<!DOCTYPE html><html><body><section><h1>Version History</h1><div>{''.join(blocks)}</div></section></body></html>"

    def _page(self, request: httpx.Request) -> httpx.Response:
        if self.page_status != 200:
            return httpx.Response(self.page_status, text="unavailable")
        return httpx.Response(200, text=self.page(), headers={"Content-Type": "text/html; charset=utf-8"})
