"""Machine-wide browser lease (DESIGN_EDITS.md decision 27).

`poster.launch_account` opens Chrome with `launch_persistent_context`, which
takes an exclusive OS lock on `profiles/<account>/`. Two flows touching one
account's profile at the same time is not a race we can lose gracefully — Chrome
either refuses to start or corrupts the profile, and this project's README
already warns that OneDrive makes profile lock files fragile.

Until now `post` (9/1/5), `stats-sync` (6am) and `init-account` (manual) were
kept apart purely by non-overlapping Task Scheduler times. Nothing enforced it.
Adding an opportunistic edit worker makes a collision inevitable, so every flow
that drives a browser now goes through here.

Priority is expressed by how callers acquire, not by anything in this module:

    post        acquire(blocking=True)   — a post must never be skipped
    stats-sync  acquire(blocking=True)
    edit        acquire(blocking=False)  — skips and retries on the next poll

Liveness is heartbeat-based rather than pid-based. A holder rewrites its
timestamp on a background thread; a lease whose heartbeat has gone quiet for
`STALE_AFTER` is reclaimed. That survives a hard kill, a reboot, and a pid
being recycled — none of which a bare pid check handles reliably on Windows.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from loguru import logger

from .config import DATA_DIR

LOCK_PATH = DATA_DIR / "browser.lock"

HEARTBEAT_SECONDS = 30.0
# Generous: a slow post with five photo uploads can sit on one step for minutes.
# Reclaiming a live lease is far worse than waiting a little longer for a dead one.
STALE_AFTER = 180.0


class LeaseBusy(RuntimeError):
    """Another flow holds the browser lease. Non-blocking callers get this."""


def _now() -> float:
    return time.time()


def _read_lock() -> dict | None:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        # Truncated or half-written by a crash mid-write. Treat as absent; the
        # heartbeat check below would reclaim it anyway.
        return None


def _is_stale(info: dict) -> bool:
    beat = info.get("heartbeat_at")
    if not isinstance(beat, (int, float)):
        return True
    return (_now() - beat) > STALE_AFTER


def _write_lock_atomic(info: dict) -> bool:
    """Create the lock file, failing if it already exists.

    O_CREAT|O_EXCL is atomic on both NTFS and POSIX, which is what makes this a
    real mutex rather than a check-then-write race.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, json.dumps(info).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def current_holder() -> dict | None:
    """Who holds the lease right now, or None. Used by `cl status`."""
    info = _read_lock()
    if info is None or _is_stale(info):
        return None
    return info


def _try_acquire(flow: str, account: str | None) -> dict | None:
    info = {
        "flow": flow,
        "account": account,
        "pid": os.getpid(),
        "host": platform.node(),
        "acquired_at": _now(),
        "heartbeat_at": _now(),
        "acquired_at_iso": datetime.now(timezone.utc).isoformat(),
    }
    if _write_lock_atomic(info):
        return info

    existing = _read_lock()
    if existing is not None and _is_stale(existing):
        age = _now() - (existing.get("heartbeat_at") or 0)
        logger.warning(
            f"reclaiming stale browser lease from flow={existing.get('flow')!r} "
            f"pid={existing.get('pid')} (heartbeat {age:.0f}s ago)"
        )
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        if _write_lock_atomic(info):
            return info
    return None


@contextmanager
def acquire(
    flow: str,
    *,
    account: str | None = None,
    blocking: bool = True,
    timeout: float = 900.0,
    poll: float = 2.0,
) -> Iterator[dict]:
    """Hold the browser lease for the duration of the block.

    Raises `LeaseBusy` immediately when `blocking=False` and someone else holds
    it, or after `timeout` seconds when blocking.
    """
    deadline = _now() + timeout
    info = _try_acquire(flow, account)
    while info is None:
        holder = _read_lock() or {}
        if not blocking:
            raise LeaseBusy(
                f"browser lease held by flow={holder.get('flow')!r} "
                f"account={holder.get('account')!r}"
            )
        if _now() >= deadline:
            raise LeaseBusy(
                f"timed out after {timeout:.0f}s waiting for the browser lease "
                f"(held by flow={holder.get('flow')!r})"
            )
        time.sleep(poll)
        info = _try_acquire(flow, account)

    logger.debug(f"browser lease acquired by flow={flow!r} account={account!r}")
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                info["heartbeat_at"] = _now()
                LOCK_PATH.write_text(json.dumps(info), encoding="utf-8")
            except Exception as e:  # pragma: no cover — disk hiccup, not fatal
                logger.warning(f"browser lease heartbeat failed: {e}")

    beat_thread = threading.Thread(target=_beat, name="cl-lease-heartbeat", daemon=True)
    beat_thread.start()
    try:
        yield info
    finally:
        stop.set()
        try:
            # Only remove the lock if it is still ours — if ours went stale and
            # someone else reclaimed it, deleting theirs would be worse than
            # leaving a file behind.
            current = _read_lock()
            if current and current.get("pid") == os.getpid() and current.get("flow") == flow:
                LOCK_PATH.unlink()
                logger.debug(f"browser lease released by flow={flow!r}")
            elif current is not None:
                logger.warning(
                    f"not releasing browser lease: it now belongs to "
                    f"flow={current.get('flow')!r} pid={current.get('pid')}"
                )
        except FileNotFoundError:
            pass
        except Exception as e:  # pragma: no cover
            logger.warning(f"browser lease release failed: {e}")
