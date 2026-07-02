from __future__ import annotations

import random
import shutil
from pathlib import Path

from loguru import logger

from .config import COVERS_DIR, Account

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _account_dir(account: Account) -> Path:
    return COVERS_DIR / account.name


def _unclaimed_dir() -> Path:
    return COVERS_DIR / "unclaimed"


def _used_dir(account: Account) -> Path:
    return _account_dir(account) / "used"


def _list_images(d: Path) -> list[Path]:
    if not d.exists():
        return []
    return [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS]


def is_cover_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(COVERS_DIR.resolve())
        return True
    except ValueError:
        return False


def pick_cover(account: Account, rng: random.Random | None = None) -> Path | None:
    """
    Return a cover image path for this account, or None if no cover is available.

    Order: existing claimed inventory in data/covers/<account>/ first, then a
    random pick from data/covers/unclaimed/ (moved into the account folder as
    the claim). Files in <account>/used/ are ignored.
    """
    rng = rng or random.Random()

    claimed = _list_images(_account_dir(account))
    if claimed:
        return rng.choice(claimed)

    unclaimed = _list_images(_unclaimed_dir())
    if not unclaimed:
        return None

    src = rng.choice(unclaimed)
    dst_dir = _account_dir(account)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    logger.info(f"[{account.name}] claimed cover: {src.name}")
    return dst


def mark_cover_used(cover_path: Path) -> None:
    """Move a cover from data/covers/<account>/ to data/covers/<account>/used/."""
    used = cover_path.parent / "used"
    used.mkdir(parents=True, exist_ok=True)
    dst = used / cover_path.name
    shutil.move(str(cover_path), str(dst))
    logger.debug(f"cover consumed: {cover_path.name} -> {dst}")
