"""Static checks of the container packaging.

Docker is not available on every development machine, so these catch the mistakes a failed `docker build` or a
badly filled Unraid template would otherwise reveal on the server: a COPY source that .dockerignore excludes,
template values that disagree with each other, Windows line endings in the entrypoint.
"""

from __future__ import annotations

import fnmatch
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _ignore_rules() -> list[tuple[bool, str]]:
    rules = []
    for line in _read(".dockerignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            negate = line.startswith("!")
            rules.append((negate, line.lstrip("!").strip("/")))
    return rules


def _is_ignored(rel: str) -> bool:
    """Simplified Docker semantics: a rule matches the path or any parent folder; the last matching rule wins."""
    parts = rel.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    ignored = False
    for negate, pattern in _ignore_rules():
        for candidate in candidates:
            if pattern.startswith("**/"):
                hit = fnmatch.fnmatchcase(candidate.rsplit("/", 1)[-1], pattern[3:])
            else:
                hit = fnmatch.fnmatchcase(candidate, pattern)
            if hit:
                ignored = not negate
    return ignored


def _copy_sources() -> list[str]:
    sources = []
    for line in _read("Dockerfile").splitlines():
        if line.startswith("COPY ") and "--from" not in line:
            sources.extend(t for t in line.split()[1:-1] if not t.startswith("--"))
    return sources


def test_every_copy_source_exists_and_survives_dockerignore():
    sources = _copy_sources()
    assert {"requirements.txt", "pumpkin_manager", "docker/entrypoint.sh", "scripts/real_binary_check.py"} <= {s.rstrip("/") for s in sources}
    for source in sources:
        path = ROOT / source
        assert path.exists(), f"Dockerfile copies {source}, which does not exist"
        files = [f.relative_to(ROOT).as_posix() for f in (path.rglob("*") if path.is_dir() else [path]) if f.is_file() and "__pycache__" not in f.parts]
        assert files, f"{source} is empty"
        assert not _is_ignored(source.rstrip("/")), f".dockerignore excludes {source}, which the Dockerfile needs"
        assert any(not _is_ignored(f) for f in files), f".dockerignore removes every file of {source}"
    # and the dashboard itself has to be inside the copied package
    assert not _is_ignored("pumpkin_manager/static/index.html")


def test_dockerignore_rules_behave_as_assumed():
    assert _is_ignored("tests/test_api.py") and _is_ignored(".venv/lib/x.py") and _is_ignored("pumpkin_manager/__pycache__/a.pyc")
    assert _is_ignored("scripts/dev_server.py") and not _is_ignored("scripts/real_binary_check.py")
    assert not _is_ignored("docker/entrypoint.sh") and not _is_ignored("requirements.txt")


def test_entrypoint_is_a_posix_shell_script_with_unix_line_endings():
    data = (ROOT / "docker" / "entrypoint.sh").read_bytes()
    assert data.startswith(b"#!/bin/sh\n"), "the shebang must be /bin/sh (Alpine has no bash)"
    assert b"\r" not in data, "CRLF line endings break the script inside the container"
    assert b"su-exec" in data and b"set -eu" in data
    attributes = _read(".gitattributes")
    assert "*.sh text eol=lf" in attributes, "git must not convert shell scripts to CRLF on Windows"


def test_dockerfile_installs_what_the_entrypoint_and_healthcheck_use():
    dockerfile = _read("Dockerfile")
    for tool in ("tini", "su-exec", "tzdata"):
        assert re.search(rf"apk add[^\n]*\b{tool}\b", dockerfile), f"{tool} is not installed"
    assert "wget" in dockerfile and "/api/health" in dockerfile  # busybox wget ships with Alpine
    assert "--only-binary=:all:" in dockerfile, "pip must never compile in the image"
    assert 'ENTRYPOINT ["/sbin/tini"' in dockerfile and "python" in dockerfile.split("CMD")[-1]


def test_unraid_template_is_consistent():
    root = ET.parse(ROOT / "unraid" / "pumpkin-manager.xml").getroot()
    config = {c.get("Name"): c for c in root.findall("Config")}
    value = {name: (c.text or "").strip() for name, c in config.items()}
    target = {name: c.get("Target") for name, c in config.items()}

    repository = root.findtext("Repository", "")
    assert repository.startswith("ghcr.io/") and repository.endswith("/pumpkin-manager:latest")
    assert "YOUR_GITHUB_USER" not in (ROOT / "unraid" / "pumpkin-manager.xml").read_text(encoding="utf-8")
    workflow = _read(".github/workflows/ci.yml")
    assert "/pumpkin-manager" in workflow, "the image the template pulls must be the one the workflow publishes"

    # the variables that must agree with the port mappings and folders
    assert value["Port range (Java)"] == target["Game ports (Java, TCP)"] == value["Game ports (Java, TCP)"]
    assert value["Port range (Bedrock)"] == target["Bedrock ports (UDP)"] == value["Bedrock ports (UDP)"]
    assert value["Backup folder (variable)"] == target["Backups"], "BACKUP_DIR must be the container path of the backups volume"
    assert target["AppData"] == "/data"
    assert config["Game ports (Java, TCP)"].get("Mode") == "tcp" and config["Bedrock ports (UDP)"].get("Mode") == "udp"
    assert f":{target['WebUI port']}" in root.findtext("WebUI", ""), "the WebUI link must use the WebUI port"
    assert "--stop-timeout" in root.findtext("ExtraParams", ""), "servers need time to save when the container stops"
    assert target["CurseForge API key"] == "CURSEFORGE_API_KEY" and not value["CurseForge API key"], "a key is never shipped in the template"
    assert "*" not in value["Reverse proxy IPs"] and not value["Admin password"], "no wildcard and no password ships in the template"
    assert "Do not use *" in config["Reverse proxy IPs"].get("Description", ""), "the template warns against trusting every proxy"
    for secret in ("Admin password", "GitHub token", "CurseForge API key"):
        assert config[secret].get("Mask") == "true", f"{secret} must be masked in the Unraid UI"

    # defaults of the application match what the template advertises
    from pumpkin_manager.config import Settings

    settings = Settings.from_env({})
    assert target["Game ports (Java, TCP)"] == f"{settings.port_range[0]}-{settings.port_range[1]}"
    assert target["Bedrock ports (UDP)"] == f"{settings.bedrock_port_range[0]}-{settings.bedrock_port_range[1]}"
    assert target["WebUI port"] == str(settings.web_port)
    for url_tag in ("Registry", "Support", "Project", "TemplateURL", "Icon"):
        assert root.findtext(url_tag, "").startswith("https://"), url_tag


def test_compose_example_matches_the_container():
    compose = _read("docker-compose.yml")
    assert "\t" not in compose
    assert 'BACKUP_DIR: "/backups"' in compose and "./backups:/backups" in compose
    assert "8420:8420" in compose and "25565-25575:25565-25575/tcp" in compose and "stop_grace_period" in compose
    assert "environment_extra" not in compose
    assert "change-me-please" not in compose, "the example must not ship a password that everybody knows"
    assert not re.search(r"(?m)^\s*ADMIN_PASSWORD:", compose), "...nor set one at all: an unset one is generated"
    assert not re.search(r'(?m)^\s*FORWARDED_ALLOW_IPS:\s*"?\*', compose), "and never trust every proxy"


def test_ci_workflow_publishes_after_the_smoke_test_only():
    workflow = _read(".github/workflows/ci.yml")
    assert "packages: write" in workflow and "docker/build-push-action" in workflow and "ghcr.io" in workflow
    image_job = workflow.split("  image:")[1].split("  publish:")[0]
    publish_job = workflow.split("  publish:")[1]
    assert "needs: test" not in image_job, "a Linux-only test failure must not block the first deployable image"
    assert "needs: image" in publish_job
    assert "linux/amd64,linux/arm64" in publish_job
