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
    FlowError,
    GhostCheck,
    PhotoInventory,
    PostAttempt,
    PostContent,
    PostEditAttempt,
    SchedulerConfig,
    SnapshotTaken,
)
from . import edits as edits_svc
from . import queue as queue_svc


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
        elif isinstance(ev, FlowError):
            inserted = _insert_flow_error(conn, ev)
        elif isinstance(ev, PostContent):
            inserted = _insert_post_content(conn, ev)
        elif isinstance(ev, PostEditAttempt):
            inserted = _insert_post_edit_attempt(conn, ev)
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
    if ev.outcome == "posted":
        # Every guardrail — the 24h cap, the per-account cooldown, the weekly
        # cap — counts rows in `posts`. This used to require `ev.post_id`, so a
        # post that published but could not be identified wrote no history at
        # all and the account looked idle. That happened: a run captured a
        # foreign /d/ link, yielded no post id, and left craigs1 free to post
        # again immediately despite having just published.
        #
        # An ad on the internet we cannot name is still an ad on the internet.
        # It gets a synthetic id derived from the event, which keeps the upsert
        # idempotent across replays, and is filed as a problem so it is not
        # only discoverable by reading this table.
        post_id = ev.post_id or f"unidentified:{ev.event_id}"
        _upsert_post(
            conn,
            post_id=post_id,
            account=ev.account,
            title=ev.ad_title,
            url=ev.post_url,
            posted_ts=ev.ts,
        )
        if not ev.post_id:
            _record_unidentified_post(conn, ev, post_id)
    cur = conn.execute(
        """
        INSERT INTO post_attempts (
            event_id, ts, machine, account, outcome, duration_seconds,
            post_id, post_url, ad_title, photos_attached, cover_photo,
            error_type, error_message, draft_id, failed_step,
            warnings, photos_confirmed, artifact_ids
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.account, ev.outcome, ev.duration_seconds,
            ev.post_id, ev.post_url, ev.ad_title, json.dumps(ev.photos_attached), ev.cover_photo,
            ev.error_type, ev.error_message, ev.draft_id, ev.failed_step,
            json.dumps(ev.warnings), ev.photos_confirmed, json.dumps(ev.artifact_ids),
        ),
    )
    inserted = _did_insert(cur)

    # A published-but-degraded post keeps outcome='posted' so the cooldown maths
    # still counts it, which means nothing in the posting tables would ever draw
    # attention to it. Mirror it into flow_errors so it lands in the same tray as
    # every other problem instead of needing its own place to be looked for.
    if inserted and ev.warnings:
        _record_degradation(conn, ev)

    # Advance the draft's state machine — but only on first receipt. A replayed
    # event must not park a draft that has since been posted or re-queued.
    if inserted and ev.draft_id is not None:
        _route_draft(conn, ev)
    return inserted


def _record_unidentified_post(
    conn: psycopg.Connection, ev: PostAttempt, synthetic_id: str
) -> None:
    """File a published-but-unidentifiable post as a problem.

    It counts for the cooldowns now, which is the safety-critical part, but a
    post with no id cannot be ghost-checked, cannot collect stats, and cannot
    be edited from the dashboard. That is worth a human knowing about, and the
    fix is usually to find the URL by hand and correct the row.
    """
    conn.execute(
        """
        INSERT INTO flow_errors (
            event_id, ts, machine, flow, step, account,
            error_type, error_message, context
        )
        VALUES (%s, %s, %s, 'post', 'extract_url', %s,
                'post_unidentified', %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            f"{ev.event_id}:unidentified",
            ev.ts,
            ev.machine,
            ev.account,
            (
                f"{ev.account} published a post but no posting id could be read "
                f"from the confirmation page. It is counted against the "
                f"cooldowns under a placeholder id, but it cannot be "
                f"ghost-checked, tracked for stats or edited until someone "
                f"finds its real URL in the account and corrects it."
            ),
            json.dumps({
                "draft_id": ev.draft_id,
                "synthetic_post_id": synthetic_id,
                "post_url": ev.post_url,
                "ad_title": ev.ad_title,
            }),
        ),
    )


