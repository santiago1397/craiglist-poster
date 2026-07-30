"""Draft CRUD, ordering and the advisory similarity score.

Ordering uses a sparse DOUBLE PRECISION `position` rather than dense integers:
moving a draft writes one row (the midpoint between its new neighbours) instead
of renumbering the whole queue. Positions only need renormalising if someone
reorders the same slot thousands of times, which `_renormalise` handles.

Similarity is advisory and never blocks (decision 6). It is computed against
`body_head` only — the ~6,000-word keyword tail is identical across every ad, so
scoring the assembled body would peg every comparison near 0.98 and tell you
nothing.
"""
from __future__ import annotations

from typing import Any

import psycopg

_EDITABLE = (
    "account", "title", "body", "body_head", "county", "city", "service_offered",
    "postal_code", "license_number", "phone_number", "not_before", "expires_at",
    "reviewed", "status",
)

# Only these transitions are allowed from the dashboard. Posting-side statuses
# (claimed/posted) are owned by the claim protocol, not by an operator edit.
_OPERATOR_STATUSES = frozenset({"queued", "needs_attention", "expired"})


def list_drafts(
    conn: psycopg.Connection,
    *,
    status: str | None = None,
    account: str | None = None,
    reviewed: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        where.append("status = %(status)s")
        params["status"] = status
    if account:
        where.append("account = %(account)s")
        params["account"] = account
    if reviewed is not None:
        where.append("reviewed = %(reviewed)s")
        params["reviewed"] = reviewed
    clause = " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT * FROM drafts
        WHERE {clause}
        ORDER BY
            CASE status WHEN 'needs_attention' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
            account, position, id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM drafts WHERE {clause}", params
    ).fetchone()["n"]
    return {"drafts": [dict(r) for r in rows], "total": total}


def get_draft(conn: psycopg.Connection, draft_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM drafts WHERE id = %s", (draft_id,)).fetchone()
    return dict(row) if row else None


def _next_position(conn: psycopg.Connection, account: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS p FROM drafts WHERE account = %s",
        (account,),
    ).fetchone()
    return float(row["p"]) + 1024.0


def create_draft(conn: psycopg.Connection, payload: dict) -> dict:
    fields = {k: v for k, v in payload.items() if k in _EDITABLE and k != "status"}
    fields.setdefault("account", payload["account"])
    fields["position"] = _next_position(conn, fields["account"])
    fields["source"] = payload.get("source", "manual")

    cols = ", ".join(fields)
    placeholders = ", ".join(f"%({k})s" for k in fields)
    row = conn.execute(
        f"INSERT INTO drafts ({cols}) VALUES ({placeholders}) RETURNING *", fields
    ).fetchone()
    return dict(row)


def update_draft(conn: psycopg.Connection, draft_id: int, payload: dict) -> dict | None:
    fields = {k: v for k, v in payload.items() if k in _EDITABLE}
    if "status" in fields and fields["status"] not in _OPERATOR_STATUSES:
        raise ValueError(
            f"status {fields['status']!r} is owned by the posting flow, not the dashboard"
        )
    if not fields:
        return get_draft(conn, draft_id)
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["id"] = draft_id
    row = conn.execute(
        f"UPDATE drafts SET {sets}, updated_at = NOW() WHERE id = %(id)s RETURNING *",
        fields,
    ).fetchone()
    return dict(row) if row else None


def delete_draft(conn: psycopg.Connection, draft_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM drafts WHERE id = %s AND status <> 'claimed'", (draft_id,)
    )
    return (cur.rowcount or 0) > 0


def _renormalise(conn: psycopg.Connection, account: str) -> None:
    """Rewrite positions as 1024, 2048, ... when neighbours get too close."""
    conn.execute(
        """
        WITH ordered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY position, id) AS rn
            FROM drafts WHERE account = %s
        )
        UPDATE drafts d SET position = o.rn * 1024.0
        FROM ordered o WHERE d.id = o.id
        """,
        (account,),
    )


def reorder_draft(
    conn: psycopg.Connection, draft_id: int, *, after_id: int | None
) -> dict | None:
    """Move `draft_id` directly after `after_id`, or to the head if None."""
    draft = get_draft(conn, draft_id)
    if draft is None:
        return None
    account = draft["account"]

    if after_id is None:
        row = conn.execute(
            "SELECT MIN(position) AS p FROM drafts WHERE account = %s", (account,)
        ).fetchone()
        head = float(row["p"]) if row["p"] is not None else 1024.0
        new_pos = head / 2.0
        gap = head
    else:
        anchor = get_draft(conn, after_id)
        if anchor is None or anchor["account"] != account:
            raise ValueError("after_id must be a draft on the same account")
        nxt = conn.execute(
            "SELECT MIN(position) AS p FROM drafts "
            "WHERE account = %s AND position > %s AND id <> %s",
            (account, anchor["position"], draft_id),
        ).fetchone()
        if nxt["p"] is None:
            new_pos = float(anchor["position"]) + 1024.0
            gap = 1024.0
        else:
            new_pos = (float(anchor["position"]) + float(nxt["p"])) / 2.0
            gap = float(nxt["p"]) - float(anchor["position"])

    conn.execute(
        "UPDATE drafts SET position = %s, updated_at = NOW() WHERE id = %s",
        (new_pos, draft_id),
    )
    # Doubles run out of room eventually; rebuild the ladder before they do.
    if gap < 1e-6:
        _renormalise(conn, account)
    return get_draft(conn, draft_id)


def similarity_report(
    conn: psycopg.Connection, *, head: str, exclude_draft_id: int | None = None, days: int = 60
) -> dict:
    """Advisory only — the closest queued draft and the closest recent posting.

    Posts are compared on title alone: the `posts` table has never stored bodies
    (only `post_attempt.ad_title` is shipped), so a body comparison is not
    available for anything published before this queue existed.
    """
    draft_row = conn.execute(
        """
        SELECT id, title, similarity(body_head, %(head)s) AS score
        FROM drafts
        WHERE body_head IS NOT NULL
          AND (%(exclude)s::bigint IS NULL OR id <> %(exclude)s::bigint)
        ORDER BY score DESC NULLS LAST
        LIMIT 1
        """,
        {"head": head, "exclude": exclude_draft_id},
    ).fetchone()

    post_row = conn.execute(
        """
        SELECT p.post_id, p.title, p.posted_ts,
               similarity(p.title, %(head)s) AS score,
               EXISTS (
                   SELECT 1 FROM ghost_checks g
                   WHERE g.post_id = p.post_id AND g.ghosted
               ) AS ghosted
        FROM posts p
        WHERE p.posted_ts >= NOW() - make_interval(days => %(days)s)
          AND p.title IS NOT NULL
        ORDER BY score DESC NULLS LAST
        LIMIT 1
        """,
        {"head": head, "days": days},
    ).fetchone()

    return {
        "closest_draft": dict(draft_row) if draft_row else None,
        "closest_post": dict(post_row) if post_row else None,
    }
