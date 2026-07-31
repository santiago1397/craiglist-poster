"""Queue and eligibility.

The server is authoritative for post history and eligibility (decision 15), so
the guardrail maths that used to live in `craigslist_auto.accounts` lives here.
The desktop no longer decides *whether* it may post or *which* account posts —
it asks, and this module answers.

Post history comes from the `posts` table, which event ingest maintains from
`post_attempt(outcome='posted')`. That is deliberate: `drafts.posted_at` only
knows about postings this queue produced, whereas `posts` also carries the 24
historical postings made before the queue existed.

Claim is atomic. `claim_next` runs inside the caller's transaction and takes a
`FOR UPDATE SKIP LOCKED` row lock, so two machines racing the same slot cannot
both walk away with the same draft.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

from ..config import get_settings

# Window and weekday are evaluated in the operator's display timezone. The
# desktop lives in America/New_York; DISPLAY_TZ keeps them aligned.
def _local_now(now: datetime) -> datetime:
    return now.astimezone(get_settings().display_zoneinfo)


def get_guardrails(conn: psycopg.Connection) -> dict:
    row = conn.execute("SELECT * FROM guardrail_settings LIMIT 1").fetchone()
    if row is None:  # pragma: no cover — migration seeds the row
        raise RuntimeError("guardrail_settings is empty; run migrations")
    return dict(row)


def set_posting_enabled(
    conn: psycopg.Connection, *, enabled: bool, reason: str | None = None
) -> dict:
    """Flip the operator kill switch.

    Pausing leaves the queue completely untouched — drafts keep their order and
    resume exactly where they left off. It only makes every account ineligible,
    so the next claim returns nothing. A post already in flight is not
    interrupted; the desktop finishes it and reports normally.
    """
    conn.execute(
        """
        UPDATE guardrail_settings
        SET posting_enabled = %s,
            paused_at     = CASE WHEN %s THEN NULL ELSE NOW() END,
            paused_reason = CASE WHEN %s THEN NULL ELSE %s END,
            updated_at    = NOW()
        WHERE singleton
        """,
        (enabled, enabled, enabled, (reason or "").strip()[:200] or None),
    )
    return get_guardrails(conn)


def update_guardrails(conn: psycopg.Connection, values: dict) -> dict:
    """Patch the singleton settings row. Only known keys are written."""
    allowed = {
        "min_hours_between_posts_same_account",
        "max_posts_per_day_total",
        "max_posts_per_account_per_week",
        "post_window_start_hour",
        "post_window_end_hour",
        "post_weekdays_only",
        "queue_depth_floor",
        "queue_depth_target",
        # Editing (DESIGN_EDITS.md decision 30)
        "edits_enabled",
        "min_hours_between_edits_same_post",
        "max_edits_per_account_per_day",
        "max_edits_per_post_lifetime",
        "edit_window_start_hour",
        "edit_window_end_hour",
        "edits_paused_reason",
    }
    patch = {k: v for k, v in values.items() if k in allowed and v is not None}
    if not patch:
        return get_guardrails(conn)
    sets = ", ".join(f"{k} = %({k})s" for k in patch)
    conn.execute(
        f"UPDATE guardrail_settings SET {sets}, updated_at = NOW() WHERE singleton",
        patch,
    )
    return get_guardrails(conn)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _last_post_by_account(conn: psycopg.Connection) -> dict[str, datetime]:
    rows = conn.execute(
        "SELECT account, MAX(posted_ts) AS last_at FROM posts "
        "WHERE posted_ts IS NOT NULL GROUP BY account"
    ).fetchall()
    return {r["account"]: r["last_at"] for r in rows}


def _posts_last_24h_total(conn: psycopg.Connection, now: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE posted_ts >= %s",
        (now - timedelta(hours=24),),
    ).fetchone()
    return row["n"] or 0


def _posts_last_7d_by_account(conn: psycopg.Connection, now: datetime) -> dict[str, int]:
    rows = conn.execute(
        "SELECT account, COUNT(*) AS n FROM posts WHERE posted_ts >= %s GROUP BY account",
        (now - timedelta(days=7),),
    ).fetchall()
    return {r["account"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def evaluate_eligibility(
    conn: psycopg.Connection,
    accounts: list[str],
    now: datetime | None = None,
) -> dict:
    """Why each account can or cannot post right now.

    Returns {"now": iso, "global_blocks": [...], "accounts": {name: {...}}}.
    `global_blocks` are reasons that stop *every* account (time of day, weekend,
    daily cap) — the desktop surfaces them verbatim.
    """
    now = now or datetime.now(timezone.utc)
    g = get_guardrails(conn)
    local = _local_now(now)

    global_blocks: list[str] = []
    # Operator kill switch. Checked first so a paused system reports one clear
    # reason instead of burying it under time-of-day noise.
    if not g.get("posting_enabled", True):
        reason = (g.get("paused_reason") or "").strip()
        global_blocks.append(
            f"posting paused from the dashboard{': ' + reason if reason else ''}"
        )
    if g["post_weekdays_only"] and local.weekday() >= 5:
        global_blocks.append("weekend: posting restricted to Mon-Fri")
    if not (g["post_window_start_hour"] <= local.hour < g["post_window_end_hour"]):
        global_blocks.append(
            f"outside posting window "
            f"({g['post_window_start_hour']:02d}-{g['post_window_end_hour']:02d} "
            f"{local.tzname()})"
        )
    total_24h = _posts_last_24h_total(conn, now)
    if total_24h >= g["max_posts_per_day_total"]:
        global_blocks.append(
            f"daily cap total: {total_24h}/{g['max_posts_per_day_total']}"
        )

    last_post = _last_post_by_account(conn)
    weekly = _posts_last_7d_by_account(conn, now)
    depths = queue_depths(conn)

    out: dict[str, dict] = {}
    for name in accounts:
        reasons = list(global_blocks)
        last = last_post.get(name)
        if last is not None:
            hours = (now - last).total_seconds() / 3600
            if hours < g["min_hours_between_posts_same_account"]:
                reasons.append(
                    f"cooldown: {hours:.1f}h since last "
                    f"(need {g['min_hours_between_posts_same_account']}h)"
                )
        wk = weekly.get(name, 0)
        if wk >= g["max_posts_per_account_per_week"]:
            reasons.append(f"weekly cap: {wk}/{g['max_posts_per_account_per_week']}")
        depth = depths.get(name, 0)
        if depth == 0:
            reasons.append("queue empty: no drafts for this account")

        out[name] = {
            "eligible": not reasons,
            "reasons": reasons,
            "last_post_at": last,
            "posts_last_7d": wk,
            "queue_depth": depth,
        }

    return {
        "now": now,
        "posts_last_24h_total": total_24h,
        "global_blocks": global_blocks,
        "accounts": out,
    }


def queue_depths(conn: psycopg.Connection) -> dict[str, int]:
    """Count of currently-postable queued drafts per account."""
    rows = conn.execute(
        """
        SELECT account, COUNT(*) AS n
        FROM drafts
        WHERE status = 'queued'
          AND (not_before IS NULL OR not_before <= NOW())
          AND (expires_at IS NULL OR expires_at > NOW())
        GROUP BY account
        """
    ).fetchall()
    return {r["account"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

def expire_stale(conn: psycopg.Connection) -> int:
    """Mark drafts past their expires_at. Returns how many were expired."""
    cur = conn.execute(
        "UPDATE drafts SET status = 'expired', updated_at = NOW() "
        "WHERE status = 'queued' AND expires_at IS NOT NULL AND expires_at <= NOW()"
    )
    return cur.rowcount or 0


def claim_next(
    conn: psycopg.Connection,
    *,
    machine: str,
    candidate_accounts: list[str],
    now: datetime | None = None,
) -> dict:
    """Atomically hand out the next draft this machine should post.

    `candidate_accounts` is the set the machine is *bound* to (its config), not
    the set that is eligible — eligibility is decided here. Among eligible
    accounts that actually have drafts, the one idle longest wins (decision 3),
    preserving the rotation fairness of the old `pick_next_account`.

    Returns {"draft": {...}} or {"draft": None, "reasons": {...}}.
    """
    now = now or datetime.now(timezone.utc)
    expire_stale(conn)

    report = evaluate_eligibility(conn, candidate_accounts, now=now)
    eligible = [n for n, info in report["accounts"].items() if info["eligible"]]
    if not eligible:
        return {"draft": None, "eligibility": report}

    # Longest-idle first. Accounts that have never posted sort first.
    def _idle_key(name: str):
        last = report["accounts"][name]["last_post_at"]
        return last or datetime.min.replace(tzinfo=timezone.utc)

    for account in sorted(eligible, key=_idle_key):
        row = conn.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'queued'
              AND account = %s
              AND (not_before IS NULL OR not_before <= NOW())
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY position
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (account,),
        ).fetchone()
        if row is None:
            continue
        # RETURNING rather than patching the pre-update row by hand — otherwise
        # fields the UPDATE touches (attempts, claimed_at) come back stale.
        claimed = conn.execute(
            """
            UPDATE drafts
            SET status = 'claimed',
                claimed_at = NOW(),
                claimed_by_machine = %s,
                attempts = attempts + 1,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (machine, row["id"]),
        ).fetchone()
        draft = dict(claimed)
        # The desktop needs id + sha256 to fetch and cache the bytes; sha256 is
        # what makes its local cache safe to reuse across runs.
        from . import images as images_svc

        draft["images"] = [
            {"id": i["id"], "slot": i["slot"], "sha256": i["sha256"], "mime": i["mime"]}
            for i in images_svc.images_for_draft(conn, draft["id"])
        ]
        return {"draft": draft, "eligibility": report}

    # Every eligible account raced empty between the depth check and here.
    return {"draft": None, "eligibility": report}


