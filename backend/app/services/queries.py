"""Read queries powering the dashboard and posts views.

Design decisions baked in here:
- "latest per X" uses DISTINCT ON (Postgres-native; fast with the ts DESC index).
- Post ranking:
    - Latest cumulative counter is `impressions`/`views` from the newest snapshot.
    - "days active" = MAX(snapshot_date WHERE status='Active') - posted_ts::date,
      floored to 1. Falls back to (CURRENT_DATE - posted_ts::date) when we've
      never seen an Active snapshot yet.
    - Posts <2 days old are excluded from rate rankings (denominator too small).
    - Default time window: last 90 days on posted_ts. `since='all'` disables it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg


_SORT_COLUMNS = {
    "posted_ts": "posted_ts",
    "views": "views",
    "impressions": "impressions",
    "views_per_day": "views_per_day",
    "impressions_per_day": "impressions_per_day",
}

MIN_AGE_DAYS_FOR_RATE = 2
DEFAULT_WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard_accounts(conn: psycopg.Connection) -> list[dict]:
    """Per-account cards for the dashboard.

    Combines: latest account_state (eligibility), live image-stack depth,
    most-recent successful post_attempt (last post link + title), most-recent
    attempt of any outcome (last-attempt indicator including failures).

    Image depth is counted here against the `images` table, not read from
    `photo_inventory_snapshots`. That table is fed by `cl photo-inventory`
    scanning `data/photos/` and `data/covers/` on the Windows box — folders the
    queue stopped reading when images moved server-side. It kept refreshing
    daily, so the dashboard reported 64 usable photos while the stack that
    actually feeds posts held 5. A stale number that looks live and reassuring
    is worse than no number.

    "Available to" is deliberate wording: an unclaimed image counts for every
    account, so these do not sum to a stack total.
    """
    rows = conn.execute(
        """
        WITH latest_state AS (
            SELECT DISTINCT ON (account)
                account, machine, ts, eligible_now, next_eligible_at, block_reasons,
                posts_last_24h_total, posts_last_7d_this_account,
                last_post_at, last_post_url, stats_sync_health
            FROM account_states
            ORDER BY account, ts DESC
        ),
        last_success AS (
            SELECT DISTINCT ON (account)
                account, ts AS last_success_ts, post_url AS last_success_url,
                ad_title AS last_success_title, post_id AS last_success_post_id
            FROM post_attempts
            WHERE outcome = 'posted'
            ORDER BY account, ts DESC
        ),
        last_attempt AS (
            SELECT DISTINCT ON (account)
                account, ts AS last_attempt_ts, outcome AS last_attempt_outcome,
                error_type AS last_attempt_error_type,
                error_message AS last_attempt_error_message
            FROM post_attempts
            ORDER BY account, ts DESC
        ),
        accounts AS (
            SELECT account FROM latest_state
            UNION SELECT account FROM last_success
            UNION SELECT account FROM last_attempt WHERE account != '(none)'
        ),
        -- Reserved by a live draft or a live posting's desired set. Mirrors
        -- images._RESERVED_SQL; kept inline so this stays one round trip.
        reserved AS (
            SELECT DISTINCT image_id FROM (
                SELECT di.image_id
                  FROM draft_images di JOIN drafts d ON d.id = di.draft_id
                 WHERE d.status IN ('queued', 'claimed', 'needs_attention')
                UNION ALL
                SELECT image_id FROM post_desired_images
            ) held
        ),
        stack AS (
            SELECT
                a.account,
                COUNT(*) FILTER (
                    WHERE i.kind = 'photo' AND r.image_id IS NULL
                      AND (i.used_at IS NULL OR i.used_at < NOW() - INTERVAL '30 days')
                ) AS photos_available,
                COUNT(*) FILTER (
                    WHERE i.kind = 'cover' AND r.image_id IS NULL
                      AND (i.used_at IS NULL OR i.used_at < NOW() - INTERVAL '30 days')
                ) AS covers_available,
                COUNT(*) FILTER (WHERE r.image_id IS NOT NULL) AS images_assigned,
                COUNT(*) FILTER (WHERE i.used_at IS NOT NULL) AS images_published
            FROM accounts a
            LEFT JOIN images i
                   ON i.status = 'approved'
                  AND (i.owner_account IS NULL OR i.owner_account = a.account)
            LEFT JOIN reserved r ON r.image_id = i.id
            GROUP BY a.account
        ),
        queue AS (
            SELECT account, COUNT(*) AS queued_drafts
            FROM drafts WHERE status = 'queued' GROUP BY account
        )
        SELECT
            a.account,
            ls.eligible_now, ls.next_eligible_at, ls.block_reasons,
            ls.posts_last_24h_total, ls.posts_last_7d_this_account,
            ls.stats_sync_health, ls.ts AS state_ts,
            COALESCE(st.photos_available, 0)  AS photos_available,
            COALESCE(st.covers_available, 0)  AS covers_available,
            COALESCE(st.images_assigned, 0)   AS images_assigned,
            COALESCE(st.images_published, 0)  AS images_published,
            COALESCE(q.queued_drafts, 0)      AS queued_drafts,
            lsu.last_success_ts, lsu.last_success_url, lsu.last_success_title,
            lsu.last_success_post_id,
            la.last_attempt_ts, la.last_attempt_outcome,
            la.last_attempt_error_type, la.last_attempt_error_message
        FROM accounts a
        LEFT JOIN latest_state ls ON ls.account = a.account
        LEFT JOIN stack st ON st.account = a.account
        LEFT JOIN queue q ON q.account = a.account
        LEFT JOIN last_success lsu ON lsu.account = a.account
        LEFT JOIN last_attempt la ON la.account = a.account
        ORDER BY a.account
        """
    ).fetchall()
    return list(rows)


# ---------------------------------------------------------------------------
# Posts view — one CTE chain, filters reference `base` columns directly.
# ---------------------------------------------------------------------------

_BASE_CTES = """
    WITH latest_snapshot AS (
        SELECT DISTINCT ON (post_id)
            post_id, snapshot_date, status,
            impressions, views, shares, favorites,
            area, category, expires_in_days, autorepost, freshness_note
        FROM snapshots
        ORDER BY post_id, snapshot_date DESC
    ),
    active_life AS (
        SELECT post_id, MAX(snapshot_date) AS last_active_date
        FROM snapshots
        WHERE lower(status) = 'active'
        GROUP BY post_id
    ),
    -- When each account's active tab was last scraped successfully. This is
    -- what makes "still live" answerable: the sync only ever writes rows for
    -- posts it *saw*, so a post whose newest snapshot predates its account's
    -- newest sync was absent from the tab by then — it ended.
    account_sync AS (
        SELECT p.account, MAX(s.snapshot_date) AS last_sync_date
        FROM snapshots s
        JOIN posts p ON p.post_id = s.post_id
        GROUP BY p.account
    ),
    latest_ghost AS (
        SELECT DISTINCT ON (post_id) post_id, ghosted, ts AS ghost_ts
        FROM ghost_checks
        ORDER BY post_id, ts DESC
    ),
    base AS (
        SELECT
            p.post_id,
            p.account,
            p.title,
            p.url,
            p.posted_ts,
            p.source,
            s.snapshot_date,
            s.status,
            s.impressions,
            s.views,
            s.shares,
            s.favorites,
            s.freshness_note,
            s.expires_in_days,
            g.ghosted,
            g.ghost_ts,
            asy.last_sync_date,
            (CURRENT_DATE - asy.last_sync_date) AS sync_age_days,
            -- Derived, not stored: 'live' | 'ended' | 'unknown'.
            --
            -- Craigslist's own status string is matched case-insensitively —
            -- ingest stores it verbatim and it has arrived as both 'Active'
            -- and 'active', so an equality test silently classified live posts
            -- as dead depending on when they were scraped.
            CASE
                WHEN s.snapshot_date IS NULL THEN 'unknown'
                WHEN lower(s.status) <> 'active' THEN 'ended'
                WHEN s.snapshot_date < asy.last_sync_date THEN 'ended'
                ELSE 'live'
            END AS liveness,
            GREATEST(1,
                COALESCE(al.last_active_date, s.snapshot_date, CURRENT_DATE)
                - p.posted_ts::date
            ) AS days_active
        FROM posts p
        LEFT JOIN latest_snapshot s ON s.post_id = p.post_id
        LEFT JOIN active_life al ON al.post_id = p.post_id
        LEFT JOIN account_sync asy ON asy.account = p.account
        LEFT JOIN latest_ghost g ON g.post_id = p.post_id
    ),
    base_rated AS (
        SELECT
            base.*,
            COALESCE(views, 0)::float / days_active AS views_per_day,
            COALESCE(impressions, 0)::float / days_active AS impressions_per_day
        FROM base
    )
