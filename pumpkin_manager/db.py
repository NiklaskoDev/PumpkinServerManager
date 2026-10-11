"""SQLite access: one shared connection, explicit transactions, tiny forward-only migrations."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_V1 = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_login_at TEXT
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  ip TEXT,
  user_agent TEXT
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE builds (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  tag TEXT NOT NULL,
  version TEXT,
  mc_version TEXT,
  digest TEXT NOT NULL,
  size INTEGER NOT NULL,
  asset_name TEXT NOT NULL,
  asset_id INTEGER,
  download_url TEXT NOT NULL,
  release_url TEXT,
  published_at TEXT,
  updated_at TEXT,
  body TEXT,
  downloaded INTEGER NOT NULL DEFAULT 0,
  downloaded_at TEXT,
  path TEXT
);

CREATE TABLE servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'stable',
  pinned_tag TEXT,
  build_id TEXT,
  previous_build_id TEXT,
  java_port INTEGER NOT NULL,
  bedrock_port INTEGER,
  mgmt_port INTEGER NOT NULL,
  mgmt_secret TEXT NOT NULL,
  policy TEXT NOT NULL DEFAULT '{}',
  desired_running INTEGER NOT NULL DEFAULT 0,
  last_backup_at TEXT,
  last_update_check_at TEXT,
  pending_build_id TEXT,
  pending_since TEXT
);

CREATE TABLE backups (
  id TEXT PRIMARY KEY,
  server_id TEXT NOT NULL,
  server_name TEXT,
  kind TEXT NOT NULL,
  file_name TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  created_at TEXT NOT NULL,
  build_id TEXT,
  build_tag TEXT,
  consistent INTEGER NOT NULL DEFAULT 1,
  verified INTEGER NOT NULL DEFAULT 0,
  protected INTEGER NOT NULL DEFAULT 0,
  note TEXT
);
CREATE INDEX idx_backups_server ON backups(server_id, created_at DESC);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  server_id TEXT,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  progress REAL,
  message TEXT,
  steps TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE updates (
  id TEXT PRIMARY KEY,
  server_id TEXT NOT NULL,
  from_build_id TEXT,
  to_build_id TEXT,
  from_tag TEXT,
  to_tag TEXT,
  backup_id TEXT,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_updates_server ON updates(server_id, started_at DESC);

CREATE TABLE activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  server_id TEXT,
  message TEXT NOT NULL
);
CREATE INDEX idx_activity_ts ON activity(ts DESC);
"""

# Accounts for friends: every existing user is the administrator (kind 'admin'); friends get a role per server.
SCHEMA_V2 = """
ALTER TABLE users ADD COLUMN kind TEXT NOT NULL DEFAULT 'admin';
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;

CREATE TABLE user_servers (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('player', 'moderator', 'admin')),
  PRIMARY KEY (user_id, server_id)
);
CREATE INDEX idx_user_servers_server ON user_servers(server_id);
"""

MIGRATIONS: list[str] = [SCHEMA_V1, SCHEMA_V2]


class Database:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._tx_depth = 0

    def migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version, script in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_script(script):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version = {version}")
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return row[0] if row else None

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Re-entrant transaction: only the outermost block issues BEGIN/COMMIT."""
        with self._lock:
            outermost = self._tx_depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outermost:
                    self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()


def _split_script(script: str) -> list[str]:
    # The schema contains no semicolons inside string literals, so a plain split is safe.
    return [s.strip() for s in script.split(";") if s.strip()]
