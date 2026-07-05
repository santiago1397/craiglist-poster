"""Event → table dispatch. Every handler is idempotent by event_id.

Public entry point: `ingest_events(conn, events)`. Called by the /events and
/events/batch routers. The router owns the transaction — this module only
issues SQL against the provided connection.
"""
from __future__ import annotations

import json

import psycopg
from loguru import logger

from ..schemas.events import (
    AccountState,
    AnyEvent,
    GhostCheck,
    PhotoInventory,
    PostAttempt,
    SchedulerConfig,
    SnapshotTaken,
)


def ingest_events(conn: psycopg.Connection, events: list[AnyEvent]) -> dict:
    """Insert a batch of events. Duplicates (same event_id) are ignored.

    Returns a small summary dict for the response.
    """
    counts = {"received": len(events), "inserted": 0, "duplicate": 0, "by_type": {}}

    for ev in events:
        et = ev.event_type
        counts["by_type"][et] = counts["by_type"].get(et, 0) + 1

        if isinstance(ev, PostAttempt):
            inserted = _insert_post_attempt(conn, ev)
        elif isinstance(ev, SnapshotTaken):
            inserted = _insert_snapshot(conn, ev)
        elif isinstance(ev, GhostCheck):
            inserted = _insert_ghost_check(conn, ev)
        elif isinstance(ev, PhotoInventory):
            inserted = _insert_photo_inventory(conn, ev)
        elif isinstance(ev, AccountState):
            inserted = _insert_account_state(conn, ev)
        elif isinstance(ev, SchedulerConfig):
            inserted = _insert_scheduler_config(conn, ev)
        else:  # pragma: no cover — pydantic union guarantees exhaustiveness
            logger.warning(f"Unknown event_type: {et}")
            inserted = False

        if inserted:
            counts["inserted"] += 1
        else:
            counts["duplicate"] += 1

    return counts


# ---------------------------------------------------------------------------
# posts dimension — used by both post_attempt(outcome=posted) and snapshot
# ---------------------------------------------------------------------------

def _upsert_post(
    conn: psycopg.Connection,
    *,
    post_id: str,
    account: str,
    title: str | None,
    url: str | None,
    posted_ts,
) -> None:
    conn.execute(
        """
        INSERT INTO posts (post_id, account, title, url, posted_ts, source, updated_at)
        VALUES (%s, %s, %s, %s, %s, 'event_ingest', NOW())
        ON CONFLICT (post_id) DO UPDATE SET
            title = COALESCE(EXCLUDED.title, posts.title),
            url = COALESCE(EXCLUDED.url, posts.url),
            posted_ts = COALESCE(posts.posted_ts, EXCLUDED.posted_ts),
            updated_at = NOW()
        """,
        (post_id, account, title, url, posted_ts),
    )


# ---------------------------------------------------------------------------
# Per-event inserters. Return True if newly inserted, False on conflict.
# ---------------------------------------------------------------------------

def _did_insert(cur: psycopg.Cursor) -> bool:
    return cur.rowcount > 0