# The Windows scheduled task fires at these local hours on weekdays. Projecting
# against the real fire times rather than "any legal hour" is the difference
# between a useful forecast and a plausible-looking fiction.
TASK_FIRE_HOURS = (9, 13, 17)


def project_schedule(
    conn: psycopg.Connection,
    *,
    accounts: list[str],
    horizon_days: int = 21,
    now: datetime | None = None,
) -> list[dict]:
    """Forecast when each queued draft will actually publish.

    Replays `claim_next`'s decision at every scheduled fire: the longest-idle
    eligible account with drafts wins. Every guardrail is applied to the
    simulated history as it grows, so the forecast tightens and spreads exactly
    the way reality will.

    Approximate by nature — a paused switch, a failed post or an edit changes
    it — so treat it as "roughly when", not a promise.
    """
    now = now or datetime.now(timezone.utc)
    g = get_guardrails(conn)
    tz = get_settings().display_zoneinfo

    if not g.get("posting_enabled", True):
        return []

    # Real history seeds the cooldowns; simulated posts extend it.
    history: list[tuple[str, datetime]] = [
        (r["account"], r["posted_ts"])
        for r in conn.execute(
            "SELECT account, posted_ts FROM posts "
            "WHERE posted_ts >= %s AND posted_ts IS NOT NULL ORDER BY posted_ts",
            (now - timedelta(days=8),),
        ).fetchall()
    ]

    queues: dict[str, list[dict]] = {}
    for account in accounts:
        queues[account] = [
            dict(r) for r in conn.execute(
                """
                SELECT id, title, city, not_before FROM drafts
                WHERE account = %s AND status = 'queued'
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY position
                """,
                (account,),
            ).fetchall()
        ]

    out: list[dict] = []
    local = now.astimezone(tz)
    cursor = local.replace(minute=0, second=0, microsecond=0)

    for _ in range(horizon_days * len(TASK_FIRE_HOURS)):
        if not any(queues.values()):
            break
        fire = _next_fire(cursor, g, tz)
        if (fire - local).days > horizon_days:
            break
        cursor = fire + timedelta(hours=1)
        fire_utc = fire.astimezone(timezone.utc)

        if sum(1 for _, t in history if t >= fire_utc - timedelta(hours=24)) >= g["max_posts_per_day_total"]:
            continue

        best: tuple[datetime, str] | None = None
        for account, drafts in queues.items():
            if not drafts:
                continue
            if drafts[0]["not_before"] and drafts[0]["not_before"] > fire_utc:
                continue
            mine = [t for a, t in history if a == account]
            last = max(mine) if mine else None
            if last and (fire_utc - last) < timedelta(hours=g["min_hours_between_posts_same_account"]):
                continue
            if sum(1 for t in mine if t >= fire_utc - timedelta(days=7)) >= g["max_posts_per_account_per_week"]:
                continue
            key = last or datetime.min.replace(tzinfo=timezone.utc)
            if best is None or key < best[0]:
                best = (key, account)

        if best is None:
            continue
        account = best[1]
        draft = queues[account].pop(0)
        history.append((account, fire_utc))
        out.append({
            "draft_id": draft["id"],
            "account": account,
            "title": draft["title"],
            "city": draft["city"],
            "at": fire_utc,
        })

    return out


