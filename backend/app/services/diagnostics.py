"""One place that answers: what is broken right now?

Before this module, the answer was spread across four tables and one of them was
write-only. `flow_errors` had been collecting every background failure since
migration 0002 — queue-sync outages, clamped guardrails, image download failures,
stats-sync crashes — and nothing in the API ever read it back. Its own docstring
in events.py promised the opposite: "so a silent failure in a background job is
visible from the dashboard instead of only in run.log on a machine nobody is
looking at."

The organising idea is a **problem**: one row, one thing wrong, with a severity,
a plain-English explanation, and where to go to fix it. Four sources feed it:

    flow_errors        every background flow that raised, including degraded
                       posts mirrored in by ingest
    post_attempts      failed posting runs, with the step and the page dumps
    drafts             claims a machine took and never reported on
    machine silence    a machine that has stopped calling home entirely

Severity is about consequence, not noise level:

    critical   posting is stopped, or a live ad is wrong right now
    warning    something failed but the system routed around it
    info       worth knowing, nothing is broken

Everything here is read-only except `acknowledge`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

# A machine that has not called home in this long is treated as down. The
# reporter daemon heartbeats every 5 minutes, so three missed beats is a
# deliberate signal rather than a slow network.
MACHINE_SILENT_MINUTES = 20

# How far back the problem feed looks. Long enough to cover a weekend outage.
DEFAULT_WINDOW_HOURS = 72


# ---------------------------------------------------------------------------
# Plain-English explanations
#
# Each entry turns a machine-generated failure into something actionable. The
# alternative is a dashboard that shows `TimeoutError` and leaves you to
# remember what that means at 9am on a Monday.
# ---------------------------------------------------------------------------

_FLOW_HELP: dict[str, str] = {
    "post": (
        "The posting run itself failed. Open the artifacts below to see the page "
        "Craigslist actually served — a changed selector looks identical to an "
        "outage from the error text alone."
    ),
    "queue_sync": (
        "The desktop could not reach this server. Posting is fail-closed, so it "
        "will post nothing until this clears. Check the machine's QUEUE_URL and "
        "MACHINE_TOKEN, and that its token has not been revoked."
    ),
    "stats_sync": (
        "Scraping impressions/views failed. Posting is unaffected. If the error "
        "is login_expired, run `uv run cl init-account <name>` on the desktop."
    ),
    "ghost_check": (
        "The visibility check failed. Posting is unaffected, but you are flying "
        "blind on whether recent ads are ghosted."
    ),
    "edit_worker": "The edit worker raised. Live postings are untouched.",
    "edit_hydrate": (
        "Reading a live posting's edit form failed, so the dashboard cannot show "
        "you its current content. Nothing on Craigslist was changed."
    ),
    "post_edit": (
        "Applying an edit to a live posting failed. Check the post's Edits entry "
        "for whether it was left degraded."
    ),
}

_ERROR_TYPE_HELP: dict[str, str] = {
    "degraded_post": (
        "The ad published, so it still counts against the cooldowns — but "
        "something about it is wrong. Read the warnings: missing photos, a "
        "guessed county, or a post URL that will not resolve later."
    ),
    "QueueUnavailable": (
        "The server was unreachable or rejected the machine's token. Nothing "
        "posted. This is safe but total — it will not retry until the next slot."
    ),
    "login_expired": (
        "Craigslist logged this account out. Run `uv run cl init-account <name>` "
        "on the desktop and log in manually."
    ),
    "TimeoutError": (
        "A selector never appeared. Usually means Craigslist changed the page. "
        "The HTML artifact is the fastest way to find the new selector."
    ),
    "image_download_failed": (
        "Images could not be fetched from this server. For an edit this is a "
        "refusal to proceed, which is correct — a partial set would delete "
        "images from a live posting."
    ),
}

_STEP_HELP: dict[str, str] = {
    "stale_claim": (
        "A machine took this draft and never reported back — it was killed "
        "mid-run, or rebooted. It is parked rather than requeued because we "
        "cannot tell whether it published. Check the account on Craigslist "
        "before requeueing, or the same ad may go out twice."
    ),
    "build_ad": (
        "The draft could not be turned into a postable ad. The draft itself is "
        "malformed; it was returned to the queue and will fail again until "
        "edited."
    ),
    "launch": (
        "Chrome never started. Usually the browser lease is held by another "
        "flow, the profile directory is locked (OneDrive does this), or "
        "patchright is not installed. Nothing reached Craigslist."
    ),
    "login_check": (
        "The account is logged out. Run `uv run cl init-account <name>` on the "
        "desktop."
    ),
    "photo_upload": (
        "Failed while uploading images, so some images were consumed. The draft "
        "is parked rather than requeued for exactly this reason."
    ),
    "billing": (
        "Craigslist's paid-category checkout did not complete. The ad may or may "
        "not have published — check the account before requeueing. If the error "
        "says no payment method, add a card to that Craigslist account: this "
        "will not fix itself, and the account is now backed off for "
        "billing_failure_backoff_minutes so it stops consuming posting slots."
    ),
    "confirmation": (
        "The form submitted but the confirmation page gave us no post URL. The "
        "ad has probably published; it just is not linked here."
    ),
}


def _explain(*, flow: str | None, error_type: str | None, step: str | None) -> str:
    """Most specific explanation available. Step beats error type beats flow."""
    for table, key in ((_STEP_HELP, step), (_ERROR_TYPE_HELP, error_type), (_FLOW_HELP, flow)):
        if key and key in table:
            return table[key]
    return "No guidance recorded for this failure. The artifacts, if any, are the place to start."


# Where in the dashboard this problem gets fixed.
_WHERE: dict[str, str] = {
    "post": "/posts",
    "post_edit": "/edits",
    "edit_hydrate": "/edits",
    "edit_worker": "/edits",
    "queue_sync": "/settings",
    "stale_claim": "/review",
}


# ---------------------------------------------------------------------------
# The problem feed
# ---------------------------------------------------------------------------

def _flow_error_problems(
    conn: psycopg.Connection, *, since: datetime, include_acknowledged: bool, limit: int
) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT event_id, ts, machine, flow, step, account, error_type,
               error_message, context, acknowledged_at
        FROM flow_errors
        WHERE ts >= %s
          {"" if include_acknowledged else "AND acknowledged_at IS NULL"}
        ORDER BY ts DESC
        LIMIT %s
        """,
        (since, limit),
    ).fetchall()

    out = []
    for r in rows:
        # A degraded post means a live ad is wrong right now; an unreachable
        # queue means nothing posts at all. Both outrank a failed stats scrape.
        critical = r["error_type"] == "degraded_post" or r["flow"] == "queue_sync"
        out.append({
            "id": r["event_id"],
            "kind": "flow_error",
            "severity": "critical" if critical else "warning",
            "ts": r["ts"],
            "machine": r["machine"],
            "account": r["account"],
            "flow": r["flow"],
            "step": r["step"],
            "title": _title(r["flow"], r["error_type"], r["step"]),
            "detail": r["error_message"],
            "explanation": _explain(
                flow=r["flow"], error_type=r["error_type"], step=r["step"]
            ),
            "where": _WHERE.get(r["step"] or "") or _WHERE.get(r["flow"] or ""),
            "context": r["context"] or {},
            "artifact_ids": (r["context"] or {}).get("artifact_ids") or [],
            "acknowledged_at": r["acknowledged_at"],
        })
    return out


