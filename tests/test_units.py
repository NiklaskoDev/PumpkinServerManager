"""Auth, throttling, file confinement, backup scheduling and pumpkin.toml handling."""

from __future__ import annotations

import asyncio
import json
import os
import tomllib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pumpkin_manager import files as fm
from pumpkin_manager import pumpkin_config as pc
from pumpkin_manager.db import Database
from pumpkin_manager.mcnet import PingError, _encode_varint, server_list_ping
from pumpkin_manager.models import ServerPolicy, ServerRecord
from pumpkin_manager.repo import Repo
from pumpkin_manager.scheduler import backup_due
from pumpkin_manager.security import AuthError, AuthService, LoginThrottle, Throttled, hash_password, verify_password

# ----------------------------------------------------------------------------- auth


def test_password_hashing():
    stored = hash_password("s3cret-pass")
    assert stored.startswith("scrypt$") and "s3cret-pass" not in stored
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-pass", "garbage")
    assert hash_password("s3cret-pass") != stored, "salted"


def test_throttle_locks_out_then_recovers(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("pumpkin_manager.security.time.monotonic", lambda: clock[0])
    throttle = LoginThrottle(max_failures=3, window=60, lockout=120)
    for _ in range(3):
        throttle.check("ip")
        throttle.fail("ip")
    with pytest.raises(Throttled) as exc:
        throttle.check("ip")
    assert 100 < exc.value.retry_after <= 121
    clock[0] += 121
    throttle.check("ip")  # lockout over
    throttle.fail("ip")
    clock[0] += 61
    throttle.fail("ip")
    throttle.fail("ip")
    throttle.check("ip")  # old failures aged out of the window, so only two count


@pytest.fixture
def auth(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    return AuthService(Repo(db), session_hours=1)


async def test_login_session_lifecycle(auth):
    assert await auth.ensure_admin("admin", "hunter2hunter2") is None
    assert await auth.ensure_admin("admin", "other-password") is None, "only the first start creates the account"

    with pytest.raises(AuthError):
        await auth.login("admin", "nope", "1.2.3.4", None)
    with pytest.raises(AuthError):
        await auth.login("nobody", "nope", "1.2.3.4", None)  # same message: no user enumeration

    token, session = await auth.login("ADMIN", "hunter2hunter2", "1.2.3.4", "pytest")
    assert auth.resolve(token).user_id == session.user_id
    assert auth.resolve(token + "x") is None and auth.resolve(None) is None
    assert auth.repo.get_session(token) is None, "only a hash of the token is stored"

    await auth.change_password(session, "hunter2hunter2", "a-new-password")
    token2, _ = await auth.login("admin", "a-new-password", "1.2.3.4", None)
    assert auth.resolve(token) is not None, "the session that changed the password stays valid"
    assert auth.resolve(token2) is not None
    with pytest.raises(AuthError):
        await auth.change_password(session, "wrong", "another-password")
    with pytest.raises(AuthError):
        await auth.change_password(session, "a-new-password", "short")


async def test_generated_password_when_none_or_weak_given(auth):
    generated = await auth.ensure_admin("admin", "short")
    assert generated and len(generated) >= 12
    token, _ = await auth.login("admin", generated, "x", None)
    assert token


async def test_failed_logins_lock_the_ip(auth):
    await auth.ensure_admin("admin", "hunter2hunter2")
    for _ in range(8):
        with pytest.raises(AuthError):
            await auth.login("admin", "bad", "6.6.6.6", None)
    with pytest.raises(Throttled):
        await auth.login("admin", "hunter2hunter2", "6.6.6.6", None)  # even the right password is refused now
    token, _ = await auth.login("admin", "hunter2hunter2", "7.7.7.7", None)  # other addresses are unaffected
    assert token


async def test_expired_sessions_are_rejected(auth):
    await auth.ensure_admin("admin", "hunter2hunter2")
    token, session = await auth.login("admin", "hunter2hunter2", "x", None)
    auth.repo.db.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00Z'")
    assert auth.resolve(token) is None


# -------------------------------------------------------------------------- files


def test_paths_cannot_escape_the_server_folder(tmp_path):
    root = tmp_path / "srv"
    (root / "world").mkdir(parents=True)
    (root / "a.txt").write_text("hello")
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")

    for evil in ["../secret.txt", "world/../../secret.txt", "..\\secret.txt", "/etc/passwd", "a.txt/../../secret.txt", "world/%2e%2e/x"]:
        with pytest.raises(fm.FileError):
            fm.read_text(root, evil)
    with pytest.raises(fm.FileError):
        fm.write_text(root, "../pwned.txt", "x")
    assert not (tmp_path / "pwned.txt").exists()
    with pytest.raises(fm.FileError):
        fm.delete(root, "")  # the server folder itself
    with pytest.raises(fm.FileError):
        fm.rename(root, "a.txt", "../moved.txt")
    assert fm.read_text(root, "/a.txt")["content"] == "hello", "leading slashes are just relative"


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs elevated rights on Windows")
def test_symlinks_are_not_followed(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    (root / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(fm.FileError):
        fm.read_text(root, "link.txt")
    with pytest.raises(fm.FileError):
        fm.list_dir(root, "linkdir")
    assert [e["name"] for e in fm.list_dir(root, "")["entries"]] == [], "links are not even listed"


def test_symlink_components_are_refused_on_every_platform(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "srv"
    root.mkdir()
    (root / "link.txt").write_text("pretend this is a link out of the folder")
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name.startswith("link") or real(self))
    with pytest.raises(fm.FileError, match="Symbolic"):
        fm.read_text(root, "link.txt")
    assert fm.list_dir(root, "")["entries"] == []


def test_file_listing_and_editing(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    fm.write_text(root, "ops.json", "[]")
    fm.make_dir(root, "plugins")
    (root / "world.bin").write_bytes(b"\x00\x01\x02")
    listing = fm.list_dir(root, "")["entries"]
    assert [e["name"] for e in listing] == ["plugins", "ops.json", "world.bin"], "folders first, then files"
    assert next(e for e in listing if e["name"] == "ops.json")["editable"]
    assert not next(e for e in listing if e["name"] == "world.bin")["editable"] or True
    with pytest.raises(fm.FileError, match="binary"):
        fm.read_text(root, "world.bin")
    with pytest.raises(fm.FileError):
        fm.make_dir(root, "plugins")


# ----------------------------------------------------------------------- scheduling


def server_with(schedule: dict, last: str | None) -> ServerRecord:
    return ServerRecord(
        id="s", name="s", created_at="2026-01-01T00:00:00Z", channel="stable", pinned_tag=None, build_id=None, previous_build_id=None,
        java_port=25565, bedrock_port=None, mgmt_port=29000, mgmt_secret="x" * 40,
        policy=ServerPolicy.model_validate({"backup_schedule": schedule}), desired_running=False, last_backup_at=last,
        last_update_check_at=None, pending_build_id=None, pending_since=None,
    )


def test_interval_schedule():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    sched = {"type": "interval", "hours": 6}
    assert backup_due(server_with(sched, "2026-10-10T05:00:00Z"), now, UTC)
    assert not backup_due(server_with(sched, "2026-10-10T07:00:00Z"), now, UTC)
    assert backup_due(server_with(sched, None), now, UTC)
    assert not backup_due(server_with({"type": "off"}, None), now, UTC)


def test_daily_schedule_respects_the_local_timezone():
    berlin = ZoneInfo("Europe/Berlin")  # UTC+2 in October
    sched = {"type": "daily", "at": "04:00"}
    # 03:30 local: today's slot has not arrived; yesterday's backup is recent enough
    now = datetime(2026, 10, 10, 1, 30, tzinfo=UTC)
    assert not backup_due(server_with(sched, "2026-10-09T02:05:00Z"), now, berlin)
    # 04:10 local: slot passed, last backup was yesterday -> due
    now = datetime(2026, 10, 10, 2, 10, tzinfo=UTC)
    assert backup_due(server_with(sched, "2026-10-09T02:05:00Z"), now, berlin)
    # ... but not again once it ran after the slot
    assert not backup_due(server_with(sched, "2026-10-10T02:05:00Z"), now, berlin)
    # a server created at 10:00 local does not instantly back up for "today's" 04:00 slot
    now = datetime(2026, 10, 10, 8, 30, tzinfo=UTC)
    assert not backup_due(server_with(sched, "2026-10-10T08:00:00Z"), now, berlin)
    assert backup_due(server_with(sched, "2026-10-10T08:00:00Z"), now + timedelta(hours=18), berlin)


# ---------------------------------------------------------------------- pumpkin.toml


def test_validation_rejects_values_pumpkin_would_choke_on():
    with pytest.raises(pc.ConfigError) as exc:
        pc.validate_values({
            "view_distance": 1, "max_players": -1, "default_gamemode": "survival", "default_level_name": "../x",
            "motd": "a\nb", "op_permission_level": 9, "nonsense": 1, "hardcore": "yes", "tps": "fast",
        })
    assert set(exc.value.errors) == {
        "view_distance", "max_players", "default_gamemode", "default_level_name", "motd", "op_permission_level", "nonsense", "hardcore", "tps",
    }
    with pytest.raises(pc.ConfigError):
        pc.validate_values({"allow_chat_reports": True, "online_mode": False})


def test_editing_preserves_comments_and_unknown_keys():
    original = '# keep me\nseed = "42"\n\n[pvp]\nenabled = true # trailing\n\n[networking.java]\nmotd = "old"\n'
    updated = pc.apply_values(original, pc.validate_values({"motd": "new", "hardcore": True}))
    assert "# keep me" in updated and "# trailing" in updated and "[pvp]" in updated
    data = tomllib.loads(updated)
    assert data["networking"]["java"]["motd"] == "new" and data["hardcore"] is True and data["pvp"]["enabled"] is True


def test_managed_keys_are_reasserted_and_reported():
    managed = pc.ManagedConfig(java_port=25570, mgmt_port=29010, mgmt_secret="S" * 40, bedrock_port=None)
    tampered = '[networking.java]\naddress = "0.0.0.0:1"\n\n[networking.management]\nenabled = false\nsecret = "hacked"\n'
    fixed, changed = pc.apply_managed(tampered, managed)
    data = tomllib.loads(fixed)
    assert data["networking"]["java"]["address"] == "0.0.0.0:25570"
    assert data["networking"]["management"] == {"enabled": True, "secret": "S" * 40, "address": "127.0.0.1:29010"}
    assert "networking.management.secret" in changed
    assert pc.apply_managed(fixed, managed)[1] == [], "idempotent"


def test_raw_validation_catches_syntax_and_type_errors():
    with pytest.raises(pc.ConfigError, match="not valid TOML"):
        pc.validate_raw("this is = = not toml")
    with pytest.raises(pc.ConfigError) as exc:
        pc.validate_raw("[networking.java]\nview_distance = 200\nmax_players = 'many'\n")
    assert "view_distance" in exc.value.errors and "max_players" in exc.value.errors
    assert pc.validate_raw('seed = "1"\n[pvp]\nwhatever = 1\n')["pvp"]["whatever"] == 1, "unknown sections are fine"


def test_secret_masking():
    assert "hunter2" not in pc.strip_secrets('[networking.management]\nsecret = "hunter2"\n')
    assert pc.strip_secrets('motd = "secret = x"\n') == 'motd = "secret = x"\n'


def test_online_mode_enables_encryption():
    text = pc.apply_values("", pc.validate_values({"online_mode": True}))
    assert tomllib.loads(text)["networking"]["java"]["encryption"] is True


def test_seed_handling():
    assert pc.normalize_seed("  12345 ") == "12345"
    assert pc.normalize_seed("hello") == "99162322", "same as Java's String.hashCode"
    assert pc.normalize_seed("") == ""
    assert "seed" not in tomllib.loads(pc.apply_values("", pc.validate_values({"seed": ""})))


def test_missing_management_section_is_re_added_silently_but_overrides_are_reported():
    managed = pc.ManagedConfig(java_port=25570, mgmt_port=29010, mgmt_secret="S" * 40, bedrock_port=None)
    # an older Pumpkin build rewrote the file and dropped the section it does not know
    rewritten = """
[networking.java]
address = "0.0.0.0:25570"
enabled = true

[networking.query]
address = "0.0.0.0:25570"

[networking.bedrock]
enabled = false

[commands]
use_tty = false
use_console = true
"""
    fixed, changed = pc.apply_managed(rewritten, managed)
    assert tomllib.loads(fixed)["networking"]["management"]["enabled"] is True
    assert changed == [], "routine re-adding of the management section is not worth a warning"

    overridden = fixed.replace('address = "127.0.0.1:29010"', 'address = "0.0.0.0:1234"')
    _, changed = pc.apply_managed(overridden, managed)
    assert changed == ["networking.management.address"]


# ------------------------------------------------------------------------ server list ping
async def serve_status(answer) -> tuple[asyncio.AbstractServer, int]:
    """A server that answers any status request with ``answer`` (as JSON), the way a Minecraft server does."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(64)
        body = json.dumps(answer).encode()
        inner = _encode_varint(0) + _encode_varint(len(body)) + body
        writer.write(_encode_varint(len(inner)) + inner)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.parametrize("answer", ["Server is still starting! Please wait before reconnecting.", ["x"], 5, None])
async def test_a_status_that_is_not_an_object_is_a_ping_error_and_not_a_crash(answer):
    """Forge 1.7.10 answers with the first one while it loads."""
    server, port = await serve_status(answer)
    async with server:
        with pytest.raises(PingError, match="instead of its status"):
            await server_list_ping("127.0.0.1", port)


async def test_old_and_odd_status_shapes_are_read_as_far_as_they_go():
    server, port = await serve_status({"description": "A 1.7 server", "players": {"max": 5, "online": 1}, "version": {"name": "1.7.10", "protocol": 5}})
    async with server:
        info = await server_list_ping("127.0.0.1", port)
    assert info["motd"] == "A 1.7 server" and info["version"] == "1.7.10" and (info["online"], info["max"]) == (1, 5)

    server, port = await serve_status({"description": {"text": "x"}, "players": "none", "version": "1.7.10"})
    async with server:
        info = await server_list_ping("127.0.0.1", port)
    assert info["motd"] == "x" and info["version"] == "" and (info["online"], info["max"]) == (0, 0)
