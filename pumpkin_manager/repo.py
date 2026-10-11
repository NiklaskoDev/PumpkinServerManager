"""Data access layer: every SQL statement of the application lives here."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .db import Database
from .models import AppSettings, BuildRecord, ServerPolicy, ServerRecord
from .util import iso, utcnow

_SERVER_COLUMNS = {
    "name", "channel", "pinned_tag", "build_id", "previous_build_id", "java_port", "bedrock_port",
    "mgmt_port", "mgmt_secret", "desired_running", "last_backup_at", "last_update_check_at",
    "pending_build_id", "pending_since",
}
_BACKUP_COLUMNS = {"size", "sha256", "verified", "protected", "note", "consistent", "file_name"}
_JOB_COLUMNS = {"status", "progress", "message", "steps", "finished_at", "title", "server_id"}
_UPDATE_COLUMNS = {"status", "message", "finished_at", "backup_id", "to_build_id", "to_tag"}
# Keys of the settings table that belong to one server ("<prefix>:<server id>"); removed together with the server.
PER_SERVER_KV = ("skip", "autofails", "autoattempt", "difficulty")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ settings
    def get_app_settings(self) -> AppSettings:
        raw = self.db.scalar("SELECT value FROM settings WHERE key='app'")
        return AppSettings.model_validate(json.loads(raw)) if raw else AppSettings()

    def save_app_settings(self, settings: AppSettings) -> None:
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES('app', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (settings.model_dump_json(),),
        )

    def get_kv(self, key: str) -> str | None:
        return self.db.scalar("SELECT value FROM settings WHERE key=?", (key,))

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --------------------------------------------------------------------- users
    def count_users(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users") or 0)

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM users WHERE username=?", (username,))

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM users WHERE id=?", (user_id,))

    def create_user(self, username: str, password_hash: str, kind: str = "admin", must_change: bool = False) -> int:
        cur = self.db.execute(
            "INSERT INTO users(username, password_hash, created_at, kind, must_change_password) VALUES(?,?,?,?,?)",
            (username, password_hash, iso(), kind, int(must_change)),
        )
        return int(cur.lastrowid)

    def set_password(self, user_id: int, password_hash: str, must_change: bool = False) -> None:
        """Store a new password hash. ``must_change`` marks it as a temporary one the user has to replace at the next login."""
        self.db.execute(
            "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?", (password_hash, int(must_change), user_id)
        )

    def touch_login(self, user_id: int) -> None:
        self.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso(), user_id))

    def list_users(self) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT id, username, kind, created_at, last_login_at, must_change_password FROM users ORDER BY kind, username COLLATE NOCASE"
        )

    def delete_user(self, user_id: int) -> None:
        self.db.execute("DELETE FROM users WHERE id=?", (user_id,))  # sessions and server assignments cascade

    # ------------------------------------------------------------ server access
    def user_assignments(self, user_id: int) -> dict[str, str]:
        rows = self.db.all("SELECT server_id, role FROM user_servers WHERE user_id=?", (user_id,))
        return {r["server_id"]: r["role"] for r in rows}

    def all_assignments(self) -> dict[int, dict[str, str]]:
        result: dict[int, dict[str, str]] = {}
        for r in self.db.all("SELECT user_id, server_id, role FROM user_servers"):
            result.setdefault(r["user_id"], {})[r["server_id"]] = r["role"]
        return result

    def set_user_servers(self, user_id: int, assignments: dict[str, str]) -> None:
        """Replace everything a user may access with ``{server id: role}``."""
        with self.db.tx():
            self.db.execute("DELETE FROM user_servers WHERE user_id=?", (user_id,))
            for server_id, role in assignments.items():
                self.db.execute(
                    "INSERT INTO user_servers(user_id, server_id, role) VALUES(?,?,?)", (user_id, server_id, role)
                )

    def users_with_server(self, server_id: str) -> set[int]:
        return {r["user_id"] for r in self.db.all("SELECT user_id FROM user_servers WHERE server_id=?", (server_id,))}

    # ------------------------------------------------------------------ sessions
    def create_session(
        self, token_hash: str, user_id: int, csrf: str, expires_at: str, ip: str | None, ua: str | None
    ) -> None:
        now = iso()
        self.db.execute(
            "INSERT INTO sessions(token_hash,user_id,csrf,created_at,expires_at,last_seen_at,ip,user_agent) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (token_hash, user_id, csrf, now, expires_at, now, ip, (ua or "")[:200]),
        )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT s.*, u.username, u.kind, u.must_change_password FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=?",
            (token_hash,),
        )

    def touch_session(self, token_hash: str, expires_at: str) -> None:
        self.db.execute(
            "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE token_hash=?", (iso(), expires_at, token_hash)
        )

    def delete_session(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_user_sessions(self, user_id: int, except_hash: str | None = None) -> None:
        self.db.execute(
            "DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user_id, except_hash or "")
        )

    def purge_sessions(self) -> int:
        return self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(),)).rowcount

    # -------------------------------------------------------------------- builds
    def upsert_build(self, b: dict[str, Any]) -> BuildRecord:
        cols = (
            "id channel tag version mc_version digest size asset_name asset_id download_url "
            "release_url published_at updated_at body"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "channel"))
        self.db.execute(
            f"INSERT INTO builds({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [b.get(c) for c in cols],
        )
        return self.get_build(b["id"])  # type: ignore[return-value]

    def get_build(self, build_id: str | None) -> BuildRecord | None:
        if not build_id:
            return None
        row = self.db.one("SELECT * FROM builds WHERE id=?", (build_id,))
        return BuildRecord.from_row(row) if row else None

    def list_builds(self) -> list[BuildRecord]:
        rows = self.db.all("SELECT * FROM builds ORDER BY COALESCE(updated_at, published_at) DESC")
        return [BuildRecord.from_row(r) for r in rows]

    def mark_downloaded(self, build_id: str, path: str) -> None:
        self.db.execute(
            "UPDATE builds SET downloaded=1, downloaded_at=?, path=? WHERE id=?", (iso(), path, build_id)
        )

    def mark_not_downloaded(self, build_id: str) -> None:
        self.db.execute("UPDATE builds SET downloaded=0, downloaded_at=NULL, path=NULL WHERE id=?", (build_id,))

    def delete_build(self, build_id: str) -> None:
        self.db.execute("DELETE FROM builds WHERE id=?", (build_id,))

    # ------------------------------------------------------------------- servers
    def insert_server(self, rec: ServerRecord) -> None:
        self.db.execute(
            "INSERT INTO servers(id,name,created_at,channel,pinned_tag,build_id,previous_build_id,java_port,"
            "bedrock_port,mgmt_port,mgmt_secret,policy,desired_running,last_backup_at,last_update_check_at,"
            "pending_build_id,pending_since,engine,spec) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.id, rec.name, rec.created_at, rec.channel, rec.pinned_tag, rec.build_id,
                rec.previous_build_id, rec.java_port, rec.bedrock_port, rec.mgmt_port, rec.mgmt_secret,
                rec.policy.model_dump_json(), int(rec.desired_running), rec.last_backup_at,
                rec.last_update_check_at, rec.pending_build_id, rec.pending_since, rec.engine, json.dumps(rec.spec),
            ),
        )

    def get_server(self, server_id: str) -> ServerRecord | None:
        row = self.db.one("SELECT * FROM servers WHERE id=?", (server_id,))
        return ServerRecord.from_row(row) if row else None

    def list_servers(self) -> list[ServerRecord]:
        return [ServerRecord.from_row(r) for r in self.db.all("SELECT * FROM servers ORDER BY created_at, id")]

    def update_server(self, server_id: str, **fields: Any) -> None:
        if "policy" in fields:
            policy = fields.pop("policy")
            fields["policy"] = (policy if isinstance(policy, ServerPolicy) else ServerPolicy.model_validate(policy)).model_dump_json()
        if "spec" in fields:
            fields["spec"] = json.dumps(fields["spec"])
        allowed = _SERVER_COLUMNS | {"policy", "spec"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown server column(s): {sorted(bad)}")
        if not fields:
            return
        assignments = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE servers SET {assignments} WHERE id=?", [*fields.values(), server_id])

    def delete_server(self, server_id: str) -> None:
        self.db.execute("DELETE FROM servers WHERE id=?", (server_id,))
        # Per-server bookkeeping must not leak into a later server that gets the same id.
        for prefix in PER_SERVER_KV:
            self.db.execute("DELETE FROM settings WHERE key=?", (f"{prefix}:{server_id}",))

    def used_ports(self) -> dict[str, set[int]]:
        rows = self.db.all("SELECT java_port, bedrock_port, mgmt_port FROM servers")
        return {
            "java": {r["java_port"] for r in rows},
            "bedrock": {r["bedrock_port"] for r in rows if r["bedrock_port"]},
            "mgmt": {r["mgmt_port"] for r in rows},
        }

    # ------------------------------------------------------------------- backups
    def insert_backup(self, **b: Any) -> dict[str, Any]:
        b.setdefault("id", new_id())
        b.setdefault("created_at", iso())
        cols = list(b)
        self.db.execute(
            f"INSERT INTO backups({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", list(b.values())
        )
        return self.get_backup(b["id"])  # type: ignore[return-value]

    def get_backup(self, backup_id: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM backups WHERE id=?", (backup_id,))

    def list_backups(self, server_id: str | None = None) -> list[dict[str, Any]]:
        if server_id:
            return self.db.all(
                "SELECT * FROM backups WHERE server_id=? ORDER BY created_at DESC, rowid DESC", (server_id,)
            )
        return self.db.all("SELECT * FROM backups ORDER BY created_at DESC, rowid DESC")

    def update_backup(self, backup_id: str, **fields: Any) -> None:
        bad = set(fields) - _BACKUP_COLUMNS
        if bad:
            raise ValueError(f"unknown backup column(s): {sorted(bad)}")
        if fields:
            self.db.execute(
                f"UPDATE backups SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                [*fields.values(), backup_id],
            )

    def delete_backup(self, backup_id: str) -> None:
        self.db.execute("DELETE FROM backups WHERE id=?", (backup_id,))

    # ---------------------------------------------------------------------- jobs
    def insert_job(self, kind: str, title: str, server_id: str | None) -> dict[str, Any]:
        job_id = new_id()
        self.db.execute(
            "INSERT INTO jobs(id,kind,server_id,title,status,created_at) VALUES(?,?,?,?, 'running', ?)",
            (job_id, kind, server_id, title, iso()),
        )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row:
            row["steps"] = json.loads(row["steps"] or "[]")
        return row

    def update_job(self, job_id: str, **fields: Any) -> None:
        bad = set(fields) - _JOB_COLUMNS
        if bad:
            raise ValueError(f"unknown job column(s): {sorted(bad)}")
        if "steps" in fields and not isinstance(fields["steps"], str):
            fields["steps"] = json.dumps(fields["steps"])
        if fields:
            self.db.execute(
                f"UPDATE jobs SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), job_id]
            )

    def list_jobs(
        self, limit: int = 50, server_id: str | None = None, server_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Newest first. ``server_ids`` restricts the result to jobs of those servers (an empty set matches nothing)."""
        if server_ids is not None:
            marks = ",".join("?" for _ in server_ids)
            rows = self.db.all(
                f"SELECT * FROM jobs WHERE server_id IN ({marks}) ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*sorted(server_ids), limit),
            ) if server_ids else []
        elif server_id:
            rows = self.db.all(
                "SELECT * FROM jobs WHERE server_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (server_id, limit)
            )
        else:
            rows = self.db.all("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        for row in rows:
            row["steps"] = json.loads(row["steps"] or "[]")
        return rows

    def fail_interrupted_jobs(self) -> int:
        cur = self.db.execute(
            "UPDATE jobs SET status='failed', message='Interrupted by a manager restart', finished_at=? "
            "WHERE status='running'",
            (iso(),),
        )
        return cur.rowcount

    def prune_jobs(self, keep: int = 300) -> None:
        self.db.execute(
            "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?)", (keep,)
        )

    # ------------------------------------------------------------------- updates
    def insert_update(self, **u: Any) -> str:
        u.setdefault("id", new_id())
        u.setdefault("started_at", iso())
        u.setdefault("status", "running")
        cols = list(u)
        self.db.execute(
            f"INSERT INTO updates({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", list(u.values())
        )
        return u["id"]

    def update_update(self, update_id: str, **fields: Any) -> None:
        bad = set(fields) - _UPDATE_COLUMNS
        if bad:
            raise ValueError(f"unknown update column(s): {sorted(bad)}")
        if fields:
            self.db.execute(
                f"UPDATE updates SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), update_id]
            )

    def list_updates(self, server_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT * FROM updates WHERE server_id=? ORDER BY started_at DESC, rowid DESC LIMIT ?", (server_id, limit)
        )

    def latest_update(self, server_id: str, statuses: tuple[str, ...] = ("success",)) -> dict[str, Any] | None:
        marks = ",".join("?" for _ in statuses)
        return self.db.one(
            f"SELECT * FROM updates WHERE server_id=? AND status IN ({marks}) ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (server_id, *statuses),
        )

    def interrupt_running_updates(self) -> int:
        cur = self.db.execute(
            "UPDATE updates SET status='interrupted', message='Interrupted by a manager restart', finished_at=? "
            "WHERE status='running'",
            (iso(),),
        )
        return cur.rowcount

    # ------------------------------------------------------------------ activity
    def add_activity(self, level: str, kind: str, message: str, server_id: str | None = None) -> dict[str, Any]:
        cur = self.db.execute(
            "INSERT INTO activity(ts, level, kind, server_id, message) VALUES(?,?,?,?,?)",
            (iso(utcnow()), level, kind, server_id, message[:1000]),
        )
        return {
            "id": cur.lastrowid, "ts": iso(), "level": level, "kind": kind, "server_id": server_id,
            "message": message[:1000],
        }

    def list_activity(
        self, limit: int = 100, server_id: str | None = None, server_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """Newest first. ``server_ids`` restricts the result to entries about those servers (an empty set matches nothing)."""
        if server_ids is not None:
            if not server_ids:
                return []
            marks = ",".join("?" for _ in server_ids)
            return self.db.all(
                f"SELECT * FROM activity WHERE server_id IN ({marks}) ORDER BY id DESC LIMIT ?", (*sorted(server_ids), limit)
            )
        if server_id:
            return self.db.all(
                "SELECT * FROM activity WHERE server_id=? ORDER BY id DESC LIMIT ?", (server_id, limit)
            )
        return self.db.all("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))

    def prune_activity(self, keep: int = 5000) -> None:
        self.db.execute(
            "DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT ?)", (keep,)
        )
