"""Authentication: password hashing, server-side sessions, CSRF tokens and login throttling."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from .repo import Repo
from .util import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

SESSION_COOKIE = "pumpkin_manager_session"
MIN_PASSWORD_LENGTH = 8

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 128 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32, maxmem=_SCRYPT_MAXMEM
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A hash of a random password, verified against when the username does not exist so that
# response timing does not reveal which usernames are valid.
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


class AuthError(Exception):
    """Raised for failed logins and invalid password changes; the message is safe to show."""


class Throttled(AuthError):
    def __init__(self, retry_after: int):
        super().__init__(f"Too many failed attempts. Try again in {retry_after} seconds.")
        self.retry_after = retry_after


class LoginThrottle:
    """Sliding-window failure counter with a temporary lockout, kept in memory."""

    def __init__(self, max_failures: int, window: int, lockout: int):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        until = self._locked_until.get(key)
        if until and until > now:
            raise Throttled(int(until - now) + 1)
        if until:
            del self._locked_until[key]

    def fail(self, key: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        recent.append(now)
        self._failures[key] = recent
        if len(recent) >= self.max_failures:
            self._locked_until[key] = now + self.lockout
            self._failures[key] = []

    def success(self, key: str) -> None:
        self._failures.pop(key, None)

    def cleanup(self) -> None:
        now = time.monotonic()
        for key in [k for k, t in self._locked_until.items() if t <= now]:
            del self._locked_until[key]
        for key in [k for k, v in self._failures.items() if not v or now - v[-1] > self.window]:
            del self._failures[key]


@dataclass(frozen=True)
class Session:
    token_hash: str
    user_id: int
    username: str
    csrf: str
    expires_at: str
    kind: str = "admin"  # "admin" (the administrator) or "friend"
    must_change: bool = False  # signed in with a temporary password that has to be replaced first


class AuthService:
    def __init__(self, repo: Repo, session_hours: int):
        self.repo = repo
        self.session_ttl = timedelta(hours=session_hours)
        self.ip_throttle = LoginThrottle(max_failures=8, window=600, lockout=600)
        self.user_throttle = LoginThrottle(max_failures=40, window=600, lockout=300)

    # ---------------------------------------------------------------- bootstrap
    async def ensure_admin(self, username: str, password: str | None) -> str | None:
        """Create the first account. Returns a generated password if one had to be invented."""
        if self.repo.count_users() > 0:
            return None
        generated = None
        if not password:
            generated = password = secrets.token_urlsafe(12)
        elif len(password) < MIN_PASSWORD_LENGTH:
            log.warning("ADMIN_PASSWORD is shorter than %d characters; generating a random one instead", MIN_PASSWORD_LENGTH)
            generated = password = secrets.token_urlsafe(12)
        hashed = await asyncio.to_thread(hash_password, password)
        self.repo.create_user(username, hashed)
        return generated

    async def reset_password(self, username: str, password: str, *, temporary: bool = False) -> bool:
        """Set a new password and sign the account out everywhere. ``temporary`` makes the user choose their own at the next login."""
        user = self.repo.get_user_by_name(username)
        if not user:
            return False
        hashed = await asyncio.to_thread(hash_password, password)
        self.repo.set_password(user["id"], hashed, must_change=temporary)
        self.repo.delete_user_sessions(user["id"])
        return True

    # -------------------------------------------------------------------- login
    async def login(self, username: str, password: str, ip: str, user_agent: str | None) -> tuple[str, Session]:
        username = username.strip()
        self.ip_throttle.check(f"ip:{ip}")
        self.user_throttle.check(f"user:{username.lower()}")

        user = self.repo.get_user_by_name(username)
        stored = user["password_hash"] if user else _dummy_hash()
        ok = await asyncio.to_thread(verify_password, password, stored)
        if not user or not ok:
            self.ip_throttle.fail(f"ip:{ip}")
            self.user_throttle.fail(f"user:{username.lower()}")
            await asyncio.sleep(0.4)
            raise AuthError("Invalid username or password.")

        self.ip_throttle.success(f"ip:{ip}")
        self.user_throttle.success(f"user:{username.lower()}")
        self.repo.touch_login(user["id"])

        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        expires = iso(utcnow() + self.session_ttl)
        self.repo.create_session(_token_hash(token), user["id"], csrf, expires, ip, user_agent)
        return token, Session(
            _token_hash(token), user["id"], user["username"], csrf, expires, user["kind"], bool(user["must_change_password"])
        )

    def resolve(self, token: str | None) -> Session | None:
        """Validate a session cookie and slide its expiry (at most once a minute)."""
        if not token or len(token) > 200:
            return None
        row = self.repo.get_session(_token_hash(token))
        if not row:
            return None
        now = utcnow()
        if parse_iso(row["expires_at"]) <= now:
            self.repo.delete_session(row["token_hash"])
            return None
        expires_at = row["expires_at"]
        if (now - parse_iso(row["last_seen_at"])).total_seconds() > 60:
            expires_at = iso(now + self.session_ttl)
            self.repo.touch_session(row["token_hash"], expires_at)
        return Session(
            row["token_hash"], row["user_id"], row["username"], row["csrf"], expires_at, row["kind"], bool(row["must_change_password"])
        )

    def logout(self, token: str | None) -> None:
        if token:
            self.repo.delete_session(_token_hash(token))

    async def change_password(self, session: Session, current: str, new: str) -> None:
        if len(new) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"The new password must be at least {MIN_PASSWORD_LENGTH} characters long.")
        user = self.repo.get_user(session.user_id)
        if not user or not await asyncio.to_thread(verify_password, current, user["password_hash"]):
            raise AuthError("The current password is not correct.")
        if new == current:
            raise AuthError("The new password must be different from the current one.")
        if new.strip().lower() == user["username"].lower():
            raise AuthError("The password must not be the same as the username.")
        self.repo.set_password(user["id"], await asyncio.to_thread(hash_password, new))  # also ends a "must change" state
        # Other devices must log in again with the new password.
        self.repo.delete_user_sessions(user["id"], except_hash=session.token_hash)

    def housekeeping(self) -> None:
        self.repo.purge_sessions()
        self.ip_throttle.cleanup()
        self.user_throttle.cleanup()


def csrf_matches(session: Session, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(session.csrf, supplied)  # type: ignore[arg-type]
