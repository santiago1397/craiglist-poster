"""Direct sqlite → Postgres backfill (Q15 option C).

Idempotent by natural keys plus UUIDv5-derived event_ids so a re-run doesn't
create duplicate post_attempts / ghost_checks.

Read sources:
  - data/stats.sqlite  → posts + snapshots
  - logs/state.json    → post_attempts (outcome=posted) + ghost_checks

Skipped by design: photo_inventory (can't reconstruct historical snapshots
from photo_usage.json — we only have last-used, not per-day inventory).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

from loguru import logger

from .config import STATE_FILE
from .stats import STATS_DB, extract_post_id

# Fixed namespace for deterministic backfill UUIDs — never change this.
_BACKFILL_NS = UUID("6d1b4a83-2b8f-4e3e-8a5e-70c7ad3d7a20")


def _event_id(kind: str, *parts: str) -> str:
    return str(uuid5(_BACKFILL_NS, kind + "|" + "|".join(parts)))


def _iso_utc(v) -> str | None:
    """Coerce whatever posted_ts/at might be into an ISO UTC string."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def run(*, dsn: str, since: str | None = None) -> None:
    import psycopg

    cutoff_date: date | None = None
    if since:
        cutoff_date = date.fromisoformat(since)

    posts_seen = 0
    snapshots_seen = 0
    attempts_seen = 0
    ghosts_seen = 0

    with psycopg.connect(dsn) as pg:
        with pg.transaction():
            # ---------------- posts + snapshots from stats.sqlite ----------------
            if STATS_DB.exists():
                sqlite = sqlite3.connect(str(STATS_DB))
                sqlite.row_factory = sqlite3.Row
                try:
                    for r in sqlite.execute(
                        "SELECT post_id, account, title, url, posted_ts, source FROM posts"
                    ):
                        posted_iso = _iso_utc(r["posted_ts"])
                        if cutoff_date and posted_iso:
                            try:
                                if datetime.fromisoformat(posted_iso).date() < cutoff_date:
                                    continue
                            except Exception:
                                pass
                        pg.execute(
                            """
                            INSERT INTO posts (post_id, account, title, url, posted_ts, source)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (post_id) DO UPDATE SET
                                title = COALESCE(EXCLUDED.title, posts.title),
                                url = COALESCE(EXCLUDED.url, posts.url),
                                posted_ts = COALESCE(posts.posted_ts, EXCLUDED.posted_ts),
                                updated_at = NOW()
                            """,
                            (r["post_id"], r["account"], r["title"], r["url"], posted_iso, r["source"] or "sqlite_backfill"),
                        )
                        posts_seen += 1

                    for r in sqlite.execute(
                        """
                        SELECT post_id, snapshot_date, snapshot_ts_utc, status,
                               impressions, views, shares, favorites,
                               area, category, expires_in_days, autorepost, freshness_note
                        FROM snapshots
                        """
                    ):
                        if cutoff_date and r["snapshot_date"] and date.fromisoformat(r["snapshot_date"]) < cutoff_date:
                            continue
                        pg.execute(
                            """
                            INSERT INTO snapshots (
                                post_id, snapshot_date, snapshot_ts_utc, status,
                                impressions, views, shares, favorites,
                                area, category, expires_in_days, autorepost, freshness_note
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (post_id, snapshot_date) DO NOTHING
                            """,
                            (
                                r["post_id"], r["snapshot_date"], _iso_utc(r["snapshot_ts_utc"]), r["status"],
                                r["impressions"], r["views"], r["shares"], r["favorites"],
                                r["area"], r["category"], r["expires_in_days"], r["autorepost"], r["freshness_note"],
                            ),
                        )
                        snapshots_seen += 1
                finally:
                    sqlite.close()
            else:
                logger.warning(f"stats.sqlite not found at {STATS_DB} — skipping posts+snapshots")

            # ---------------- post_attempts + ghost_checks from state.json ----------------
            if STATE_FILE.exists():
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                for p in state.get("posts", []):
                    posted_iso = _iso_utc(p.get("at"))
                    if not posted_iso:
                        continue
                    if cutoff_date and datetime.fromisoformat(posted_iso).date() < cutoff_date:
                        continue

                    account = p.get("account") or "(unknown)"
                    post_url = p.get("url")
                    post_id = extract_post_id(post_url or "")

                    # Ensure the post row exists (backfill may see state.json entries
                    # whose post_id never made it into stats.sqlite)
                    if post_id:
                        pg.execute(
                            """
                            INSERT INTO posts (post_id, account, title, url, posted_ts, source)
                            VALUES (%s, %s, %s, %s, %s, 'state_json_backfill')
                            ON CONFLICT (post_id) DO NOTHING
                            """,
                            (post_id, account, p.get("title"), post_url, posted_iso),
                        )

                    attempt_id = _event_id("post_attempt", account, posted_iso)
                    pg.execute(
                        """
                        INSERT INTO post_attempts (
                            event_id, ts, machine, account, outcome,
                            post_id, post_url, ad_title, photos_attached
                        )
                        VALUES (%s, %s, %s, %s, 'posted', %s, %s, %s, '[]'::jsonb)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (
                            attempt_id, posted_iso, "backfill",
                            account, post_id, post_url, p.get("title"),
                        ),
                    )
                    attempts_seen += 1

                    if p.get("ghosted") is not None and post_id:
                        ghost_id = _event_id("ghost_check", account, posted_iso)
                        pg.execute(
                            """
                            INSERT INTO ghost_checks (event_id, ts, post_id, account, ghosted)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (event_id) DO NOTHING
                            """,
                            (ghost_id, posted_iso, post_id, account, bool(p["ghosted"])),
                        )
                        ghosts_seen += 1
            else:
                logger.warning(f"state.json not found at {STATE_FILE} — skipping attempts+ghosts")

    logger.info(
        f"backfill complete: posts={posts_seen} snapshots={snapshots_seen} "
        f"attempts={attempts_seen} ghosts={ghosts_seen}"
    )
