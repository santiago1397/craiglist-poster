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

import json
from datetime import datetime, timedelta, timezone

import psycopg
from loguru import logger

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
        "max_posts_per_account_per_day",
        "max_posts_per_account_per_week",
        "post_window_start_hour",
        "post_window_end_hour",
        "post_weekdays_only",
        "queue_depth_floor",
        "queue_depth_target",
        # How long a failed account stands down (migration 0029).
        "failure_backoff_minutes",
        "billing_failure_backoff_minutes",
        # Editing (DESIGN_EDITS.md decision 30)
        "edits_enabled",
        "min_hours_between_edits_same_post",
        "max_edits_per_account_per_day",
        "max_edits_per_post_lifetime",
        "edit_window_start_hour",
        "edit_window_end_hour",
        "edits_paused_reason",
        # Image reuse (migration 0027). Here rather than in generation_settings
        # because these constrain what may go out, like the posting caps above —
        # generation_settings is about what it costs to make things.
        "image_owner_binding",
        "image_reuse_cooldown_days",
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


# Steps whose failure is a configuration problem rather than a blip. A missing
# payment method fails identically on every retry, and each retry parks another
# draft and burns another set of uploaded images, so it gets the long backoff.
CONFIG_FAILURE_STEPS = ("billing",)


def _last_failure_by_account(
    conn: psycopg.Connection, now: datetime, lookback_hours: int = 48
) -> dict[str, dict]:
    """Most recent failed attempt per account, with the step it died on.

    The rotation used to be blind to this: every count in `evaluate_eligibility`
    reads `posts`, which only ingest fills on success, so an account that failed
    stayed the longest-idle one and was handed the very next fire. One account
    with a broken card consumed all eight fires of a day while the other three
    posted nothing.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (account) account, ts, failed_step, error_message
        FROM post_attempts
        WHERE outcome LIKE 'failed%%'
          AND ts >= %s
          AND account <> '(none)'
        ORDER BY account, ts DESC
        """,
        (now - timedelta(hours=lookback_hours),),
    ).fetchall()
    return {r["account"]: dict(r) for r in rows}


def _failure_backoff(g: dict, failed_step: str | None) -> int:
    """Minutes an account waits after a failure, by kind of failure."""
    if failed_step in CONFIG_FAILURE_STEPS:
        return int(g.get("billing_failure_backoff_minutes") or 0)
    return int(g.get("failure_backoff_minutes") or 0)


def _posts_last_24h_total(conn: psycopg.Connection, now: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE posted_ts >= %s",
        (now - timedelta(hours=24),),
    ).fetchone()
    return row["n"] or 0


def _accounts_with_claim(conn: psycopg.Connection) -> set[str]:
    """Accounts holding a draft that was claimed but has not reported yet.

    Bounded by `release_stale_claims`, which parks anything older than
    STALE_CLAIM_MINUTES — so a machine that dies mid-post blocks its account
    until the reaper runs, not forever.
    """
    rows = conn.execute(
        "SELECT DISTINCT account FROM drafts WHERE status = 'claimed'"
    ).fetchall()
    return {r["account"] for r in rows}