def _insert_post_attempt(conn: psycopg.Connection, ev: PostAttempt) -> bool:
    if ev.outcome == "posted" and ev.post_id:
        _upsert_post(
            conn,
            post_id=ev.post_id,
            account=ev.account,
            title=ev.ad_title,
            url=ev.post_url,
            posted_ts=ev.ts,
        )
    cur = conn.execute(
        """
        INSERT INTO post_attempts (
            event_id, ts, machine, account, outcome, duration_seconds,
            post_id, post_url, ad_title, photos_attached, cover_photo,
            error_type, error_message
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.account, ev.outcome, ev.duration_seconds,
            ev.post_id, ev.post_url, ev.ad_title, json.dumps(ev.photos_attached), ev.cover_photo,
            ev.error_type, ev.error_message,
        ),
    )
    return _did_insert(cur)


def _insert_snapshot(conn: psycopg.Connection, ev: SnapshotTaken) -> bool:
    # Ensure the post row exists first (satisfies snapshots FK).
    _upsert_post(
        conn,
        post_id=ev.post_id,
        account=ev.account,
        title=ev.title,
        url=ev.url,
        posted_ts=ev.posted_ts,
    )
    cur = conn.execute(
        """
        INSERT INTO snapshots (
            post_id, snapshot_date, snapshot_ts_utc, status,
            impressions, views, shares, favorites,
            area, category, expires_in_days, autorepost, freshness_note
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (post_id, snapshot_date) DO UPDATE SET
            snapshot_ts_utc = EXCLUDED.snapshot_ts_utc,
            status = EXCLUDED.status,
            impressions = EXCLUDED.impressions,
            views = EXCLUDED.views,
            shares = EXCLUDED.shares,
            favorites = EXCLUDED.favorites,
            area = COALESCE(EXCLUDED.area, snapshots.area),
            category = COALESCE(EXCLUDED.category, snapshots.category),
            expires_in_days = EXCLUDED.expires_in_days,
            autorepost = COALESCE(EXCLUDED.autorepost, snapshots.autorepost),
            freshness_note = COALESCE(EXCLUDED.freshness_note, snapshots.freshness_note)
        """,
        (
            ev.post_id, ev.snapshot_date, ev.ts, ev.status,
            ev.impressions, ev.views, ev.shares, ev.favorites,
            ev.area, ev.category, ev.expires_in_days, ev.autorepost, ev.freshness_note,
        ),
    )
    # snapshots table doesn't have event_id — the (post_id, snapshot_date) pair
    # is the natural idempotency key. Report as "inserted" if any row action
    # occurred (INSERT or UPDATE both return 1 from ON CONFLICT DO UPDATE).
    return _did_insert(cur)


def _insert_ghost_check(conn: psycopg.Connection, ev: GhostCheck) -> bool:
    cur = conn.execute(
        """
        INSERT INTO ghost_checks (event_id, ts, post_id, account, ghosted)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (ev.event_id, ev.ts, ev.post_id, ev.account, ev.ghosted),
    )
    return _did_insert(cur)


def _insert_photo_inventory(conn: psycopg.Connection, ev: PhotoInventory) -> bool:
    cur = conn.execute(
        """
        INSERT INTO photo_inventory_snapshots (
            event_id, ts, account,
            photos_total, photos_never_used, photos_eligible,
            covers_total, covers_never_used, covers_eligible
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.account,
            ev.photos_total, ev.photos_never_used, ev.photos_eligible,
            ev.covers_total, ev.covers_never_used, ev.covers_eligible,
        ),
    )
    return _did_insert(cur)


def _insert_account_state(conn: psycopg.Connection, ev: AccountState) -> bool:
    health_json = json.dumps(ev.stats_sync_health.model_dump(mode="json")) if ev.stats_sync_health else None
    cur = conn.execute(
        """
        INSERT INTO account_states (
            event_id, ts, machine, account, eligible_now, next_eligible_at,
            block_reasons, posts_last_24h_total, posts_last_7d_this_account,
            last_post_at, last_post_url, stats_sync_health
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.account, ev.eligible_now, ev.next_eligible_at,
            json.dumps(ev.block_reasons), ev.posts_last_24h_total, ev.posts_last_7d_this_account,
            ev.last_post_at, ev.last_post_url, health_json,
        ),
    )
    return _did_insert(cur)


def _insert_scheduler_config(conn: psycopg.Connection, ev: SchedulerConfig) -> bool:
    cur = conn.execute(
        """
        INSERT INTO scheduler_configs (
            event_id, ts, machine, posting_cadence, stats_sync_cadence,
            min_hours_between_posts_same_account, max_posts_per_day_total,
            max_posts_per_account_per_week, post_window_start_hour,
            post_window_end_hour, post_weekdays_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.posting_cadence, ev.stats_sync_cadence,
            ev.min_hours_between_posts_same_account, ev.max_posts_per_day_total,
            ev.max_posts_per_account_per_week, ev.post_window_start_hour,
            ev.post_window_end_hour, ev.post_weekdays_only,
        ),
    )
    return _did_insert(cur)
