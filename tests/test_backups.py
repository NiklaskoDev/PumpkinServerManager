"""Backup archives: integrity, hostile archives, hot backups, restore, retention."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest
from conftest import create_server, finish

from pumpkin_manager.backups import BackupError, extract_archive, verify_archive, write_archive


def make_tree(root: Path) -> None:
    (root / "world" / "region").mkdir(parents=True)
    (root / "world" / "region" / "r.0.0.mca").write_bytes(os.urandom(50_000))
    (root / "pumpkin.toml").write_text("seed = '1'\n")
    (root / "logs").mkdir()
    (root / "logs" / "old.log").write_text("x" * 100)


def build_archive(src: Path, dest: Path, excludes=()) -> dict:
    return write_archive(src, dest, {"format": 1}, list(excludes), lambda n: None)


def test_archive_roundtrip_is_verified_and_restorable(tmp_path):
    src = tmp_path / "src"
    make_tree(src)
    result = build_archive(src, tmp_path / "a.tar.gz")
    info = verify_archive(tmp_path / "a.tar.gz", result["sha256"])
    assert info["manifest"] == {"format": 1} and result["files"] == 3

    out = tmp_path / "out"
    extract_archive(tmp_path / "a.tar.gz", out, 10**9)
    assert (out / "world" / "region" / "r.0.0.mca").read_bytes() == (src / "world" / "region" / "r.0.0.mca").read_bytes()
    assert not (out / ".pumpkin-manager").exists(), "the manifest is metadata, not server data"


def test_excludes_are_honoured(tmp_path):
    src = tmp_path / "src"
    make_tree(src)
    build_archive(src, tmp_path / "a.tar.gz", excludes=["logs"])
    out = tmp_path / "out"
    extract_archive(tmp_path / "a.tar.gz", out, 10**9)
    assert (out / "pumpkin.toml").exists() and not (out / "logs").exists()


def test_corruption_is_detected(tmp_path):
    src = tmp_path / "src"
    make_tree(src)
    result = build_archive(src, tmp_path / "a.tar.gz")
    data = bytearray((tmp_path / "a.tar.gz").read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        verify_archive(bad, result["sha256"])
    with pytest.raises(BackupError):
        verify_archive(bad, None)  # structure/CRC alone must catch it too

    truncated = tmp_path / "trunc.tar.gz"
    truncated.write_bytes((tmp_path / "a.tar.gz").read_bytes()[:-200])
    with pytest.raises(BackupError):
        verify_archive(truncated, None)


def evil_archive(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    import gzip

    with gzip.open(path, "wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for info, payload in members:
            tar.addfile(info, io.BytesIO(payload) if payload is not None else None)


def file_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    return info


def test_extraction_refuses_path_traversal_and_links(tmp_path):
    victim = tmp_path / "victim.txt"
    link = tarfile.TarInfo("sneaky")
    link.type, link.linkname = tarfile.SYMTYPE, str(tmp_path)
    hard = tarfile.TarInfo("hard")
    hard.type, hard.linkname = tarfile.LNKTYPE, "../../../etc/passwd"
    evil_archive(tmp_path / "evil.tar.gz", [
        (file_info("../victim.txt", 4), b"pwnd"),
        (file_info("../../victim2.txt", 4), b"pwnd"),
        (file_info("/tmp/absolute.txt", 4), b"pwnd"),
        (link, None),
        (hard, None),
        (file_info("fine.txt", 2), b"ok"),
    ])
    out = tmp_path / "out"
    try:
        extract_archive(tmp_path / "evil.tar.gz", out, 10**9)
    except BackupError:
        pass  # refusing the whole archive is acceptable ...
    assert not victim.exists() and not (tmp_path / "victim2.txt").exists()  # ... writing outside never is
    assert not (out / "sneaky").exists() and not (out / "hard").exists()
    assert not Path("/tmp/absolute.txt").exists() or Path("/tmp/absolute.txt").stat().st_size != 4


def test_extraction_respects_the_disk_budget(tmp_path):
    src = tmp_path / "src"
    make_tree(src)
    build_archive(src, tmp_path / "a.tar.gz")
    with pytest.raises(BackupError, match="free disk space"):
        extract_archive(tmp_path / "a.tar.gz", tmp_path / "out", 1000)


def test_a_file_that_shrinks_while_it_is_archived_keeps_the_archive_valid(tmp_path, monkeypatch):
    """A tar member must have exactly the size announced in its header, or everything after it is garbage."""
    from pumpkin_manager import backups as backups_module

    src = tmp_path / "src"
    make_tree(src)
    big = src / "world" / "region" / "r.0.0.mca"
    original = big.read_bytes()

    class ShrinksRightAfterItsSizeWasRecorded(backups_module._FixedSizeReader):
        def __init__(self, fh, size):
            super().__init__(fh, size)
            if size == len(original):  # the tar header already says 50 000 bytes; now the game truncates the file
                with open(big, "r+b") as other:
                    other.truncate(5)

    monkeypatch.setattr(backups_module, "_FixedSizeReader", ShrinksRightAfterItsSizeWasRecorded)
    result = write_archive(src, tmp_path / "a.tar.gz", {}, [], lambda n: None)
    verify_archive(tmp_path / "a.tar.gz", result["sha256"])  # still a perfectly readable archive
    assert result["changed"] == ["world/region/r.0.0.mca"], "the file is reported as having changed"

    out = tmp_path / "out"
    extract_archive(tmp_path / "a.tar.gz", out, 10**9)
    restored = (out / "world" / "region" / "r.0.0.mca").read_bytes()
    assert len(restored) == len(original), "padded to the announced size"
    assert restored[:5] == original[:5] and set(restored[5:]) == {0}
    assert (out / "pumpkin.toml").read_text() == "seed = '1'\n", "files after the changed one are intact"


def test_fixed_size_reader_pads_and_truncates():
    import io

    from pumpkin_manager.backups import _FixedSizeReader

    short = _FixedSizeReader(io.BytesIO(b"abc"), 6)
    assert short.read(100) == b"abc\0\0\0" and short.short and short.read(10) == b""
    long = _FixedSizeReader(io.BytesIO(b"abcdefgh"), 4)
    assert long.read() == b"abcd" and not long.short


async def test_hot_backup_quiesces_the_server_and_resumes_saving(ctx):
    server = await create_server(ctx, "Hot")
    job = ctx.backups.submit_backup(server, "manual", "hello")
    row = await finish(ctx, job)
    assert row["status"] == "success", row["message"]
    backup = ctx.repo.list_backups(server.id)[0]
    assert backup["kind"] == "manual" and backup["verified"] and backup["consistent"] and backup["note"] == "hello"
    log = [line["m"] for line in ctx.pm.backlog(server.id, 100)]
    # the order matters: pause autosave before copying, resume afterwards
    off = max(i for i, m in enumerate(log) if "Automatic saving is now disabled" in m)
    on = max(i for i, m in enumerate(log) if "Automatic saving is now enabled" in m)
    assert off < on
    assert ctx.pm.runtime(server.id).state == "running", "hot backups do not stop the server"


async def test_cold_backup_mode_stops_and_restarts(ctx):
    server = await create_server(ctx, "Cold", policy={"backup_mode": "cold"})
    pid = ctx.pm.runtime(server.id).pid
    row = await finish(ctx, ctx.backups.submit_backup(ctx.repo.get_server(server.id), "manual"))
    assert row["status"] == "success", row["message"]
    assert ctx.pm.runtime(server.id).state == "running" and ctx.pm.runtime(server.id).pid != pid


async def test_restore_replaces_files_and_keeps_a_safety_backup(ctx):
    server = await create_server(ctx, "Restorable")
    await finish(ctx, ctx.backups.submit_backup(server, "manual"))
    original = ctx.repo.list_backups(server.id)[0]

    marker = ctx.settings.servers_dir / server.id / "world" / "marker.txt"
    await finish(ctx, ctx.servers.submit_stop(server))
    marker.write_text("created after the backup")

    job = ctx.backups.submit_restore(ctx.repo.get_server(server.id), original["id"], True, lambda r: ctx.servers.ensure_config(r))
    row = await finish(ctx, job)
    assert row["status"] == "success", row["message"]
    assert not marker.exists(), "files that did not exist in the backup are gone"
    kinds = sorted(b["kind"] for b in ctx.repo.list_backups(server.id))
    assert kinds == ["manual", "pre-restore"]
    safety = next(b for b in ctx.repo.list_backups(server.id) if b["kind"] == "pre-restore")
    out = ctx.settings.backup_dir / "check"
    extract_archive(ctx.settings.backup_dir / safety["file_name"], out, 10**9)
    assert (out / "world" / "marker.txt").read_text() == "created after the backup"
    assert not list(ctx.settings.servers_dir.glob(".*restore-*")) and not list(ctx.settings.servers_dir.glob(".*old-*"))


async def test_restore_reasserts_managed_ports(ctx):
    server = await create_server(ctx, "Ports")
    await finish(ctx, ctx.backups.submit_backup(server, "manual"))
    backup = ctx.repo.list_backups(server.id)[0]
    ctx.servers.patch(ctx.repo.get_server(server.id), {"java_port": server.java_port + 3})
    await finish(ctx, ctx.servers.submit_stop(ctx.repo.get_server(server.id)))
    await finish(ctx, ctx.backups.submit_restore(ctx.repo.get_server(server.id), backup["id"], False, lambda r: ctx.servers.ensure_config(r)))
    import tomllib

    cfg = tomllib.loads((ctx.settings.servers_dir / server.id / "pumpkin.toml").read_text())
    assert cfg["networking"]["java"]["address"].endswith(str(server.java_port + 3)), "the manager's port wins over the old file"


async def test_retention_keeps_newest_and_spares_protected(ctx):
    server = await create_server(ctx, "Retention", policy={"backup_keep": 2, "update_mode": "off"})
    ids = []
    for i in range(4):
        await finish(ctx, ctx.backups.submit_backup(ctx.repo.get_server(server.id), "manual", f"b{i}"))
        ids.append(ctx.repo.list_backups(server.id)[0]["id"])
        if i == 0:
            ctx.repo.update_backup(ids[0], protected=1)
    notes = sorted(b["note"] for b in ctx.repo.list_backups(server.id))
    assert notes == ["b0", "b2", "b3"], "b0 is protected, only the two newest others remain"


async def test_reconcile_picks_up_files_and_drops_missing_ones(ctx):
    server = await create_server(ctx, "Index")
    await finish(ctx, ctx.backups.submit_backup(server, "manual"))
    row = ctx.repo.list_backups(server.id)[0]
    (ctx.settings.backup_dir / row["file_name"]).rename(ctx.settings.backup_dir / server.id / "copied-in.tar.gz")
    result = ctx.backups.reconcile()
    assert result == {"added": 1, "removed": 1}
    assert ctx.repo.list_backups(server.id)[0]["file_name"].endswith("copied-in.tar.gz")


async def test_backup_aborts_when_disk_is_too_full(ctx, monkeypatch):
    server = await create_server(ctx, "Full")
    from pumpkin_manager import backups as backups_module

    monkeypatch.setattr(backups_module, "free_bytes", lambda path: 1000)
    row = await finish(ctx, ctx.backups.submit_backup(server, "manual"))
    assert row["status"] == "failed" and "free space" in row["message"]
    assert not list(ctx.settings.backup_dir.glob("*/*.partial"))