def _title(flow: str | None, error_type: str | None, step: str | None) -> str:
    if error_type == "degraded_post":
        return "Post published in a degraded state"
    bits = [flow or "unknown flow"]
    if step:
        bits.append(f"at {step}")
    if error_type and error_type != "error":
        bits.append(f"({error_type})")
    return " ".join(bits)


def _failed_post_problems(
    conn: psycopg.Connection, *, since: datetime, limit: int
) -> list[dict]:
    """Failed posting runs.

    Kept separate from flow_errors on purpose: a failed post carries a step that
    decides the draft's fate and page dumps that explain it, and folding it into
    the generic error shape would lose both.
    """
    rows = conn.execute(
        """
        SELECT event_id, ts, machine, account, outcome, error_type, error_message,
               failed_step, draft_id, ad_title, artifact_ids
        FROM post_attempts
        WHERE ts >= %s AND outcome LIKE 'failed%%'
        ORDER BY ts DESC
        LIMIT %s
        """,
        (since, limit),
    ).fetchall()

    out = []
    for r in rows:
        # A login failure blocks every future post for that account; a form
        # failure is usually one bad run.
        critical = r["outcome"] == "failed_login"
        out.append({
            "id": r["event_id"],
            "kind": "post_failure",
            "severity": "critical" if critical else "warning",
            "ts": r["ts"],
            "machine": r["machine"],
            "account": r["account"],
            "flow": "post",
            "step": r["failed_step"],
            "title": f"Post failed at {r['failed_step'] or 'an unknown step'}",
            "detail": r["error_message"],
            "explanation": _explain(
                flow="post", error_type=r["error_type"], step=r["failed_step"]
            ),
            "where": "/review",
            "context": {"draft_id": r["draft_id"], "ad_title": r["ad_title"]},
            "artifact_ids": r["artifact_ids"] or [],
            "acknowledged_at": None,
        })
    return out