def _posts_today_by_account(
    conn: psycopg.Connection, now: datetime
) -> dict[str, int]:
    """Posts each account has made so far *today*, in the operator's timezone.

    A calendar-day count, deliberately unlike every other cap here. The daily
    total and the weekly cap are rolling windows, which is why both are
    configured one above the figure they enforce — a post lands minutes after
    its fire, so yesterday's post at the same clock time is still inside the
    window. Counting "two per account per day" that way would mean yesterday
    afternoon's post still counted at this morning's fire, and the morning slot
    would never open at all.

    The boundary is derived from the caller's `now` rather than the database's
    `NOW()`, even though the two agree in production. Every other check in
    `evaluate_eligibility` honours the injected clock, and one that quietly did
    not would make this cap untestable — a test pinning it would pass whatever
    the code did, because the rows it seeded would land on a different calendar
    date than the real today.
    """
    midnight = _local_now(now).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute(
        "SELECT account, COUNT(*) AS n FROM posts WHERE posted_ts >= %s GROUP BY account",
        (midnight,),
    ).fetchall()
    return {r["account"]: r["n"] for r in rows}


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
    last_fail = _last_failure_by_account(conn, now)
    today = _posts_today_by_account(conn, now)
    weekly = _posts_last_7d_by_account(conn, now)
    depths = queue_depths(conn)
    in_flight = _accounts_with_claim(conn)

    out: dict[str, dict] = {}
    for name in accounts:
        reasons = list(global_blocks)
        # A claim that has not reported yet is invisible to every count above,
        # because all of them read `posts`, which ingest only fills once the
        # attempt comes back. Two claims a minute apart therefore both pass the
        # cooldown and the daily cap on stale history, and the browser lease
        # serialises them without cancelling either — so both publish. The
        # in-flight claim is the only evidence that exists in that window.
        if name in in_flight:
            reasons.append("a post is already in flight for this account")
        last = last_post.get(name)
        if last is not None:
            hours = (now - last).total_seconds() / 3600
            if hours < g["min_hours_between_posts_same_account"]:
                reasons.append(
                    f"cooldown: {hours:.1f}h since last "
                    f"(need {g['min_hours_between_posts_same_account']}h)"
                )
        # Two a day, one in the morning block and one in the afternoon. The
        # cooldown alone very nearly implies this — five hours twice does not
        # fit inside a ten-hour window — but "very nearly" is not a guarantee,
        # and the arithmetic quietly stops holding the moment somebody widens
        # the window in Settings. State the rule instead of deriving it.
        td = today.get(name, 0)
        if td >= g["max_posts_per_account_per_day"]:
            reasons.append(
                f"daily cap: {td}/{g['max_posts_per_account_per_day']} today"
            )
        wk = weekly.get(name, 0)
        if wk >= g["max_posts_per_account_per_week"]:
            reasons.append(f"weekly cap: {wk}/{g['max_posts_per_account_per_week']}")
        depth = depths.get(name, 0)
        if depth == 0:
            reasons.append("queue empty: no drafts for this account")

        # Back off after a failure so one broken account cannot take every fire
        # of the day. This is not a kill switch: it expires on its own, so a
        # transient failure costs one slot and needs nobody.
        fail = last_fail.get(name)
        backoff_until = None
        if fail is not None:
            minutes = _failure_backoff(g, fail["failed_step"])
            if minutes > 0:
                until = fail["ts"] + timedelta(minutes=minutes)
                if until > now:
                    backoff_until = until
                    mins_left = int((until - now).total_seconds() // 60) + 1
                    step = fail["failed_step"] or "an unknown step"
                    extra = (
                        " — this is a setup problem, not a blip: check the "
                        "account's payment method"
                        if fail["failed_step"] in CONFIG_FAILURE_STEPS
                        else ""
                    )
                    reasons.append(
                        f"backing off after a failure at {step}: "
                        f"{mins_left}m left{extra}"
                    )

        out[name] = {
            "eligible": not reasons,
            "reasons": reasons,
            "last_post_at": last,
            "last_failure_at": fail["ts"] if fail else None,
            "last_failure_step": fail["failed_step"] if fail else None,
            "backoff_until": backoff_until,
            "posts_today": td,
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


# The Windows Scheduled Task runs `cl post` with a 30-minute execution limit
# (scripts/install-schedule.ps1), so a claim older than this cannot still be
# in flight — the process that held it is gone.
STALE_CLAIM_MINUTES = 45


def release_stale_claims(
    conn: psycopg.Connection, older_than_minutes: int = STALE_CLAIM_MINUTES
) -> int:
    """Rescue drafts a machine claimed and never reported on.

    A claim is a promise to report an outcome. When the desktop dies between
    the two — Task Scheduler hitting its execution limit mid-upload, a reboot,
    a crash before the event is emitted — nothing ever moved the draft on. It
    sat 'claimed' forever, invisible to `queue_depths` (which counts only
    'queued'), so the account reported "queue empty: no drafts" and silently
    stopped posting. The queue looked healthy because top-up kept refilling
    around the corpse.

    **These are parked, not requeued, and that is deliberate.** We do not know
    what the dead run did. If it published and its `post_attempt` is still
    sitting in the desktop's outbox, requeueing would post the same ad twice —
    and the outbox is durable, so that event *will* arrive eventually. Parking
    costs one click on Review; a duplicate live ad costs an account.

    This is self-healing in the good case: `_route_draft` runs on first receipt
    of the event whatever the draft's current status, so a late-arriving
    `posted` flips the parked draft straight to 'posted' with no human involved.

    Mirrors `edits.release_stale_claims`. Returns how many were rescued.
    """
    cur = conn.execute(
        """
        UPDATE drafts
        SET status = 'needs_attention',
            claimed_at = NULL,
            failed_step = 'stale_claim',
            failed_message = 'Machine ' || COALESCE(claimed_by_machine, '?')
                || ' claimed this draft at ' || to_char(claimed_at, 'YYYY-MM-DD HH24:MI')
                || ' UTC and never reported an outcome. It may or may not have '
                || 'published — check the account on Craigslist before requeueing.',
            updated_at = NOW()
        WHERE status = 'claimed'
          AND claimed_at IS NOT NULL
          AND claimed_at < NOW() - make_interval(mins => %s)
        """,
        (older_than_minutes,),
    )
    return cur.rowcount or 0


# A "Post now" request is refused synchronously when the guardrails don't allow
# it, so a live flag means the desktop simply hasn't polled yet — normally a
# matter of seconds. This is the backstop for the case nothing else covers: the
# daemon was down when the button was pressed. Long enough to outlast a posting
# run holding the browser lease, short enough that a Friday click can never be
# picked up on Monday.
POST_REQUEST_TTL_MINUTES = 20


def expire_post_requests(
    conn: psycopg.Connection, older_than_minutes: int = POST_REQUEST_TTL_MINUTES
) -> int:
    """Drop post requests nothing ever collected, and say so on the draft.

    The message matters more than the cleanup. Without it, "I pressed Post now
    and nothing happened" is unanswerable from the dashboard — every other trace
    of the refusal lives in `logs/run.log` on a machine the operator is probably
    not sitting at.
    """
    cur = conn.execute(
        """
        UPDATE drafts
        SET post_requested_at = NULL,
            post_requested_by = NULL,
            post_request_error = 'Post now expired after '
                || make_interval(mins => %s) || ' without the desktop picking it '
                || 'up. Nothing was posted. The machine was most likely offline '
                || 'or busy with another run.',
            updated_at = NOW()
        WHERE post_requested_at IS NOT NULL
          AND post_requested_at < NOW() - make_interval(mins => %s)
        """,
        (older_than_minutes, older_than_minutes),
    )
    return cur.rowcount or 0


def pending_post_requests(
    conn: psycopg.Connection, *, accounts: list[str], limit: int = 10
) -> list[dict]:
    """Live "Post now" requests for these accounts, oldest first."""
    rows = conn.execute(
        """
        SELECT id, account, title, post_requested_at AS requested_at
        FROM drafts
        WHERE post_requested_at IS NOT NULL
          AND post_requested_at > NOW() - make_interval(mins => %s)
          AND status = 'queued'
          AND account = ANY(%s)
        ORDER BY post_requested_at
        LIMIT %s
        """,
        (POST_REQUEST_TTL_MINUTES, accounts, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def stuck_claims(conn: psycopg.Connection) -> list[dict]:
    """Drafts currently held by a claim, newest first.

    A short list here is normal — it is a post in progress. A list that does not
    drain is the reaper's queue.
    """
    rows = conn.execute(
        """
        SELECT id, account, title, claimed_at, claimed_by_machine, attempts,
               EXTRACT(EPOCH FROM (NOW() - claimed_at)) / 60 AS held_minutes
        FROM drafts
        WHERE status = 'claimed'
        ORDER BY claimed_at
        """
    ).fetchall()
    return [dict(r) for r in rows]


def claim_next(
    conn: psycopg.Connection,
    *,
    machine: str,
    candidate_accounts: list[str],
    now: datetime | None = None,
    draft_id: int | None = None,
) -> dict:
    """Atomically hand out the next draft this machine should post.

    `candidate_accounts` is the set the machine is *bound* to (its config), not
    the set that is eligible — eligibility is decided here. Among eligible
    accounts that actually have drafts, the one idle longest wins (decision 3),
    preserving the rotation fairness of the old `pick_next_account`.

    `draft_id` pins the claim to one specific draft — the operator pressed
    "Post now" on it. Everything else is identical: the same guardrails decide,
    on the server, exactly as they do for a scheduled fire. What changes is only
    *which* draft is considered, never *whether* it may go out.

    The invariant that matters in that mode: a targeted claim either hands back
    the draft that was asked for or hands back nothing. It must never fall
    through to the next account in the rotation — the operator clicked one row
    and a different ad publishing instead would be worse than not posting.

    Returns {"draft": {...}} or {"draft": None, "reasons": {...}}.
    """
    now = now or datetime.now(timezone.utc)
    expire_stale(conn)
    # Rescue anything a dead machine left mid-flight before deciding what to
    # hand out. Runs here rather than on a timer because the posting slot is
    # exactly when a stranded draft costs you something, and it is the one
    # moment we know a machine is alive to ask.
    rescued = release_stale_claims(conn)
    if rescued:
        logger.warning(
            f"released {rescued} stale draft claim(s) into needs_attention — "
            f"a machine claimed them and never reported an outcome"
        )
    expire_post_requests(conn)

    # Resolve a targeted claim before spending anything on eligibility, so the
    # refusals that are about *this draft* read as such rather than arriving
    # dressed up as a guardrail block.
    target = None
    if draft_id is not None:
        target = conn.execute(
            "SELECT id, account, status, post_requested_at FROM drafts WHERE id = %s",
            (draft_id,),
        ).fetchone()
        if target is None:
            return {
                "draft": None,
                "refused": "no_such_draft",
                "detail": f"draft {draft_id} does not exist",
            }
        # The flag IS the authorisation, and this check is the whole of it.
        # Without it a machine token could name any draft id and pull it out of
        # order; with it, a machine can only execute something an operator
        # already asked for from the dashboard. Do not drop this as redundant
        # with the status check — it is not.
        if target["post_requested_at"] is None:
            return {
                "draft": None,
                "refused": "not_requested",
                "detail": (
                    f"draft {draft_id} has no live post request — it was "
                    f"cancelled, already handled, or never requested"
                ),
            }
        if target["account"] not in candidate_accounts:
            return {
                "draft": None,
                "refused": "not_bound",
                "detail": (
                    f"draft {draft_id} belongs to {target['account']}, which is "
                    f"not bound to this machine"
                ),
            }
        if target["status"] != "queued":
            return {
                "draft": None,
                "refused": "not_queued",
                "detail": (
                    f"draft {draft_id} is {target['status']}, not queued — "
                    f"another machine may have claimed it first"
                ),
            }

    report = evaluate_eligibility(conn, candidate_accounts, now=now)
    eligible = [n for n, info in report["accounts"].items() if info["eligible"]]
    if not eligible:
        return {"draft": None, "eligibility": report}

    # Longest-idle first. Accounts that have never posted sort first.
    def _idle_key(name: str):
        # Ranked on the last time we *tried*, not the last time we succeeded.
        # On success alone, an account that keeps failing never advances its
        # timestamp, stays the longest-idle one, and wins every fire forever.
        info = report["accounts"][name]
        stamps = [t for t in (info["last_post_at"], info.get("last_failure_at")) if t]
        return max(stamps) if stamps else datetime.min.replace(tzinfo=timezone.utc)

    if target is not None:
        # One account, one draft, no fallback. If its account is blocked the
        # answer is "no", not "here is someone else's ad".
        if target["account"] not in eligible:
            return {"draft": None, "eligibility": report}
        order = [target["account"]]
    else:
        order = sorted(eligible, key=_idle_key)

    for account in order:
        # Same row lock either way; the targeted form just pins the id instead
        # of taking the head of the queue.
        row = conn.execute(
            f"""
            SELECT * FROM drafts
            WHERE status = 'queued'
              AND account = %s
              AND (not_before IS NULL OR not_before <= NOW())
              AND (expires_at IS NULL OR expires_at > NOW())
              {"AND id = %s AND post_requested_at IS NOT NULL"
               if target is not None else ""}
            ORDER BY position
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (account, draft_id) if target is not None else (account,),
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

        _backstop_cover(conn, draft)
        draft["images"] = [
            {"id": i["id"], "slot": i["slot"], "sha256": i["sha256"], "mime": i["mime"]}
            for i in images_svc.images_for_draft(conn, draft["id"])
        ]
        return {"draft": draft, "eligibility": report}

    if target is not None:
        # The account was eligible but the row did not come back: either another
        # claim took it in the last few milliseconds, or it carries a not_before
        # /expires_at window that excludes right now.
        return {
            "draft": None,
            "refused": "unavailable",
            "detail": (
                f"draft {draft_id} could not be claimed — it is outside its "
                f"not_before/expires_at window, or another machine took it first"
            ),
            "eligibility": report,
        }

    # Every eligible account raced empty between the depth check and here.
    return {"draft": None, "eligibility": report}


def _backstop_cover(conn: psycopg.Connection, draft: dict) -> None:
    """Give a draft a thumbnail if you never chose one.

    Covers are picked by hand, which means some drafts reach their posting slot
    without one. An empty slot 1 does not publish a coverless ad — the desktop
    uploads in slot order and Craigslist thumbnails whatever lands first — so
    the real outcome is a random roof photo silently promoted into the most
    valuable position on the listing.

    Two rules make this safe to do automatically:

    * A draft with **no images at all** is left alone. That is the deliberate
      imageless roll, not an oversight, and giving it a cover would undo the
      only variation left in the image profile.
    * The substitution is **reported**. It files itself as a flow_error so it
      surfaces in Diagnostics like anything else the machine decided on its
      own — a cover you would not have picked is worth knowing about, even
      though it is not a failure.
    """
    from . import images as images_svc

    attached = images_svc.images_for_draft(conn, draft["id"])
    if not attached:
        return  # deliberately imageless
    if any(i["slot"] == images_svc.COVER_SLOT for i in attached):
        return

    chosen = images_svc.attach_cover(
        conn, draft_id=draft["id"], account=draft["account"]
    )
    event_id = f"cover-auto:{draft['id']}:{draft.get('attempts') or 0}"
    if chosen is None:
        message = (
            f"Draft {draft['id']} was claimed with no cover and the cover stack "
            f"is empty for {draft['account']}, so its first photo becomes the "
            f"Craigslist thumbnail. Add covers on the Images page."
        )
    else:
        message = (
            f"No cover was chosen for draft {draft['id']}, so image "
            f"{chosen['id']} was picked automatically at claim time. Pick "
            f"covers in Review to control the thumbnail."
        )
    logger.info(message)
    conn.execute(
        """
        INSERT INTO flow_errors (
            event_id, ts, machine, flow, step, account,
            error_type, error_message, context
        )
        VALUES (%s, NOW(), %s, 'post', 'cover_backstop', %s,
                'cover_auto_chosen', %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            event_id,
            draft.get("claimed_by_machine"),
            draft["account"],
            message,
            json.dumps({
                "draft_id": draft["id"],
                "image_id": chosen["id"] if chosen else None,
                "cover_stack_empty": chosen is None,
            }),
        ),
    )


# The Windows scheduled task fires at these local hours on weekdays. Projecting
# against the real fire times rather than "any legal hour" is the difference
# between a useful forecast and a plausible-looking fiction.
#
# A morning block and an afternoon block, four fires each, matching the two
# triggers in scripts/install-schedule.ps1. Four accounts and longest-idle-first
# selection make the rotation land on the pairing by itself: fire k and fire k+4
# are six hours apart, an hour clear of the five-hour cooldown, so a slow run or
# one displaced slot does not cost a fire.
#
# (hour, minute) pairs in DISPLAY_TZ. These were whole hours until the posting
# window narrowed to 08:00-17:00, which does not contain eight whole hours that
# also clear the cooldown -- see below -- so `_next_fire` and `project_schedule`
# were rewritten to carry minutes.
#
# **The 45-minute spacing is what makes the cooldown work.** Each block spans
# 2h15m, so fire k and fire k+4 are always six hours apart: 08:00 pairs with
# 14:00, 10:15 with 16:15. That is an hour of clearance over the five-hour
# same-account cooldown, which an account needs to take both a morning and an
# afternoon slot. Widen a block and that clearance shrinks by the same amount;
# at a three-hour block it reaches exactly five hours and every afternoon fire
# starts getting refused, because a post lands minutes *after* the fire that
# produced it and the cooldown is measured from the post.
#
# The last fire is 16:15 rather than 17:00 on purpose: the window check is
# `start <= hour < end`, so an end hour of 17 refuses anything from 17:00:00.
TASK_FIRE_TIMES = (
    (8, 0), (8, 45), (9, 30), (10, 15),
    (14, 0), (14, 45), (15, 30), (16, 15),
)


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
    # Minutes are kept. Truncating to the hour used to be harmless because no
    # two fires shared one; now it would re-offer 08:00 at 08:30.
    cursor = local.replace(second=0, microsecond=0)

    for _ in range(horizon_days * len(TASK_FIRE_TIMES)):
        if not any(queues.values()):
            break
        fire = _next_fire(cursor, g, tz)
        if (fire - local).days > horizon_days:
            break
        # One minute, not one hour: fires are 45 minutes apart, so stepping an
        # hour would skip the next one.
        cursor = fire + timedelta(minutes=1)
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
            # Calendar day in the display timezone, matching
            # `_posts_today_by_account`. Omitting this would forecast a third
            # post on a day the server will refuse one.
            if sum(
                1 for t in mine if t.astimezone(tz).date() == fire.date()
            ) >= g["max_posts_per_account_per_day"]:
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
            c = (c + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        for hour, minute in TASK_FIRE_TIMES:
            # Compared as a pair: hour alone would return a slot already gone
            # past earlier in the same hour, now that two can share one.
            if (hour, minute) < (c.hour, c.minute):
                continue
            if not (g["post_window_start_hour"] <= hour < g["post_window_end_hour"]):
                continue
            return c.replace(hour=hour, minute=minute)
        c = (c + timedelta(days=1)).replace(hour=0, minute=0)
    return c


# Steps that run before any photo reaches Craigslist. A failure at or before
# these consumed nothing, so the draft can safely go back to the queue
# (decision 16). Anything later burned assets and needs a human.
PRE_UPLOAD_STEPS = frozenset({
    # 'build_ad' happens on the desktop before the browser opens at all; 'launch'
    # covers the browser lease and Chrome startup. Both are reported explicitly
    # so a failure that touched nothing returns to the queue instead of costing
    # a manual rescue — the desktop must never send None here.
    "build_ad",
    "launch", "warmup", "login_check", "open_post_form", "dismiss_reuse_prompt",
    "advance_to_type", "type_service_offered", "category_skilled_trade",
    "advance_to_form", "form_title", "form_zip", "form_city", "form_license",
    "form_phone", "form_body",
    # Craigslist rejected the form and redisplayed it. Nothing was uploaded —
    # the run never got past the details page — so the draft goes back to the
    # queue and its images stay clean. This exists because the rejection used
    # to surface as a photo_upload timeout and cost 24 images per attempt.
    "form_validation",
    "map_confirm",
    # The same trap as 'form_validation', one page later: an unanswered region
    # question on the map step leaves the run somewhere with no photo uploader,
    # which used to surface as a photo_upload timeout — parking the draft and
    # retiring 24 images for a run that uploaded nothing.
    "map_validation",
})


def release_or_park(
    conn: psycopg.Connection,
    *,
    draft_id: int,
    failed_step: str | None,
    failed_message: str | None,
    photos_confirmed: int | None = None,
) -> str:
    """Route a failed claim per decision 16. Returns the new status.

    A missing or unrecognised step is treated as post-upload — parking a draft
    for review is recoverable, silently re-uploading images to Craigslist is
    not.

    A post-upload failure also **retires the images the site already saw**.
    Nothing did this before: `mark_used` ran only from `mark_posted`, so a run
    that uploaded four photos and then died at `publish` left all four looking
    unused, free to be handed to another draft and shown to Craigslist a second
    time under the same account.
    """
    pre_upload = failed_step in PRE_UPLOAD_STEPS
    new_status = "queued" if pre_upload else "needs_attention"
    if not pre_upload:
        from . import images as images_svc

        burned = images_svc.mark_confirmed_used(
            conn, draft_id=draft_id, photos_confirmed=photos_confirmed
        )
        if burned:
            logger.info(
                f"draft {draft_id} failed at {failed_step!r} after uploading; "
                f"retired {burned} image(s) Craigslist had already rendered"
            )
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
    # Stamp the attached images as published. That starts their reuse cooldown
    # (`image_reuse_cooldown_days`) and, while `image_owner_binding` is on,
    # makes the account claim permanent — Craigslist has now seen these photos
    # under this seller.
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