def _next_fire(cursor: datetime, g: dict, tz) -> datetime:
    """Next scheduled task fire at or after `cursor`, inside the posting window."""
    c = cursor
    for _ in range(400):
        if g["post_weekdays_only"] and c.weekday() >= 5:
            c = (c + timedelta(days=1)).replace(hour=0)
            continue
        for hour in TASK_FIRE_HOURS:
            if hour < c.hour:
                continue
            if not (g["post_window_start_hour"] <= hour < g["post_window_end_hour"]):
                continue
            return c.replace(hour=hour)
        c = (c + timedelta(days=1)).replace(hour=0)
    return c


# Steps that run before any photo reaches Craigslist. A failure at or before
# these consumed nothing, so the draft can safely go back to the queue
# (decision 16). Anything later burned assets and needs a human.
PRE_UPLOAD_STEPS = frozenset({
    "launch", "warmup", "login_check", "open_post_form", "dismiss_reuse_prompt",
    "advance_to_type", "type_service_offered", "category_skilled_trade",
    "advance_to_form", "form_title", "form_zip", "form_city", "form_license",
    "form_phone", "form_body", "map_confirm",
})


def release_or_park(
    conn: psycopg.Connection,
    *,
    draft_id: int,
    failed_step: str | None,
    failed_message: str | None,
) -> str:
    """Route a failed claim per decision 16. Returns the new status.

    A missing or unrecognised step is treated as post-upload — parking a draft
    for review is recoverable, silently re-uploading images to Craigslist is
    not.
    """
    pre_upload = failed_step in PRE_UPLOAD_STEPS
    new_status = "queued" if pre_upload else "needs_attention"
    conn.execute(
        """
        UPDATE drafts
        SET status = %s,
            claimed_at = NULL,
            claimed_by_machine = NULL,
            failed_step = %s,
            failed_message = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (new_status, failed_step, (failed_message or "")[:1000], draft_id),
    )
    return new_status


def mark_posted(
    conn: psycopg.Connection,
    *,
    draft_id: int,
    post_id: str | None,
    posted_at: datetime | None = None,
) -> None:
    # Stamp the attached images as published. That starts their 30-day reuse
    # cooldown and makes the account claim permanent — Craigslist has now seen
    # these photos under this seller.
    from . import images as images_svc

    images_svc.mark_used(
        conn, [i["id"] for i in images_svc.images_for_draft(conn, draft_id)]
    )
    conn.execute(
        """
        UPDATE drafts
        SET status = 'posted',
            posted_post_id = %s,
            posted_at = COALESCE(%s, NOW()),
            failed_step = NULL,
            failed_message = NULL,
            updated_at = NOW()
        WHERE id = %s
        """,
        (post_id, posted_at, draft_id),
    )
