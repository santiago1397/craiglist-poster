"""Content-addressed blob storage on a mounted volume.

Deliberately a thin interface over the filesystem (decision 12). Everything the
rest of the app does goes through `put_bytes` / `open_path` / `delete`, so
swapping to S3 or R2 later is one file rather than a refactor.

Paths shard by the first two hex characters of the digest — a flat directory
with thousands of files is slow to list and unpleasant to work with over SSH.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import get_settings

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def root() -> Path:
    p = Path(get_settings().images_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_path(digest: str, mime: str) -> str:
    return f"{digest[:2]}/{digest}{_EXT_BY_MIME.get(mime, '.bin')}"


def put_bytes(data: bytes, mime: str = "image/jpeg") -> tuple[str, str, int]:
    """Store bytes. Returns (sha256, relative path, size).

    Idempotent by content: writing the same bytes twice is a no-op, which is
    what makes the images table's UNIQUE(sha256) safe to rely on.
    """
    digest = sha256_of(data)
    rel = relative_path(digest, mime)
    dest = root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        # Write to a temp name then rename, so a crash mid-write cannot leave a
        # truncated file sitting at the address of valid content.
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return digest, rel, len(data)


def open_path(rel: str) -> Path:
    """Absolute path for a stored blob, guarded against traversal."""
    base = root().resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise ValueError(f"path escapes the storage root: {rel!r}")
    return p


def delete(rel: str) -> bool:
    try:
        open_path(rel).unlink()
        return True
    except (FileNotFoundError, ValueError):
        return False


def usage() -> dict:
    """Total bytes and file count, for the dashboard."""
    total = count = 0
    for p in root().rglob("*"):
        if p.is_file() and not p.name.endswith(".part"):
            total += p.stat().st_size
            count += 1
    return {"files": count, "bytes": total}