def _record_degradation(conn: psycopg.Connection, ev: PostAttempt) -> None:
    """File a degraded post as a flow_error so one tray shows everything wrong.

    Keyed off the attempt's event_id with a suffix, so it inherits the same
    idempotency: a replayed batch cannot file the same degradation twice.
    """
    conn.execute(
        """
        INSERT INTO flow_errors (
            event_id, ts, machine, flow, step, account,
            error_type, error_message, context
        )
        VALUES (%s, %s, %s, 'post', %s, %s, 'degraded_post', %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            f"{ev.event_id}:degraded",
            ev.ts,
            ev.machine,
            ev.failed_step,
            ev.account,
            f"Published with {len(ev.warnings)} degradation(s): " + "; ".join(ev.warnings),
            json.dumps({
                "post_id": ev.post_id,
                "post_url": ev.post_url,
                "draft_id": ev.draft_id,
                "outcome": ev.outcome,
                "warnings": ev.warnings,
                "photos_attached": len(ev.photos_attached),
                "photos_confirmed": ev.photos_confirmed,
                "artifact_ids": ev.artifact_ids,
            }),
        ),
    )


def _route_draft(conn: psycopg.Connection, ev: PostAttempt) -> None:
    """Move the claimed draft on, per decision 16."""
    if ev.outcome == "posted":
        queue_svc.mark_posted(
            conn, draft_id=ev.draft_id, post_id=ev.post_id, posted_at=ev.ts
        )
        _clear_post_request(conn, ev.draft_id)
    elif ev.outcome in ("failed_login", "failed_form", "failed_other"):
        new_status = queue_svc.release_or_park(
            conn,
            draft_id=ev.draft_id,
            failed_step=ev.failed_step,
            failed_message=ev.error_message,
            # How many thumbnails Craigslist actually rendered before the run
            # died. Uploads go in slot order, so this identifies exactly which
            # images are burned.
            photos_confirmed=ev.photos_confirmed,
        )
        # Clear the request on failure too, and deliberately. A pre-upload
        # failure sends the draft back to 'queued'; if the flag survived, the
        # daemon would see it again on its next 15s beat and retry unattended,
        # forever. One press of the button is one attempt — the operator reads
        # the failure and decides whether to press it again.
        _clear_post_request(conn, ev.draft_id, error=ev.error_message)
        logger.info(
            f"draft {ev.draft_id} failed at step {ev.failed_step!r} -> {new_status}"
        )
    # dry_run and the skipped_* outcomes never hold a claim, so nothing to do.


def _clear_post_request(
    conn: psycopg.Connection, draft_id: int, *, error: str | None = None
) -> None:
    """End a "Post now" request now that its attempt has been accounted for.

    Runs on the durable path rather than at claim time, so a VPS outage delays
    the clear instead of losing it — the same reason `record_content` clears
    `hydrate_requested_at` here rather than over the queue API.
    """
    conn.execute(
        """
        UPDATE drafts
        SET post_requested_at = NULL,
            post_requested_by = NULL,
            post_request_error = %s,
            updated_at = NOW()
        WHERE id = %s AND post_requested_at IS NOT NULL
        """,
        ((error or "")[:1000] or None, draft_id),
    )


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


def _insert_flow_error(conn: psycopg.Connection, ev: FlowError) -> bool:
    cur = conn.execute(
        """
        INSERT INTO flow_errors (
            event_id, ts, machine, flow, step, account,
            error_type, error_message, context
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.flow, ev.step, ev.account,
            ev.error_type, ev.error_message, json.dumps(ev.context),
        ),
    )
    return _did_insert(cur)


def _insert_post_content(conn: psycopg.Connection, ev: PostContent) -> bool:
    """Store a hydration result (decision 23).

    There is no event-log table for this one: hydration is a full overwrite of
    the post's content columns, so the post row *is* the state. `record_content`
    guards on the timestamp, which is what makes a replayed or out-of-order
    delivery safe — an older read must never regress a newer one.
    """
    return edits_svc.record_content(conn, ev)


def _insert_post_edit_attempt(conn: psycopg.Connection, ev: PostEditAttempt) -> bool:
    cur = conn.execute(
        """
        INSERT INTO post_edit_attempts (
            event_id, ts, machine, account, post_id, outcome, duration_seconds,
            desired_rev, applied_rev, steps, failed_step, error_type,
            error_message, images_live_count, images_desired_count, artifact_ids
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.account, ev.post_id, ev.outcome,
            ev.duration_seconds, ev.desired_rev, ev.applied_rev,
            json.dumps([s.model_dump(mode="json") for s in ev.steps]),
            ev.failed_step, ev.error_type, ev.error_message,
            ev.images_live_count, ev.images_desired_count,
            json.dumps(ev.artifact_ids),
        ),
    )
    inserted = _did_insert(cur)

    # Advance the desired state only on first receipt. A replayed failure must
    # not park an edit that has since been applied or re-queued by hand.
    if inserted:
        edits_svc.apply_attempt(conn, ev)
    return inserted


def _insert_scheduler_config(conn: psycopg.Connection, ev: SchedulerConfig) -> bool:
    cur = conn.execute(
        """
        INSERT INTO scheduler_configs (
            event_id, ts, machine, posting_cadence, stats_sync_cadence,
            min_hours_between_posts_same_account, max_posts_per_day_total,
            max_posts_per_account_per_week, post_window_start_hour,
            post_window_end_hour, post_weekdays_only, code_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            ev.event_id, ev.ts, ev.machine, ev.posting_cadence, ev.stats_sync_cadence,
            ev.min_hours_between_posts_same_account, ev.max_posts_per_day_total,
            ev.max_posts_per_account_per_week, ev.post_window_start_hour,
            ev.post_window_end_hour, ev.post_weekdays_only, ev.code_version,
        ),
    )
    return _did_insert(cur)