def _silent_machine_problems(conn: psycopg.Connection) -> list[dict]:
    """Machines that have stopped calling home.

    This is the gap nothing else covers: every other signal here needs the
    desktop to report something. When the desktop is off, the Scheduled Task is
    disabled, or the reporter daemon has died, the dashboard just shows an
    ageing "last post" and no error at all — the most expensive silence in the
    system, and previously the only one nobody was watching for.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (machine) machine, ts
        FROM account_states
        ORDER BY machine, ts DESC
        """
    ).fetchall()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MACHINE_SILENT_MINUTES)
    out = []
    for r in rows:
        if r["ts"] >= cutoff:
            continue
        minutes = int((datetime.now(timezone.utc) - r["ts"]).total_seconds() / 60)
        out.append({
            "id": f"machine_silent:{r['machine']}",
            "kind": "machine_silent",
            "severity": "critical",
            "ts": r["ts"],
            "machine": r["machine"],
            "account": None,
            "flow": "heartbeat",
            "step": None,
            "title": f"Machine {r['machine']} has not reported in {minutes} minutes",
            "detail": (
                f"Last heartbeat {r['ts']:%Y-%m-%d %H:%M} UTC. Nothing will post "
                f"from this machine while it is silent, and no error will be "
                f"raised either — the desktop is what reports errors."
            ),
            "explanation": (
                "The reporter daemon is not running, the machine is off, or it "
                "cannot reach this server. Check that the machine is on and "
                "logged in (the browser needs a desktop session), then that the "
                "reporter daemon service is running."
            ),
            "where": "/settings",
            "context": {"last_seen": r["ts"].isoformat(), "silent_minutes": minutes},
            "artifact_ids": [],
            "acknowledged_at": None,
        })
    return out


def _stuck_claim_problems(conn: psycopg.Connection) -> list[dict]:
    """Drafts held by a claim longer than a posting run can possibly take.

    The reaper parks these at the next claim, but the next claim may be hours
    away and this is worth seeing immediately.
    """
    from . import queue as queue_svc

    out = []
    for d in queue_svc.stuck_claims(conn):
        held = d["held_minutes"] or 0
        if held < queue_svc.STALE_CLAIM_MINUTES:
            continue  # a post in progress, which is normal
        out.append({
            "id": f"stuck_claim:{d['id']}",
            "kind": "stuck_claim",
            "severity": "warning",
            "ts": d["claimed_at"],
            "machine": d["claimed_by_machine"],
            "account": d["account"],
            "flow": "post",
            "step": "stale_claim",
            "title": f"Draft {d['id']} held by a claim for {int(held)} minutes",
            "detail": d["title"],
            "explanation": _STEP_HELP["stale_claim"],
            "where": "/review",
            "context": {"draft_id": d["id"], "attempts": d["attempts"]},
            "artifact_ids": [],
            "acknowledged_at": None,
        })
    return out


