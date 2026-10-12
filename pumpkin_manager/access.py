"""Who may do what.

There are two kinds of accounts:

* the **administrator** (``kind == "admin"``, the account created on the first start) can do everything, including
  creating servers, deleting them and managing the other accounts;
* **friends** (``kind == "friend"``) only see the servers they were assigned to, and what they may do on each of them
  depends on the role they got *for that server*: player, moderator or admin (an admin of one server, not of the manager).

Every API route declares what it needs (see ``api/deps.py``); this module holds the vocabulary and the decisions, and
shapes what a friend gets to see of a server and of the live event stream. Unknown things are denied by default.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------------------------------------------ permissions
VIEW = "view"  # status: running or not, player count, CPU and memory, how to connect
CONSOLE_VIEW = "console.view"  # read the console output
ACTIVITY = "activity.view"  # the activity feed and background jobs of the server
START = "server.start"
MODERATE = "players.moderate"  # kick, ban and pardon (players and IPs), whitelist; see the player lists
CONTROL = "server.control"  # stop, restart, kill
CONSOLE_COMMAND = "console.command"
OPERATORS = "players.operators"  # make and unmake operators, set their level
FILES = "files"
SETTINGS = "settings"
BACKUPS = "backups"
VERSIONS = "versions"
DELETE = "server.delete"  # never part of a role: only the administrator may delete a server

PLAYER, MODERATOR, ADMIN = "player", "moderator", "admin"
ROLES: tuple[str, ...] = (PLAYER, MODERATOR, ADMIN)
OWNER = "owner"  # what the administrator account is shown as on every server

ROLE_PERMS: dict[str, frozenset[str]] = {
    PLAYER: frozenset({VIEW}),
    MODERATOR: frozenset({VIEW, CONSOLE_VIEW, ACTIVITY, START, MODERATE}),
    ADMIN: frozenset({
        VIEW, CONSOLE_VIEW, ACTIVITY, START, MODERATE, CONTROL, CONSOLE_COMMAND, OPERATORS, FILES, SETTINGS, BACKUPS, VERSIONS,
    }),
}
OWNER_PERMS: frozenset[str] = ROLE_PERMS[ADMIN] | {DELETE}
ALL_PERMS: frozenset[str] = OWNER_PERMS

ROLE_LABELS = {PLAYER: "Player", MODERATOR: "Moderator", ADMIN: "Admin"}

# What a player or moderator gets to see of a server; everything else (update policy, backup times, ...) is for admins.
_LIMITED_FIELDS = (
    "id", "name", "created_at", "state", "busy", "desired_running", "uptime", "exit_code", "channel", "build", "ports",
    "public_address", "players", "mc_version", "motd", "ping_ok", "stats", "disk_bytes", "engine", "modpack",
)
_MODPACK_MANAGEMENT = ("pending", "can_roll_back", "client_only", "client_only_count", "leave_out_client_only", "manual")
# Placeholders for the parts the dashboard expects to find on every server, so it never trips over a missing key.
_NEUTRAL_FIELDS: dict[str, Any] = {
    "pinned_tag": None,
    "java": None,  # how a Java server is launched (memory, options) is for those who manage it
    "previous_build": None,
    "policy": {},
    "last_backup_at": None,
    "msmp_ok": False,
    "last_error": None,
    "update": {"mode": None, "available": False, "pending_build": None, "pending_since": None, "last_check": None},
}


@dataclass(frozen=True)
class Principal:
    """The account behind a request, with what it may access."""

    user_id: int
    username: str
    kind: str
    roles: Mapping[str, str] = field(default_factory=dict)  # server id -> role (friends)

    @property
    def is_owner(self) -> bool:
        return self.kind == "admin"

    def role_for(self, server_id: str) -> str | None:
        return OWNER if self.is_owner else self.roles.get(server_id)

    def perms_for(self, server_id: str) -> frozenset[str]:
        if self.is_owner:
            return OWNER_PERMS
        return ROLE_PERMS.get(self.roles.get(server_id, ""), frozenset())

    def can(self, server_id: str, perm: str) -> bool:
        return perm in self.perms_for(server_id)

    def can_see(self, server_id: str) -> bool:
        return self.role_for(server_id) is not None

    def can_anywhere(self, perm: str) -> bool:
        return self.is_owner or any(perm in ROLE_PERMS.get(role, ()) for role in self.roles.values())

    def server_ids_with(self, perm: str) -> set[str] | None:
        """Ids of the servers where ``perm`` is granted; ``None`` means every server (the administrator)."""
        if self.is_owner:
            return None
        return {sid for sid, role in self.roles.items() if perm in ROLE_PERMS.get(role, ())}


class AccessService:
    """Looks up principals (cached: roles only change through this process) and filters what they may see."""

    def __init__(self, repo: Any):
        self.repo = repo
        self._cache: dict[int, Principal] = {}

    def principal(self, user_id: int) -> Principal | None:
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        row = self.repo.get_user(user_id)
        if row is None:
            return None
        roles = {} if row["kind"] == "admin" else self.repo.user_assignments(user_id)
        principal = Principal(user_id, row["username"], row["kind"], roles)
        self._cache[user_id] = principal
        return principal

    def invalidate(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(user_id, None)

    # ------------------------------------------------------------------------------------ what a server looks like
    @staticmethod
    def shape_server_view(principal: Principal, view: dict[str, Any]) -> dict[str, Any] | None:
        """The server as this account may see it (with an ``access`` entry telling the dashboard what to offer), or None."""
        server_id = view["id"]
        role = principal.role_for(server_id)
        if role is None:
            return None
        perms = principal.perms_for(server_id)
        if role in (OWNER, ADMIN):
            shaped = dict(view)
        else:
            shaped = {**copy.deepcopy(_NEUTRAL_FIELDS), **{key: view[key] for key in _LIMITED_FIELDS if key in view}}
            if shaped.get("modpack"):  # which modpack it is is for everybody; updates and undo are for those who manage it
                shaped["modpack"] = {k: v for k, v in shaped["modpack"].items() if k not in _MODPACK_MANAGEMENT}
            if CONSOLE_VIEW in perms:
                shaped["last_error"] = view.get("last_error")
        shaped["access"] = {"role": role, "can": sorted(perms)}
        return shaped

    # ------------------------------------------------------------------------------- what a live feed may carry
    def filter_event(self, user_id: int, message: dict[str, Any]) -> dict[str, Any] | None:
        """Decide whether an account gets a live event, and in which shape. Anything not listed here is dropped."""
        topic, kind, data = message["topic"], message["type"], message["data"]
        if topic == "servers" and kind == "access":
            # "Your access changed": only for the account it is about, even if it no longer exists (its live feed then ends).
            return message if data.get("user_id") == user_id else None
        principal = self.principal(user_id)
        if principal is None:
            return None
        if principal.is_owner:  # the administrator gets everything, and the server view with its ``access`` entry like everyone else
            if topic == "servers" and kind == "server":
                return {**message, "data": self.shape_server_view(principal, data)}
            return message
        if topic == "servers":
            if kind == "server":
                shaped = self.shape_server_view(principal, data)
                return None if shaped is None else {**message, "data": shaped}
            if kind == "stats":
                allowed = {sid: stats for sid, stats in data.items() if principal.can(sid, VIEW)}
                return {**message, "data": allowed} if allowed else None
            if kind == "deleted":
                return message  # addressed to the accounts that had the server by the publisher
            return None
        if topic in ("jobs", "activity"):
            server_id = data.get("server_id")
            return message if server_id and principal.can(server_id, ACTIVITY) else None
        if topic.startswith("console:"):
            return message if principal.can(topic.split(":", 1)[1], CONSOLE_VIEW) else None
        return None
