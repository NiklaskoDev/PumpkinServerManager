"""Accounts for friends: created by the administrator, with temporary passwords and a role per server."""

from __future__ import annotations

import asyncio
import re
import secrets
import sqlite3
from typing import Any

from .access import ROLES, AccessService
from .errors import UserError
from .events import EventBus
from .repo import Repo
from .reporter import Reporter
from .security import hash_password

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
# No 0/O, 1/l/I and the like: the password is read from a screen or a chat message and typed in once.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class AccountError(UserError):
    """An account could not be created or changed; the message is for the administrator."""


def generate_temporary_password() -> str:
    """Twelve random characters in three groups, for example ``k7Pq-Xw3m-Rb9d`` (about 70 bits)."""
    raw = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(12))
    return "-".join(raw[i : i + 4] for i in range(0, 12, 4))


class AccountService:
    def __init__(self, repo: Repo, access: AccessService, reporter: Reporter, events: EventBus):
        self.repo = repo
        self.access = access
        self.reporter = reporter
        self.events = events

    # ------------------------------------------------------------------------------------------------- views
    def _public(self, row: dict[str, Any], assignments: dict[str, str]) -> dict[str, Any]:
        return {
            "id": row["id"], "username": row["username"], "kind": row["kind"], "created_at": row["created_at"],
            "last_login_at": row["last_login_at"], "must_change_password": bool(row["must_change_password"]),
            "servers": assignments,
        }

    def list_all(self) -> list[dict[str, Any]]:
        assignments = self.repo.all_assignments()
        return [self._public(row, assignments.get(row["id"], {})) for row in self.repo.list_users()]

    def get(self, user_id: int) -> dict[str, Any]:
        row = self.repo.get_user(user_id)
        if row is None:
            raise AccountError("That account does not exist.", 404)
        return self._public(row, self.repo.user_assignments(user_id))

    def _friend(self, user_id: int) -> dict[str, Any]:
        row = self.repo.get_user(user_id)
        if row is None:
            raise AccountError("That account does not exist.", 404)
        if row["kind"] != "friend":
            raise AccountError("The administrator account is managed under Settings, not here.", 400)
        return row

    # ---------------------------------------------------------------------------------------------- validation
    def _clean_assignments(self, assignments: dict[str, str]) -> dict[str, str]:
        known = {rec.id for rec in self.repo.list_servers()}
        clean: dict[str, str] = {}
        for server_id, role in assignments.items():
            if server_id not in known:
                raise AccountError(f"The server '{server_id}' does not exist.", 422)
            if role not in ROLES:
                raise AccountError(f"'{role}' is not a role. Choose one of: {', '.join(ROLES)}.", 422)
            clean[server_id] = role
        return clean

    def _describe(self, assignments: dict[str, str]) -> str:
        names = {rec.id: rec.name for rec in self.repo.list_servers()}
        if not assignments:
            return "no servers"
        return ", ".join(f"{names.get(sid, sid)} ({role})" for sid, role in assignments.items())

    # -------------------------------------------------------------------------------------------- operations
    async def create(self, username: str, assignments: dict[str, str], by: str) -> tuple[dict[str, Any], str]:
        """Create a friend account. Returns it together with the temporary password, which is not stored anywhere readable."""
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise AccountError(
                "The username must be 3-32 characters: letters and digits, with '.', '_' or '-' inside, starting with a letter or digit.", 422
            )
        clean = self._clean_assignments(assignments)
        temporary = generate_temporary_password()
        hashed = await asyncio.to_thread(hash_password, temporary)
        try:
            with self.repo.db.tx():
                try:
                    user_id = self.repo.create_user(username, hashed, kind="friend", must_change=True)
                except sqlite3.IntegrityError:
                    raise AccountError(f"The username '{username}' is already taken.", 409) from None
                self.repo.set_user_servers(user_id, clean)
        except sqlite3.IntegrityError:  # a server was deleted between the check above and the insert
            raise AccountError("A server was deleted while the account was being created. Please try again.", 409) from None
        self.access.invalidate(user_id)
        self.reporter.record("info", "account", f"{by} created the account '{username}' with access to {self._describe(clean)}")
        return self.get(user_id), temporary

    def set_access(self, user_id: int, assignments: dict[str, str], by: str) -> dict[str, Any]:
        row = self._friend(user_id)
        clean = self._clean_assignments(assignments)
        self.repo.set_user_servers(user_id, clean)
        self.access.invalidate(user_id)
        self.events.publish("servers", "access", {"user_id": user_id}, audience={user_id})
        self.reporter.record("info", "account", f"{by} changed the access of '{row['username']}': {self._describe(clean)}")
        return self.get(user_id)

    async def reset_password(self, user_id: int, by: str) -> str:
        row = self._friend(user_id)
        temporary = generate_temporary_password()
        hashed = await asyncio.to_thread(hash_password, temporary)
        self.repo.set_password(user_id, hashed, must_change=True)
        self.repo.delete_user_sessions(user_id)  # whoever is signed in with the old password is signed out
        self.events.publish("servers", "access", {"user_id": user_id}, audience={user_id})  # ...and their live feed ends at once
        self.reporter.record("warning", "account", f"{by} gave '{row['username']}' a new temporary password")
        return temporary

    def delete(self, user_id: int, by: str) -> None:
        row = self._friend(user_id)
        self.repo.delete_user(user_id)
        self.access.invalidate(user_id)
        self.events.publish("servers", "access", {"user_id": user_id}, audience={user_id})
        self.reporter.record("warning", "account", f"{by} deleted the account '{row['username']}'")
