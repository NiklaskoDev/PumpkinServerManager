"""Process-wide configuration, read once from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _parse_range(value: str, default: tuple[int, int], name: str) -> tuple[int, int]:
    value = value.strip()
    if not value:
        return default
    try:
        if "-" in value:
            lo, hi = (int(p) for p in value.split("-", 1))
        else:
            lo = hi = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must look like '25565-25575' (got {value!r})") from exc
    if not (1 <= lo <= hi <= 65535):
        raise ValueError(f"{name} must be within 1-65535 and ascending (got {value!r})")
    return lo, hi


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    backup_dir: Path
    web_host: str = "0.0.0.0"
    web_port: int = 8420
    # Public Java ports handed out to servers. These must match the ports published by Docker
    # (or be free on the host when running with host networking).
    port_range: tuple[int, int] = (25565, 25575)
    bedrock_port_range: tuple[int, int] = (19132, 19142)
    # Loopback-only ports used for the management protocol; never need to be published.
    mgmt_port_range: tuple[int, int] = (29000, 29999)
    timezone: str = "UTC"
    admin_user: str = "admin"
    admin_password: str | None = None
    github_token: str | None = None
    github_repo: str = "Pumpkin-MC/Pumpkin"
    github_api_url: str = "https://api.github.com"
    update_check_minutes: int = 360
    session_hours: int = 24 * 7
    cookie_secure: str = "auto"  # auto | always | never
    public_address: str | None = None
    log_level: str = "INFO"
    api_docs: bool = False
    shutdown_timeout: int = 240  # a big modded world needs minutes to save when the container stops
    max_upload_bytes: int = 4096 * 1024 * 1024
    extra_env_passthrough: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Java and management ports are both TCP and handed out independently; overlapping pools could double-book one.
        (a_lo, a_hi), (b_lo, b_hi) = self.port_range, self.mgmt_port_range
        if a_lo <= b_hi and b_lo <= a_hi:
            raise ValueError(
                f"PORT_RANGE {a_lo}-{a_hi} and MGMT_PORT_RANGE {b_lo}-{b_hi} overlap; pick distinct ranges"
            )

    @property
    def servers_dir(self) -> Path:
        return self.data_dir / "servers"

    @property
    def binaries_dir(self) -> Path:
        return self.data_dir / "binaries"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def java_dir(self) -> Path:
        return self.data_dir / "java"  # Java runtimes, one folder per major version

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"  # downloaded mods and uploaded modpacks

    @property
    def db_path(self) -> Path:
        return self.data_dir / "manager.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        data_dir = Path(env.get("DATA_DIR", "/data")).expanduser()
        backup_dir = Path(env.get("BACKUP_DIR") or (data_dir / "backups")).expanduser()
        return cls(
            data_dir=data_dir,
            backup_dir=backup_dir,
            web_host=env.get("WEB_HOST", "0.0.0.0"),
            web_port=int(env.get("WEB_PORT", "8420")),
            port_range=_parse_range(env.get("PORT_RANGE", ""), (25565, 25575), "PORT_RANGE"),
            bedrock_port_range=_parse_range(
                env.get("BEDROCK_PORT_RANGE", ""), (19132, 19142), "BEDROCK_PORT_RANGE"
            ),
            mgmt_port_range=_parse_range(
                env.get("MGMT_PORT_RANGE", ""), (29000, 29999), "MGMT_PORT_RANGE"
            ),
            timezone=env.get("TZ", "UTC") or "UTC",
            admin_user=env.get("ADMIN_USER", "admin") or "admin",
            admin_password=env.get("ADMIN_PASSWORD") or None,
            github_token=env.get("GITHUB_TOKEN") or None,
            github_repo=env.get("PUMPKIN_REPO", "Pumpkin-MC/Pumpkin"),
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            update_check_minutes=max(5, int(env.get("UPDATE_CHECK_MINUTES", "360"))),
            session_hours=max(1, int(env.get("SESSION_HOURS", str(24 * 7)))),
            cookie_secure=env.get("COOKIE_SECURE", "auto").lower(),
            public_address=env.get("PUBLIC_ADDRESS") or None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            api_docs=_bool(env.get("ENABLE_API_DOCS"), False),
            shutdown_timeout=max(10, int(env.get("SHUTDOWN_TIMEOUT", "240"))),
            max_upload_bytes=max(1, int(env.get("MAX_UPLOAD_MB", "4096"))) * 1024 * 1024,
        )