def _image_stack_problems(conn: psycopg.Connection) -> list[dict]:
    """The photo and cover stacks against what the queue actually needs.

    Not a failure — with refill on manual this is the ordinary state, and it
    resolves itself by you generating more. It belongs here anyway because
    nothing else says it out loud: a short stack produces thin posts silently,
    and an empty cover stack means the claim-time backstop has nothing to reach
    for and a roof photo becomes the thumbnail.
    """
    from . import images as images_svc

    h = images_svc.stack_health(conn)
    now = datetime.now(timezone.utc)
    out = []

    if h["photos_short"] > 0:
        out.append({
            "id": "image_stack:photos_short",
            "kind": "image_stack",
            "severity": "warning",
            "ts": now,
            "machine": None,
            "account": None,
            "flow": "images",
            "step": "stack_depth",
            "title": (
                f"Photo stack short by {h['photos_short']} — "
                f"{h['queued_drafts']} queued draft(s) want {h['photo_demand']}, "
                f"{h['photos_available']} available"
            ),
            "detail": None,
            "explanation": (
                "Drafts take whatever the stack can give and publish thinner "
                "rather than waiting, so nothing is blocked. Generate or upload "
                "more photos on the Images page, or turn on the background "
                "refill under Settings once the image prompts are settled."
            ),
            "where": "/images",
            "context": h,
            "artifact_ids": [],
            "acknowledged_at": None,
        })

    if h["covers_available"] == 0:
        out.append({
            "id": "image_stack:no_covers",
            "kind": "image_stack",
            "severity": "critical",
            "ts": now,
            "machine": None,
            "account": None,
            "flow": "images",
            "step": "stack_depth",
            "title": "Cover stack is empty",
            "detail": None,
            "explanation": (
                "Covers are chosen by hand, and the claim-time backstop needs "
                "one to fall back on. With none left, any draft you have not "
                "given a cover publishes with an ordinary roof photo as its "
                "Craigslist thumbnail — the highest-leverage visual on the ad, "
                "spent on a picture nobody picked."
            ),
            "where": "/images",
            "context": h,
            "artifact_ids": [],
            "acknowledged_at": None,
        })
    return out


_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def problems(
    conn: psycopg.Connection,
    *,
    hours: int = DEFAULT_WINDOW_HOURS,
    include_acknowledged: bool = False,
    limit: int = 200,
) -> dict:
    """The whole feed, most severe first, then most recent."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    items = [
        *_flow_error_problems(
            conn, since=since, include_acknowledged=include_acknowledged, limit=limit
        ),
        *_failed_post_problems(conn, since=since, limit=limit),
        *_silent_machine_problems(conn),
        *_stuck_claim_problems(conn),
        *_image_stack_problems(conn),
    ]
    items.sort(key=lambda p: (_SEVERITY_ORDER.get(p["severity"], 9), -p["ts"].timestamp()))

    counts = {"critical": 0, "warning": 0, "info": 0}
    for p in items:
        counts[p["severity"]] = counts.get(p["severity"], 0) + 1

    return {
        "window_hours": hours,
        "counts": counts,
        "total": len(items),
        "problems": items[:limit],
    }


def summary(conn: psycopg.Connection) -> dict:
    """Counts only — cheap enough for the header pill to poll."""
    report = problems(conn, limit=500)
    return {"counts": report["counts"], "total": report["total"]}


def acknowledge(conn: psycopg.Connection, event_ids: list[str]) -> int:
    """Mark flow errors as seen.

    Only flow_errors can be acknowledged. The other three kinds are derived from
    live state — a stuck claim stops being a problem when the draft moves, a
    silent machine when it reports — so acknowledging them would just hide a
    condition that is still true.
    """
    if not event_ids:
        return 0
    cur = conn.execute(
        "UPDATE flow_errors SET acknowledged_at = NOW() "
        "WHERE event_id = ANY(%s) AND acknowledged_at IS NULL",
        (event_ids,),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Per-post history — the posting equivalent of /edits/{post_id}/attempts
# ---------------------------------------------------------------------------

def attempts_for_post(conn: psycopg.Connection, post_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_id, ts, machine, account, outcome, duration_seconds,
               failed_step, error_type, error_message, warnings,
               photos_attached, photos_confirmed, cover_photo, artifact_ids,
               draft_id, ad_title
        FROM post_attempts
        WHERE post_id = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (post_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def attempts_for_draft(conn: psycopg.Connection, draft_id: int, limit: int = 20) -> list[dict]:
    """Every run against one draft.

    A draft can be claimed, fail, requeue and be claimed again, so "why is this
    draft not going out" is a question about the sequence, not the last row.
    """
    rows = conn.execute(
        """
        SELECT event_id, ts, machine, account, outcome, duration_seconds,
               failed_step, error_type, error_message, warnings,
               photos_confirmed, artifact_ids, post_id, post_url
        FROM post_attempts
        WHERE draft_id = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (draft_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
