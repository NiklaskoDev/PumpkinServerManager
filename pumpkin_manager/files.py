"""File browser operations confined to one server directory."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .errors import UserError
from .util import atomic_write_text, is_within

MAX_EDIT_BYTES = 1024 * 1024
TEXT_EXTENSIONS = {
    ".toml", ".json", ".txt", ".log", ".yml", ".yaml", ".properties", ".cfg", ".conf", ".ini", ".md", ".csv", ".mcmeta", ".lang",
}


class FileError(UserError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message, status)


def resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Map a user supplied relative path to a real path inside ``root`` (never outside, no symlinks)."""
    if "\x00" in rel:
        raise FileError("Invalid path.")
    rel = rel.replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileError("Invalid path.")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise FileError("Symbolic links are not allowed.", 403)
    if not is_within(current, root):
        raise FileError("Invalid path.")
    if must_exist and not os.path.lexists(current):
        raise FileError("File or folder not found.", 404)
    return current


def rel_of(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    return "" if str(rel) == "." else rel.as_posix()


def _entry(root: Path, path: Path) -> dict[str, Any]:
    st = path.lstat()
    is_dir = stat.S_ISDIR(st.st_mode)
    return {
        "name": path.name,
        "path": rel_of(root, path),
        "type": "dir" if is_dir else "file",
        "size": 0 if is_dir else st.st_size,
        "modified": int(st.st_mtime),
        "editable": (not is_dir) and st.st_size <= MAX_EDIT_BYTES and (path.suffix.lower() in TEXT_EXTENSIONS or path.suffix == ""),
    }


def list_dir(root: Path, rel: str) -> dict[str, Any]:
    target = resolve(root, rel)
    if not target.is_dir():
        raise FileError("Not a folder.")
    entries = []
    with os.scandir(target) as it:
        for item in it:
            p = Path(item.path)
            if p.is_symlink():
                continue
            try:
                entries.append(_entry(root, p))
            except OSError:
                continue
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": rel_of(root, target), "entries": entries}


def read_text(root: Path, rel: str) -> dict[str, Any]:
    target = resolve(root, rel)
    if not target.is_file():
        raise FileError("Not a file.")
    size = target.stat().st_size
    if size > MAX_EDIT_BYTES:
        raise FileError(f"This file is larger than {MAX_EDIT_BYTES // 1024} KiB; download it instead.", 413)
    raw = target.read_bytes()
    if b"\x00" in raw:
        raise FileError("This looks like a binary file; download it instead.", 415)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FileError("This file is not UTF-8 text; download it instead.", 415) from None
    return {"path": rel_of(root, target), "content": text, "size": size, "modified": int(target.stat().st_mtime)}


def write_text(root: Path, rel: str, content: str) -> Path:
    target = resolve(root, rel, must_exist=False)
    if target.exists() and not target.is_file():
        raise FileError("Not a file.")
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise FileError(f"Edits are limited to {MAX_EDIT_BYTES // 1024} KiB.", 413)
    if not target.parent.is_dir():
        raise FileError("The parent folder does not exist.", 404)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    atomic_write_text(target, content, mode)
    return target


def make_dir(root: Path, rel: str) -> Path:
    target = resolve(root, rel, must_exist=False)
    if target.exists():
        raise FileError("That name already exists.", 409)
    if not target.parent.is_dir():
        raise FileError("The parent folder does not exist.", 404)
    target.mkdir()
    return target


def delete(root: Path, rel: str) -> None:
    target = resolve(root, rel)
    if target.resolve() == root.resolve():
        raise FileError("The server folder itself cannot be deleted.", 403)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def rename(root: Path, rel_from: str, rel_to: str) -> Path:
    src = resolve(root, rel_from)
    dst = resolve(root, rel_to, must_exist=False)
    if src.resolve() == root.resolve():
        raise FileError("The server folder itself cannot be renamed.", 403)
    if dst.exists():
        raise FileError("The destination already exists.", 409)
    if not dst.parent.is_dir():
        raise FileError("The destination folder does not exist.", 404)
    src.rename(dst)
    return dst