"""

# How many days a sync can lag before "live" stops meaning anything. The
# scrape runs daily at 06:00, so two days covers one missed run plus slack;
# past that the page says "as of <date>" instead of claiming a post is up.
STALE_SYNC_DAYS = 2


def _build_where(
    *,
    account: str | None,
    status_filter: str | None,
    ghost_filter: str | None,
    since: str | None,
    search: str | None,
    exclude_young_posts: bool,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    if account:
        where.append("account = %s")
        params.append(account)

    if since == "all":
        pass
    elif since:
        where.append("posted_ts >= %s")
        params.append(since)
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS)).date()
        where.append("posted_ts >= %s")
        params.append(cutoff)

    if search:
        where.append("(title ILIKE %s OR post_id ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")

    # Filters on the derived `liveness`, not on Craigslist's raw status string.
    # The old `status = 'Active'` was case-sensitive against a column ingest
    # stores verbatim, so "Active" matched nothing and "Inactive" matched every
    # row — the two filters were exactly backwards from what they claimed.
    if status_filter == "active":
        where.append("liveness = 'live'")
    elif status_filter == "inactive":
        where.append("liveness = 'ended'")
    elif status_filter == "unknown":
        where.append("liveness = 'unknown'")

    if ghost_filter == "visible":
        where.append("ghosted = FALSE")
    elif ghost_filter == "ghosted":
        where.append("ghosted = TRUE")
    elif ghost_filter == "unchecked":
        where.append("ghosted IS NULL")

    if exclude_young_posts:
        where.append(f"(CURRENT_DATE - posted_ts::date) >= {MIN_AGE_DAYS_FOR_RATE}")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def posts_page(
    conn: psycopg.Connection,
    *,
    account: str | None = None,
    status_filter: str | None = None,
    ghost_filter: str | None = None,
    since: str | None = None,
    search: str | None = None,
    sort: str = "posted_ts",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if sort not in _SORT_COLUMNS:
        sort = "posted_ts"
    sort_col = _SORT_COLUMNS[sort]
    direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
    exclude_young = sort in {"views_per_day", "impressions_per_day"}

    where_sql, params = _build_where(
        account=account,
        status_filter=status_filter,
        ghost_filter=ghost_filter,
        since=since,
        search=search,
        exclude_young_posts=exclude_young,
    )

    total = conn.execute(
        f"{_BASE_CTES} SELECT COUNT(*) AS n FROM base_rated {where_sql}",
        params,
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        {_BASE_CTES}
        SELECT * FROM base_rated
        {where_sql}
        ORDER BY {sort_col} {direction} NULLS LAST
        LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    ).fetchall()

    # Counts ignore the liveness filter itself, so the tallies stay put while
    # you click between them instead of collapsing to the one you selected.
    count_where_sql, count_params = _build_where(
        account=account,
        status_filter=None,
        ghost_filter=ghost_filter,
        since=since,
        search=search,
        exclude_young_posts=exclude_young,
    )
    tallies = conn.execute(
        f"""
        {_BASE_CTES}
        SELECT liveness, COUNT(*) AS n FROM base_rated {count_where_sql}
        GROUP BY liveness
        """,
        count_params,
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": list(rows),
        "counts": {r["liveness"]: r["n"] for r in tallies},
        "sync": sync_freshness(conn, account=account),
    }


def sync_freshness(conn: psycopg.Connection, *, account: str | None = None) -> dict:
    """When each account's active tab was last scraped, and whether that is recent.

    Without this the Posts page cannot tell "this post is up" from "nobody has
    checked since the 5th". Both render as a green badge otherwise, and the
    second one is a lie that gets more convincing the longer the scrape stays
    broken.
    """
    rows = conn.execute(
        """
        SELECT p.account,
               MAX(s.snapshot_date) AS last_sync_date,
               (CURRENT_DATE - MAX(s.snapshot_date)) AS age_days
        FROM snapshots s
        JOIN posts p ON p.post_id = s.post_id
        GROUP BY p.account
        ORDER BY p.account
        """
    ).fetchall()
    accounts = [
        {
            "account": r["account"],
            "last_sync_date": r["last_sync_date"],
            "age_days": r["age_days"],
            "stale": (r["age_days"] or 0) > STALE_SYNC_DAYS,
        }
        for r in rows
        if account is None or r["account"] == account
    ]
    return {
        "accounts": accounts,
        "stale": any(a["stale"] for a in accounts) if accounts else True,
        "stale_after_days": STALE_SYNC_DAYS,
    }


# ---------------------------------------------------------------------------
# Post detail
# ---------------------------------------------------------------------------

def post_detail(conn: psycopg.Connection, post_id: str) -> dict | None:
    post = conn.execute(
        f"{_BASE_CTES} SELECT * FROM base_rated WHERE post_id = %s",
        (post_id,),
    ).fetchone()
    if not post:
        return None

    snapshots = conn.execute(
        """
        SELECT snapshot_date, snapshot_ts_utc, status,
               impressions, views, shares, favorites,
               area, category, expires_in_days, autorepost, freshness_note
        FROM snapshots
        WHERE post_id = %s
        ORDER BY snapshot_date
        """,
        (post_id,),
    ).fetchall()

    ghost_history = conn.execute(
        """
        SELECT ts, ghosted
        FROM ghost_checks
        WHERE post_id = %s
        ORDER BY ts
        """,
        (post_id,),
    ).fetchall()

    return {
        "post": post,
        "snapshots": list(snapshots),
        "ghost_history": list(ghost_history),
        "sync": sync_freshness(conn, account=post["account"]),
    }
