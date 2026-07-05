"""Windows-side event reporter.

Every side-effect the script cares about (a post attempt, a stats snapshot, a
ghost check, a heartbeat) is emitted via `emit()`. Events go into an on-disk
outbox (SQLite) so they survive VPS downtime and network blips. A separate
flusher (see cli.reporter_daemon) drains the outbox to the VPS with retries.

Environment variables (read once at import time):
  REPORTER_URL     — base URL of the ingest endpoint (e.g. https://api.example.com)
                     If unset, emit() writes to the outbox anyway — a later
                     configuration is enough to catch up. Set to empty to disable.
  REPORTER_TOKEN   — shared bearer token
  MACHINE_ID       — human-friendly identifier for this box (defaults to
                     socket hostname). Included on machine-scoped events.

The outbox lives at data/outbox.sqlite.
"""
from __future__ import annotations

import json
import os
import platform
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from loguru import logger

from .config import DATA_DIR
from .events import AnyEvent

OUTBOX_DB = DATA_DIR / "outbox.sqlite"

_INIT_LOCK = threading.Lock()
_INITED = False


def machine_id() -> str:
    return os.environ.get("MACHINE_ID") or platform.node().lower()


def _reporter_url() -> str | None:
    url = os.environ.get("REPORTER_URL", "").strip()
    return url or None


def _reporter_token() -> str | None:
    tok = os.environ.get("REPORTER_TOKEN", "").strip()
    return tok or None


# ---------------------------------------------------------------------------
# Outbox schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    event_id      TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_ts    TEXT NOT NULL,
    sent_ts       TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox(created_ts) WHERE sent_ts IS NULL;
"""


def _init() -> None:
    global _INITED
    if _INITED:
        return
    with _INIT_LOCK:
        if _INITED:
            return
        OUTBOX_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(OUTBOX_DB)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _INITED = True


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    _init()
    conn = sqlite3.connect(OUTBOX_DB, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# emit — cheap, safe to call from anywhere
# ---------------------------------------------------------------------------

def emit(event: AnyEvent) -> None:
    """Append an event to the outbox. Never raises to the caller.

    Any exception during outbox write is logged and swallowed — dropping an
    event beats crashing the poster. In practice this only fails on disk-full.
    """
    try:
        payload_json = event.model_dump_json()
        with _connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox
                    (event_id, event_type, payload_json, created_ts)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    payload_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception as e:
        logger.warning(f"reporter.emit failed for {event.event_type}: {e}")


# ---------------------------------------------------------------------------
# Flusher — called by the reporter daemon
# ---------------------------------------------------------------------------

BATCH_SIZE = 100
REQUEST_TIMEOUT = 15.0


class FlushResult:
    __slots__ = ("sent", "failed", "backoff_seconds")

    def __init__(self, sent: int, failed: int, backoff_seconds: float):
        self.sent = sent
        self.failed = failed
        self.backoff_seconds = backoff_seconds


def _fetch_unsent(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_id, event_type, payload_json, attempts
        FROM outbox
        WHERE sent_ts IS NULL
        ORDER BY created_ts
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _mark_sent(conn: sqlite3.Connection, event_ids: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE outbox SET sent_ts = ?, last_error = NULL WHERE event_id = ?",
        [(now, eid) for eid in event_ids],
    )


def _mark_failed(conn: sqlite3.Connection, event_ids: list[str], error: str) -> None:
    conn.executemany(
        "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE event_id = ?",
        [(error[:500], eid) for eid in event_ids],
    )


def flush_once() -> FlushResult:
    """Attempt to send one batch. Returns counts + suggested backoff on failure."""
    url = _reporter_url()
    token = _reporter_token()
    if not url or not token:
        # Not configured — nothing to do, don't retry aggressively.
        return FlushResult(sent=0, failed=0, backoff_seconds=30.0)

    with _connect() as conn:
        rows = _fetch_unsent(conn, BATCH_SIZE)
        if not rows:
            return FlushResult(sent=0, failed=0, backoff_seconds=5.0)

        events_payload = [json.loads(r["payload_json"]) for r in rows]
        event_ids = [r["event_id"] for r in rows]

        try:
            resp = httpx.post(
                f"{url.rstrip('/')}/batch",
                headers={"Authorization": f"Bearer {token}"},
                json={"events": events_payload},
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as e:
            _mark_failed(conn, event_ids, f"network: {e!r}")
            attempts = max(r["attempts"] for r in rows) + 1
            return FlushResult(sent=0, failed=len(rows), backoff_seconds=_backoff(attempts))

        if resp.status_code // 100 == 2:
            _mark_sent(conn, event_ids)
            return FlushResult(sent=len(rows), failed=0, backoff_seconds=0.5)

        # 4xx = permanent for this batch shape — mark attempts and hold off.
        # 5xx = transient, back off and retry.
        body_snippet = resp.text[:300]
        _mark_failed(conn, event_ids, f"http {resp.status_code}: {body_snippet}")
        attempts = max(r["attempts"] for r in rows) + 1
        return FlushResult(sent=0, failed=len(rows), backoff_seconds=_backoff(attempts))


def _backoff(attempts: int) -> float:
    # 1s, 2s, 4s, 8s, ... capped at 60s
    return min(60.0, 2.0 ** min(attempts, 6))


def flush_forever(stop_event: threading.Event | None = None) -> None:
    """Long-running flusher. The daemon calls this on the main thread.

    Sleeps between batches; wakes early when the stop_event is set so we
    can exit cleanly on SIGTERM.
    """
    logger.info("reporter flusher started")
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("reporter flusher stopping (stop_event set)")
            return
        result = flush_once()
        if result.sent:
            logger.info(f"flushed {result.sent} events")
        elif result.failed:
            logger.warning(
                f"flush failed for {result.failed} events, backing off {result.backoff_seconds:.1f}s"
            )
        sleep_for = max(0.5, result.backoff_seconds)
        if stop_event is not None:
            if stop_event.wait(sleep_for):
                logger.info("reporter flusher stopping (stop_event set during sleep)")
                return
        else:
            time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Diagnostics — used by cl status
# ---------------------------------------------------------------------------

def outbox_summary() -> dict:
    """Return counts for `cl status` and a `cl outbox` command."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE sent_ts IS NULL) AS pending,
                COUNT(*) FILTER (WHERE sent_ts IS NOT NULL) AS sent,
                COUNT(*) FILTER (WHERE sent_ts IS NULL AND attempts > 0) AS retrying,
                MAX(attempts) AS max_attempts
            FROM outbox
            """
        ).fetchone()
    return {
        "pending": row["pending"] or 0,
        "sent": row["sent"] or 0,
        "retrying": row["retrying"] or 0,
        "max_attempts": row["max_attempts"] or 0,
        "configured": _reporter_url() is not None and _reporter_token() is not None,
    }


def purge_sent(older_than_days: int = 14) -> int:
    """Delete outbox rows successfully sent more than N days ago. Returns deleted."""
    cutoff = (datetime.now(timezone.utc).timestamp() - older_than_days * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM outbox WHERE sent_ts IS NOT NULL AND sent_ts < ?",
            (cutoff_iso,),
        )
        return cur.rowcount or 0
