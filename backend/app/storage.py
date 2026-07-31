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
    ok = False
    try:
        open_path(rel).unlink()
        ok = True
    except (FileNotFoundError, ValueError):
        pass
    # Derived files are worthless without their source, and leaving them behind
    # would keep bytes on the volume that nothing can ever serve again.
    for width in THUMB_WIDTHS:
        try:
            thumb_path(rel, width).unlink()
        except (FileNotFoundError, ValueError):
            pass
    return ok


# ---------------------------------------------------------------------------
# Thumbnails
#
# `/raw` serves the stored file, which is what Craigslist needs and what the
# desktop downloads. Putting that same file behind a 5-column grid meant a
# 120-image page pulled ~94MB of full-resolution JPEG to render tiles a couple
# of hundred pixels wide. These are derived, cached next to the original, and
# rebuilt on demand — deleting the whole `thumbs/` tree is always safe.
# ---------------------------------------------------------------------------

# Two sizes, both derived: 480 for grid tiles, 1024 for constrained previews
# (a modal capped at 420px tall is ~560px wide, so 1024 covers it at 2x).
# Add a width here rather than falling back to `/raw` when a new size appears —
# no view should pull a full-resolution original to render it smaller.
THUMB_WIDTHS = (480, 1024)
DEFAULT_THUMB_WIDTH = 480
THUMB_QUALITY = 78


def thumb_path(rel: str, width: int) -> Path:
    """Where the derived thumbnail for a stored blob lives."""
    base = root().resolve()
    stem = Path(rel).stem
    p = (base / "thumbs" / f"{width}" / rel.split("/")[0] / f"{stem}.jpg").resolve()
    if not p.is_relative_to(base):
        raise ValueError(f"path escapes the storage root: {rel!r}")
    return p


def get_or_make_thumb(rel: str, width: int = DEFAULT_THUMB_WIDTH) -> Path | None:
    """Return a cached thumbnail, rendering it once if it does not exist.

    Returns None when the source bytes are missing, so the caller can answer
    410 rather than 500. Any Pillow failure also returns None — a thumbnail is
    an optimisation, and falling back to the original beats a broken grid.

    Never upscales: a source narrower than `width` is returned as-is by Pillow's
    `thumbnail`, so a small upload does not become a larger file than it was.
    """
    if width not in THUMB_WIDTHS:
        raise ValueError(f"width must be one of {THUMB_WIDTHS}")
    src = open_path(rel)
    if not src.exists():
        return None
    dest = thumb_path(rel, width)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    from PIL import Image

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width * 4), Image.LANCZOS)
            tmp = dest.with_suffix(".part")
            im.save(tmp, format="JPEG", quality=THUMB_QUALITY, optimize=True)
            tmp.replace(dest)
    except Exception:
        return None
    return dest


def usage() -> dict:
    """Total bytes and file count, for the dashboard.

    Thumbnails are excluded: they are a rebuildable cache, and counting them
    would make the stack look like it is using storage it does not need.
    """
    total = count = 0
    thumbs = (root() / "thumbs").resolve()
    for p in root().rglob("*"):
        if not p.is_file() or p.name.endswith(".part"):
            continue
        if p.resolve().is_relative_to(thumbs):
            continue
        total += p.stat().st_size
        count += 1
    return {"files": count, "bytes": total}
