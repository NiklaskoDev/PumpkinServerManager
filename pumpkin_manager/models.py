"""Domain models: per-server policy, global settings and the records stored in SQLite."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Channel = Literal["stable", "nightly", "canary"]
CHANNELS: tuple[str, ...] = ("stable", "nightly", "canary")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class BackupSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["off", "interval", "daily"] = "daily"
    hours: int = Field(24, ge=1, le=24 * 30)
    at: str = "04:00"

    @field_validator("at")
    @classmethod
    def _check_time(cls, value: str) -> str:
        if not _HHMM.match(value):
            raise ValueError("time must be HH:MM (24h)")
        return value


class ServerPolicy(BaseModel):
    """Everything the manager (not Pumpkin) decides about how a server is run."""

    model_config = ConfigDict(extra="ignore")

    autostart: Literal["always", "last", "never"] = "last"
    restart_on_crash: bool = True
    stop_timeout: int = Field(60, ge=5, le=600)
    start_timeout: int = Field(180, ge=15, le=1800)
    update_mode: Literal["off", "notify", "auto"] = "auto"
    update_only_when_empty: bool = True
    update_max_defer_hours: int = Field(24, ge=0, le=720)
    backup_schedule: BackupSchedule = Field(default_factory=BackupSchedule)
    backup_keep: int = Field(7, ge=1, le=365)
    backup_keep_pre_update: int = Field(5, ge=1, le=100)
    backup_mode: Literal["hot", "cold"] = "hot"
    backup_exclude: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("backup_exclude")
    @classmethod
    def _check_excludes(cls, value: list[str]) -> list[str]:
        cleaned = []
        for pattern in value:
            pattern = pattern.strip().replace("\\", "/").lstrip("/")
            if not pattern or ".." in pattern.split("/") or len(pattern) > 200:
                raise ValueError(f"invalid exclude pattern: {pattern!r}")
            cleaned.append(pattern)
        return cleaned


class AppSettings(BaseModel):
    """Global settings editable from the dashboard (stored as one JSON document)."""

    model_config = ConfigDict(extra="ignore")

    update_check_enabled: bool = True
    update_check_minutes: int = Field(360, ge=5, le=60 * 24 * 7)
    default_channel: Channel = "stable"
    github_token: str | None = None
    webhook_url: str | None = None
    webhook_events: list[str] = Field(
        default_factory=lambda: ["update_applied", "update_failed", "update_available", "backup_failed", "crashed"]
    )
    telemetry_default: bool = False
    public_address: str | None = None
    keep_old_builds: int = Field(1, ge=0, le=10)

    @field_validator("webhook_url")
    @classmethod
    def _check_webhook(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not re.match(r"^https?://[^\s]+$", value) or len(value) > 500:
            raise ValueError("webhook URL must start with http:// or https://")
        return value


@dataclass
class BuildRecord:
    id: str
    channel: str
    tag: str
    version: str | None
    mc_version: str | None
    digest: str
    size: int
    asset_name: str
    asset_id: int | None
    download_url: str
    release_url: str | None
    published_at: str | None
    updated_at: str | None
    body: str | None
    downloaded: bool
    downloaded_at: str | None
    path: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "BuildRecord":
        return cls(**{**row, "downloaded": bool(row["downloaded"])})

    @property
    def label(self) -> str:
        """Human label for UIs and logs: 'stable 0.2.0 (MC 26.3)' / 'nightly 3b9d9709'."""
        if self.channel == "stable" and self.version:
            suffix = f" · MC {self.mc_version}" if self.mc_version else ""
            return f"{self.version}{suffix}"
        return f"{self.tag} ({self.digest[:8]})"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "tag": self.tag,
            "version": self.version,
            "mc_version": self.mc_version,
            "digest": self.digest,
            "size": self.size,
            "release_url": self.release_url,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "downloaded": self.downloaded,
            "downloaded_at": self.downloaded_at,
            "label": self.label,
        }


@dataclass
class ServerRecord:
    id: str
    name: str
    created_at: str
    channel: str
    pinned_tag: str | None
    build_id: str | None
    previous_build_id: str | None
    java_port: int
    bedrock_port: int | None
    mgmt_port: int
    mgmt_secret: str
    policy: ServerPolicy
    desired_running: bool
    last_backup_at: str | None
    last_update_check_at: str | None
    pending_build_id: str | None
    pending_since: str | None
    # "pumpkin" servers run a downloaded Pumpkin build (build_id, channel and the pending_* fields describe it).
    # "java" servers run a Java server (a modpack): ``spec`` says which one and how it is started.
    engine: str = "pumpkin"
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def is_java(self) -> bool:
        return self.engine == "java"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ServerRecord":
        data = dict(row)
        data["policy"] = ServerPolicy.model_validate(json.loads(data["policy"] or "{}"))
        data["desired_running"] = bool(data["desired_running"])
        data["spec"] = json.loads(data.get("spec") or "{}")
        return cls(**data)
