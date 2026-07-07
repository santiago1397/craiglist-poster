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

    Combines: latest account_state (eligibility), latest photo_inventory,
    most-recent successful post_attempt (last post link + title), most-recent
    attempt of any outcome (last-attempt indicator including failures).
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
        latest_inventory AS (
            SELECT DISTINCT ON (account)
                account, ts AS inventory_ts,
                photos_total, photos_never_used, photos_eligible,
                covers_total, covers_never_used, covers_eligible
            FROM photo_inventory_snapshots
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
            UNION SELECT account FROM latest_inventory
            UNION SELECT account FROM last_success
            UNION SELECT account FROM last_attempt WHERE account != '(none)'
        )
        SELECT
            a.account,
            ls.eligible_now, ls.next_eligible_at, ls.block_reasons,
            ls.posts_last_24h_total, ls.posts_last_7d_this_account,
            ls.stats_sync_health, ls.ts AS state_ts,
            li.photos_total, li.photos_never_used, li.photos_eligible,
            li.covers_total, li.covers_never_used, li.covers_eligible,
            li.inventory_ts,
            lsu.last_success_ts, lsu.last_success_url, lsu.last_success_title,
            lsu.last_success_post_id,
            la.last_attempt_ts, la.last_attempt_outcome,
            la.last_attempt_error_type, la.last_attempt_error_message
        FROM accounts a
        LEFT JOIN latest_state ls ON ls.account = a.account
        LEFT JOIN latest_inventory li ON li.account = a.account
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
        WHERE status = 'Active'
        GROUP BY post_id
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
            GREATEST(1,
                COALESCE(al.last_active_date, s.snapshot_date, CURRENT_DATE)
                - p.posted_ts::date
            ) AS days_active
        FROM posts p
        LEFT JOIN latest_snapshot s ON s.post_id = p.post_id
        LEFT JOIN active_life al ON al.post_id = p.post_id
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

    if status_filter == "active":
        where.append("status = 'Active'")
    elif status_filter == "inactive":
        where.append("status IS DISTINCT FROM 'Active'")

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

    return {"total": total, "limit": limit, "offset": offset, "items": list(rows)}


# ---------------------------------------------------------------------------
# Post detail
# ---------------------------------------------------------------------------

def post_detail(conn: psycopg.Connection, post_id: str) -> dict | None:
    post = conn.execute(
        "SELECT * FROM posts WHERE post_id = %s",
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

    return {"post": post, "snapshots": list(snapshots), "ghost_history": list(ghost_history)}
