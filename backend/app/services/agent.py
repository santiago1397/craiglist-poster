"""The agent read surface: composed answers, and the prose that carries them.

Three decisions are baked in here, and the code is shaped by all three.

**Composed, not mirrored.** The interesting facts in this system are spread
across routers that mirror the dashboard's pages, not an agent's questions.
"Can craigs2 post right now, and if not why" currently means joining
`dashboard_accounts`, `evaluate_eligibility` and `get_guardrails`. Handing an
agent the ingredients means every caller re-derives the answer, and a weak model
re-derives it wrong — reporting "queue empty" as the reason when the real block
is a 20-hour cooldown. The server already knows; it should say so.

**Prose by default, JSON on request.** The consumer is a language model. Once
the joining is done server-side the result is a conclusion, not a dataset, and
English is the format that survives being relayed. The safety argument matters
more than the ergonomic one: a caveat lives inside a sentence and gets repeated,
while a `"caveat"` field gets dropped the moment a model summarises. Every
number below that comes with a condition — the schedule being approximate, the
stats being a daily scrape that may be stale — has that condition welded into
the sentence rather than sitting in a sibling key.

**Every report states its own shape.** Counts always say "showing N of M".
Silent truncation reads as completeness, and an agent that believes it has seen
everything will confidently report that nothing is wrong.

`render_*` takes exactly what its `*_report` returns, so `?format=json` and the
default text can never describe different states.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from ..config import get_settings
from . import diagnostics as diagnostics_svc
from . import drafts as drafts_svc
from . import images as images_svc
from . import queries
from . import queue as queue_svc

# A machine that has not reported in this long is not "quiet", it is a problem:
# every other failure signal in the system needs the desktop to send it.
SILENT_MACHINE_HOURS = 6

# Snapshots come from a daily 06:00 scrape on the posting desktop. Past this
# many days the counters stop describing the present.
STALE_STATS_DAYS = 2


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _tz():
    return get_settings().display_zoneinfo


def _dt(value: datetime | None, *, with_date: bool = True) -> str:
    """A timestamp a model can repeat without converting anything.

    Always rendered in the display timezone with the zone named. UTC in, local
    out — an agent asked "when is the next post" should never be the thing that
    has to know the server stores UTC.
    """
    if value is None:
        return "never"
    local = value.astimezone(_tz())
    fmt = "%Y-%m-%d %H:%M" if with_date else "%H:%M"
    return f"{local.strftime(fmt)} {local.tzname()}"


def _ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "never"
    delta = now - value
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _bullets(lines: list[str], indent: str = "  ") -> str:
    return "\n".join(f"{indent}- {line}" for line in lines)


def _showing(shown: int, total: int, hint: str) -> str:
    """Never let a truncated list read as a complete one."""
    if shown >= total:
        return f"Showing all {total}."
    return f"Showing {shown} of {total}. {hint}"


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def _account_names(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT account FROM (
            SELECT account FROM posts
            UNION SELECT account FROM post_attempts
            UNION SELECT account FROM account_states
            UNION SELECT account FROM drafts
        ) sub
        WHERE account <> '(none)'
        ORDER BY account
        """
    ).fetchall()
    return [r["account"] for r in rows]


def known_accounts(conn: psycopg.Connection) -> list[str]:
    """Public name for the account list, for callers outside this module.

    The compose surface validates a draft's account against it — a typo would
    otherwise create a draft in an account no machine is bound to, which sits in
    the queue looking healthy and never posts.
    """
    return _account_names(conn)


def _machines(conn: psycopg.Connection, now: datetime) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (machine) machine, ts
        FROM account_states
        WHERE machine IS NOT NULL AND machine <> ''
        ORDER BY machine, ts DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        silent_for = (now - r["ts"]).total_seconds() / 3600
        out.append({
            "machine": r["machine"],
            "last_seen_at": r["ts"],
            "silent": silent_for >= SILENT_MACHINE_HOURS,
            "silent_hours": round(silent_for, 1),
        })
    return out


def _problem_pointer(conn: psycopg.Connection) -> dict:
    """Counts only, so every report can end by pointing at what is broken.

    `/agent/status` carries this because the failure that matters most — a
    desktop that has stopped reporting — is precisely the one an agent would
    never think to go looking for. A status report that can say "everything is
    fine" without consulting it would be lying by omission.
    """
    summary = diagnostics_svc.summary(conn)
    return {"counts": summary["counts"], "total": summary["total"]}


def _pointer_line(pointer: dict) -> str:
    total = pointer["total"]
    if not total:
        return "No open problems."
    counts = pointer["counts"]
    critical = counts.get("critical", 0)
    bits = f"{total} open {_plural(total, 'problem')}"
    if critical:
        bits += f" ({critical} CRITICAL)"
    return f"{bits} — call /agent/problems for detail."


# ---------------------------------------------------------------------------
# /agent/status
# ---------------------------------------------------------------------------

def status_report(conn: psycopg.Connection, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    accounts = _account_names(conn)
    guardrails = queue_svc.get_guardrails(conn)
    eligibility = queue_svc.evaluate_eligibility(conn, accounts, now=now)

    schedule = queue_svc.project_schedule(
        conn, accounts=accounts, horizon_days=14, now=now
    )
    next_by_account: dict[str, dict] = {}
    for item in schedule:
        next_by_account.setdefault(item["account"], item)

    reviewed = {
        r["account"]: r["n"]
        for r in conn.execute(
            "SELECT account, COUNT(*) AS n FROM drafts "
            "WHERE status = 'queued' AND reviewed = TRUE GROUP BY account"
        ).fetchall()
    }

    per_account = []
    for name in accounts:
        info = eligibility["accounts"][name]
        nxt = next_by_account.get(name)
        per_account.append({
            "account": name,
            "eligible_now": info["eligible"],
            "blocked_by": info["reasons"],
            "last_post_at": info["last_post_at"],
            "posts_last_7d": info["posts_last_7d"],
            "queue_depth": info["queue_depth"],
            "reviewed_depth": reviewed.get(name, 0),
            "next_post_at": nxt["at"] if nxt else None,
            "next_post_title": nxt["title"] if nxt else None,
            "next_post_draft_id": nxt["draft_id"] if nxt else None,
        })

    return {
        "now": now,
        "posting_enabled": guardrails.get("posting_enabled", True),
        "paused_reason": guardrails.get("paused_reason"),
        "posts_last_24h_total": eligibility["posts_last_24h_total"],
        "max_posts_per_day_total": guardrails["max_posts_per_day_total"],
        "global_blocks": eligibility["global_blocks"],
        "accounts": per_account,
        "next_post": schedule[0] if schedule else None,
        "machines": _machines(conn, now),
        "problems": _problem_pointer(conn),
    }


def render_status(d: dict) -> str:
    lines = [
        "STATUS — Craigslist auto-poster",
        f"As of {_dt(d['now'])}.",
        "",
    ]

    if not d["posting_enabled"]:
        reason = (d.get("paused_reason") or "").strip()
        lines.append(
            "POSTING IS PAUSED from the dashboard"
            + (f": {reason}" if reason else ".")
        )
        lines.append("Nothing will publish until it is re-enabled in Settings.")
        lines.append("")

    lines.append(
        f"Posts in the last 24h: {d['posts_last_24h_total']} of "
        f"{d['max_posts_per_day_total']} allowed."
    )

    if d["next_post"]:
        nxt = d["next_post"]
        lines.append(
            f"Next post: {nxt['account']} at {_dt(nxt['at'])} — "
            f"\"{nxt['title']}\" (draft {nxt['draft_id']})."
        )
        lines.append(
            "  That time is a forecast, not a promise: it replays the scheduled "
            "fires against today's guardrails, and a pause, a failed post or an "
            "edit moves it."
        )
    else:
        lines.append(
            "Next post: none forecast. Either every queue is empty or posting is "
            "paused — the per-account lines below say which."
        )

    if d["global_blocks"]:
        lines += ["", "Blocking every account right now:", _bullets(d["global_blocks"])]

    lines += ["", "Accounts:"]
    for a in d["accounts"]:
        verdict = "CAN POST NOW" if a["eligible_now"] else "cannot post now"
        lines.append(f"  {a['account']} — {verdict}")
        if a["blocked_by"]:
            lines.append(f"    Blocked by: {'; '.join(a['blocked_by'])}")
        lines.append(
            f"    Queue: {a['queue_depth']} queued, {a['reviewed_depth']} reviewed. "
            f"Last post: {_dt(a['last_post_at'])}. "
            f"{a['posts_last_7d']} in the last 7 days."
        )
        if a["next_post_at"]:
            lines.append(
                f"    Next: {_dt(a['next_post_at'])} — \"{a['next_post_title']}\" "
                f"(draft {a['next_post_draft_id']}, approximate)."
            )

    if d["machines"]:
        lines += ["", "Machines:"]
        for m in d["machines"]:
            flag = " — SILENT, nothing can post or report from it" if m["silent"] else ""
            lines.append(
                f"  {m['machine']} — last reported {_dt(m['last_seen_at'])}{flag}"
            )

    lines += ["", _pointer_line(d["problems"])]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /agent/queue
# ---------------------------------------------------------------------------

def queue_report(
    conn: psycopg.Connection,
    *,
    account: str | None = None,
    reviewed: bool | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    page = drafts_svc.list_drafts(
        conn, status="queued", account=account, reviewed=reviewed, limit=limit
    )

    accounts = [account] if account else _account_names(conn)
    schedule = queue_svc.project_schedule(
        conn, accounts=accounts, horizon_days=21, now=now
    )
    when = {s["draft_id"]: s["at"] for s in schedule}

    ids = [d["id"] for d in page["drafts"]]
    image_counts: dict[int, dict] = {}
    if ids:
        rows = conn.execute(
            """
            SELECT draft_id,
                   COUNT(*) FILTER (WHERE slot = 1) AS cover,
                   COUNT(*) FILTER (WHERE slot > 1) AS photos
            FROM draft_images WHERE draft_id = ANY(%s) GROUP BY draft_id
            """,
            (ids,),
        ).fetchall()
        image_counts = {r["draft_id"]: dict(r) for r in rows}

    items = []
    for draft in page["drafts"]:
        counts = image_counts.get(draft["id"], {"cover": 0, "photos": 0})
        missing = []
        if not counts["cover"]:
            missing.append("no cover image (slot 1 — the Craigslist thumbnail)")
        if not counts["photos"]:
            missing.append("no photos attached")
        items.append({
            "draft_id": draft["id"],
            "account": draft["account"],
            "title": draft["title"],
            "city": draft.get("city"),
            "reviewed": draft["reviewed"],
            "cover_images": counts["cover"],
            "photo_images": counts["photos"],
            "missing": missing,
            "projected_post_at": when.get(draft["id"]),
            "post_requested_at": draft.get("post_requested_at"),
        })

    return {
        "now": now,
        "total": page["total"],
        "shown": len(items),
        "filter": {"account": account, "reviewed": reviewed},
        "drafts": items,
    }


def render_queue(d: dict) -> str:
    lines = ["QUEUE — drafts waiting to publish", f"As of {_dt(d['now'])}.", ""]
    if not d["drafts"]:
        lines.append(
            "Nothing queued matching that filter. With an empty queue nothing "
            "posts — the poster never invents an ad locally."
        )
        return "\n".join(lines)

    lines.append(_showing(d["shown"], d["total"], "Narrow with ?account= or ?reviewed=true."))
    lines.append("")
    for item in d["drafts"]:
        mark = "reviewed" if item["reviewed"] else "NOT REVIEWED"
        lines.append(f"  Draft {item['draft_id']} — {item['account']} — {mark}")
        lines.append(f"    \"{item['title']}\"" + (f" ({item['city']})" if item["city"] else ""))
        lines.append(
            f"    Images: {item['cover_images']} cover, {item['photo_images']} photos."
        )
        for miss in item["missing"]:
            lines.append(f"    Missing: {miss}")
        if item["post_requested_at"]:
            lines.append(
                f"    A post has already been requested for this draft "
                f"({_dt(item['post_requested_at'])}) and is waiting for the desktop."
            )
        if item["projected_post_at"]:
            lines.append(f"    Projected: {_dt(item['projected_post_at'])} (approximate).")
        else:
            lines.append("    Projected: not within the next 21 days.")
    lines += [
        "",
        "Only drafts marked reviewed can be published through /agent/post-now.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /agent/posts
# ---------------------------------------------------------------------------

def _since_to_date(since: str | None) -> tuple[str | None, str]:
    """Turn an agent-friendly window into what `posts_page` actually takes.

    `queries.posts_page` expects a date or the literal 'all' — it interpolates
    the value straight into `posted_ts >= %s`. Passing it '30d' is a 500, and
    '30d' is exactly what a model will send when the parameter is described as
    a time window. Translating here keeps the agent-facing vocabulary friendly
    without loosening the query underneath.

    Returns (value_for_query, label_for_prose).
    """
    if not since or since == "all":
        return ("all", "all time")
    text = since.strip().lower()
    if text.endswith("d") and text[:-1].isdigit():
        days = max(1, min(3650, int(text[:-1])))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        return (cutoff.isoformat(), f"{days} days")
    # An explicit date passes through; anything else falls back rather than
    # failing, because a rejected report teaches an agent nothing.
    try:
        return (date.fromisoformat(text).isoformat(), f"since {text}")
    except ValueError:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        return (cutoff.isoformat(), "30 days")


def posts_report(
    conn: psycopg.Connection,
    *,
    account: str | None = None,
    since: str | None = "30d",
    limit: int = 20,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    since_value, since_label = _since_to_date(since)
    page = queries.posts_page(
        conn, account=account, since=since_value, limit=limit, sort="posted_ts"
    )
    items = [{
        "post_id": r["post_id"],
        "account": r["account"],
        "title": r["title"],
        "url": r["url"],
        "posted_at": r["posted_ts"],
        "liveness": r["liveness"],
        "ghosted": r["ghosted"],
        "views": r["views"],
        "impressions": r["impressions"],
        "edit_status": r["edit_status"],
        "has_pending_edit": r["has_pending_edit"],
    } for r in page["items"]]

    return {
        "now": now,
        "total": page["total"],
        "shown": len(items),
        "window": since_label,
        "filter": {"account": account},
        "counts": page["counts"],
        "edit_counts": page["edit_counts"],
        "sync": page["sync"],
        "posts": items,
    }


def render_posts(d: dict) -> str:
    counts = d["counts"]
    lines = [
        "POSTS — ads that have published",
        f"As of {_dt(d['now'])}. Window: {d['window']}.",
        "",
        f"{counts.get('live', 0)} live, {counts.get('ended', 0)} ended, "
        f"{counts.get('unknown', 0)} unknown.",
    ]

    edits = d["edit_counts"]
    if edits.get("degraded"):
        lines.append(
            f"{edits['degraded']} post(s) DEGRADED — a live listing is in a worse "
            "state than before an edit and needs a human."
        )
    if edits.get("parked"):
        lines.append(
            f"{edits['parked']} edit(s) parked — the live post moved underneath a "
            "staged change, so it was not applied."
        )
    if edits.get("pending"):
        lines.append(f"{edits['pending']} post(s) have an edit staged and not yet applied.")

    lines += ["", _showing(d["shown"], d["total"], "Narrow with ?account= or ?since=."), ""]
    for p in d["posts"]:
        flags = []
        if p["ghosted"]:
            flags.append("GHOSTED — not visible in public search")
        if p["edit_status"] == "degraded_live":
            flags.append("DEGRADED")
        if p["has_pending_edit"]:
            flags.append("edit staged")
        suffix = f" [{'; '.join(flags)}]" if flags else ""
        lines.append(f"  {p['post_id']} — {p['account']} — {p['liveness']}{suffix}")
        lines.append(f"    \"{p['title']}\" posted {_dt(p['posted_at'])}")
        views = "no counters yet" if p["views"] is None else f"{p['views']} views"
        impressions = "" if p["impressions"] is None else f", {p['impressions']} impressions"
        lines.append(f"    {views}{impressions} (lifetime totals — see /agent/stats for a period)")
        if p["url"]:
            lines.append(f"    {p['url']}")

    lines += ["", _sync_caveat(d["sync"])]
    return "\n".join(lines)


def _sync_caveat(sync: dict) -> str:
    """Liveness is only as current as the last scrape. Say so, every time."""
    stale = sync.get("stale")
    # `sync_freshness` reports per account; the caveat needs one date, and the
    # newest is the most generous reading — if even that is stale, all of it is.
    dates = [
        a["last_sync_date"] for a in sync.get("accounts", []) if a["last_sync_date"]
    ]
    as_of = max(dates) if dates else "never"
    if stale:
        return (
            f"WARNING: the last stats scrape was {as_of}. Liveness and counters "
            "above are stale — a post shown as live may have ended. The scrape "
            "runs daily at 06:00 on the posting desktop; if that machine is off, "
            "nothing updates."
        )
    return (
        f"Counters and liveness are as of the {as_of} scrape (daily, 06:00 ET on "
        "the posting desktop). Today's activity is not included."
    )


# ---------------------------------------------------------------------------
# /agent/stats
# ---------------------------------------------------------------------------

_WINDOWS = {"yesterday": 1, "7d": 7, "30d": 30}


def stats_report(
    conn: psycopg.Connection,
    *,
    window: str = "7d",
    account: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> dict:
    """Views and impressions *accrued in a period*, not lifetime averages.

    Craigslist's counters are cumulative, and the snapshot table stores them
    verbatim — one row per post per day. So a period figure is a difference
    between two snapshots, and there was no server-side code computing one.

    This matters more than it sounds. `queries.posts_page` exposes
    `views_per_day`, which is `total_views / days_active` — a *lifetime
    average*. Handing an agent a field with that name and letting it answer
    "how many views yesterday" produces a confident, wrong number on any post
    that spiked on day one and has been flat since. The fix is to compute the
    real delta, and to never surface the average here at all.

    `partial_window` marks posts younger than the window, whose delta is
    everything they ever earned rather than a period figure.
    """
    now = now or datetime.now(timezone.utc)
    days = _WINDOWS.get(window, 7)

    # The window is anchored to each post's newest snapshot, NOT to CURRENT_DATE.
    #
    # `snapshot_date` is written in America/New_York by the desktop scraper,
    # while CURRENT_DATE is evaluated in the database's timezone (UTC in
    # production). Between 20:00 and midnight Eastern those disagree by a day,
    # so `snapshot_date <= CURRENT_DATE - 1` would select the *same* row as the
    # latest snapshot and every post would report a delta of exactly zero —
    # every evening, silently, with no error anywhere. Comparing the data
    # against itself removes the clock from the question entirely.
    rows = conn.execute(
        """
        WITH late AS (
            SELECT DISTINCT ON (post_id)
                   post_id, snapshot_date, status, views, impressions
            FROM snapshots
            ORDER BY post_id, snapshot_date DESC
        ),
        early AS (
            SELECT DISTINCT ON (s.post_id)
                   s.post_id, s.snapshot_date, s.views, s.impressions
            FROM snapshots s
            JOIN late l ON l.post_id = s.post_id
            WHERE s.snapshot_date <= l.snapshot_date - %(days)s::int
            ORDER BY s.post_id, s.snapshot_date DESC
        )
        SELECT p.post_id, p.account, p.title, p.url, p.posted_ts,
               l.snapshot_date AS as_of,
               l.status,
               l.views  AS total_views,
               l.impressions AS total_impressions,
               COALESCE(l.views, 0) - COALESCE(e.views, 0) AS d_views,
               COALESCE(l.impressions, 0) - COALESCE(e.impressions, 0) AS d_impressions,
               (e.post_id IS NULL) AS partial_window
        FROM posts p
        JOIN late l ON l.post_id = p.post_id
        LEFT JOIN early e ON e.post_id = p.post_id
        WHERE (%(account)s::text IS NULL OR p.account = %(account)s)
        ORDER BY d_views DESC, l.snapshot_date DESC
        """,
        {"days": days, "account": account},
    ).fetchall()

    items = [dict(r) for r in rows]
    totals = {
        "views": sum(i["d_views"] or 0 for i in items),
        "impressions": sum(i["d_impressions"] or 0 for i in items),
        "posts": len(items),
    }
    by_account: dict[str, dict] = {}
    for i in items:
        acct = by_account.setdefault(
            i["account"], {"views": 0, "impressions": 0, "posts": 0}
        )
        acct["views"] += i["d_views"] or 0
        acct["impressions"] += i["d_impressions"] or 0
        acct["posts"] += 1

    as_of: date | None = max((i["as_of"] for i in items), default=None)
    stale = bool(as_of and (datetime.now(_tz()).date() - as_of).days > STALE_STATS_DAYS)

    return {
        "now": now,
        "window": window if window in _WINDOWS else "7d",
        "window_days": days,
        "as_of": as_of,
        "stale": stale,
        "totals": totals,
        "by_account": by_account,
        "shown": min(limit, len(items)),
        "total": len(items),
        "posts": items[:limit],
    }


def render_stats(d: dict) -> str:
    label = {
        "yesterday": "the last scraped day",
        "7d": "the last 7 days",
        "30d": "the last 30 days",
    }[d["window"]]

    lines = [
        f"STATS — performance over {label}",
        f"As of {_dt(d['now'])}.",
        "",
    ]

    if d["as_of"] is None:
        lines.append(
            "No snapshots at all. Nothing has been scraped yet, so there are no "
            "counters to report. The scrape runs daily at 06:00 ET on the posting "
            "desktop and needs that machine to be on and logged in."
        )
        return "\n".join(lines)

    lines.append(
        f"Impressions = times the ad appeared in a results page. "
        f"Views = clicks into the full posting."
    )
    lines.append(
        f"Totals across {d['totals']['posts']} {_plural(d['totals']['posts'], 'post')}: "
        f"{d['totals']['views']} views, {d['totals']['impressions']} impressions."
    )
    lines.append("")
    lines.append("By account:")
    for name, agg in sorted(d["by_account"].items()):
        lines.append(
            f"  {name} — {agg['views']} views, {agg['impressions']} impressions "
            f"across {agg['posts']} {_plural(agg['posts'], 'post')}"
        )

    lines += ["", _showing(d["shown"], d["total"], "Narrow with ?account=."), ""]
    for p in d["posts"]:
        note = " (post is younger than the window — this is its whole life)" \
            if p["partial_window"] else ""
        lines.append(f"  {p['post_id']} — {p['account']}")
        lines.append(f"    \"{p['title']}\"")
        lines.append(
            f"    {p['d_views']} views, {p['d_impressions']} impressions in "
            f"{label}{note}"
        )
        if p["total_views"] is None:
            lines.append(
                "    Lifetime: Craigslist has not released counters for this post "
                "yet (it withholds them for roughly the first day)."
            )
        else:
            lines.append(
                f"    Lifetime: {p['total_views']} views, "
                f"{p['total_impressions']} impressions."
            )

    lines += ["", _stats_caveat(d)]
    return "\n".join(lines)


def _stats_caveat(d: dict) -> str:
    base = (
        f"These are differences between daily snapshots, not live counters. The "
        f"most recent snapshot is from {d['as_of']}; the scrape runs once a day "
        f"at 06:00 ET on the posting desktop, so today is never complete and a "
        f"day the machine was off leaves a gap that shows up as a flat period."
    )
    if d["stale"]:
        return (
            "WARNING: " + base + " This data is stale — treat the numbers as a "
            "historical record, not the current state."
        )
    return base


# ---------------------------------------------------------------------------
# /agent/problems
# ---------------------------------------------------------------------------

def problems_report(
    conn: psycopg.Connection, *, hours: int = 72, limit: int = 30
) -> dict:
    report = diagnostics_svc.problems(conn, hours=hours, limit=200)
    items = report["problems"]
    return {
        "now": datetime.now(timezone.utc),
        "window_hours": hours,
        "counts": report["counts"],
        "total": report["total"],
        "shown": min(limit, len(items)),
        "problems": items[:limit],
    }


def render_problems(d: dict) -> str:
    lines = [
        "PROBLEMS — what is currently wrong",
        f"As of {_dt(d['now'])}. Window: last {d['window_hours']}h.",
        "",
    ]
    if not d["total"]:
        lines.append("Nothing is wrong. No open problems in this window.")
        return "\n".join(lines)

    counts = d["counts"]
    lines.append(
        f"{counts.get('critical', 0)} critical, {counts.get('warning', 0)} warning, "
        f"{counts.get('info', 0)} info."
    )
    lines.append(_showing(d["shown"], d["total"], "Widen or narrow with ?hours=."))
    lines.append("")

    for p in d["problems"]:
        lines.append(f"  [{p['severity'].upper()}] {p['title']}")
        lines.append(f"    When: {_dt(p['ts'])}")
        if p.get("account"):
            lines.append(f"    Account: {p['account']}")
        if p.get("machine"):
            lines.append(f"    Machine: {p['machine']}")
        if p.get("explanation"):
            lines.append(f"    What it means: {p['explanation']}")
        if p.get("detail"):
            lines.append(f"    Error: {p['detail']}")
        if p.get("where"):
            lines.append(f"    Fix it in: {p['where']}")
        if p.get("artifact_ids"):
            lines.append(
                f"    Captured evidence: {len(p['artifact_ids'])} artifact(s) — "
                "screenshots and page dumps, viewable in the dashboard."
            )
        lines.append("")

    lines.append(
        "This is the interpreted view. For raw error records as the desktop "
        "reported them, call /agent/logs."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /agent/logs
# ---------------------------------------------------------------------------

def logs_report(
    conn: psycopg.Connection,
    *,
    hours: int = 24,
    account: str | None = None,
    flow: str | None = None,
    limit: int = 40,
) -> dict:
    """Raw error records, uninterpreted.

    `/agent/problems` is the curated view: deduplicated, severity-ranked, with
    an explanation attached, and deliberately silent about conditions that have
    resolved. That is the right default and the wrong thing to debug with — it
    drops the third identical timeout that tells you a selector is broken
    rather than a page being slow.

    This returns what the desktop actually sent, newest first, from both places
    failures land: `flow_errors` (any background flow) and `post_attempts` with
    a non-posted outcome (a posting run that died, carrying the step it died
    on). Nothing is collapsed and nothing is hidden.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    flow_where = ["ts >= %(since)s"]
    params: dict[str, Any] = {"since": since, "limit": limit}
    if account:
        flow_where.append("account = %(account)s")
        params["account"] = account
    if flow:
        flow_where.append("flow = %(flow)s")
        params["flow"] = flow

    flow_rows = conn.execute(
        f"""
        SELECT event_id, ts, machine, flow, step, account, error_type,
               error_message, context
        FROM flow_errors
        WHERE {' AND '.join(flow_where)}
        ORDER BY ts DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()

    attempt_where = ["ts >= %(since)s", "outcome <> 'posted'"]
    attempt_params: dict[str, Any] = {"since": since, "limit": limit}
    if account:
        attempt_where.append("account = %(account)s")
        attempt_params["account"] = account
    attempt_rows = [] if flow and flow != "post" else conn.execute(
        f"""
        SELECT event_id, ts, machine, account, outcome, error_type, error_message,
               failed_step, post_id, ad_title
        FROM post_attempts
        WHERE {' AND '.join(attempt_where)}
        ORDER BY ts DESC
        LIMIT %(limit)s
        """,
        attempt_params,
    ).fetchall()

    entries = [{
        "source": "flow_error",
        "id": r["event_id"],
        "ts": r["ts"],
        "machine": r["machine"],
        "account": r["account"],
        "flow": r["flow"],
        "step": r["step"],
        "error_type": r["error_type"],
        "message": r["error_message"],
        "artifact_ids": (r["context"] or {}).get("artifact_ids") or [],
    } for r in flow_rows] + [{
        "source": "post_attempt",
        "id": r["event_id"],
        "ts": r["ts"],
        "machine": r["machine"],
        "account": r["account"],
        "flow": "post",
        "step": r["failed_step"],
        "error_type": r["error_type"],
        "message": r["error_message"],
        "outcome": r["outcome"],
        "post_id": r["post_id"],
        "title": r["ad_title"],
        "artifact_ids": [],
    } for r in attempt_rows]

    entries.sort(key=lambda e: e["ts"], reverse=True)
    return {
        "now": datetime.now(timezone.utc),
        "window_hours": hours,
        "filter": {"account": account, "flow": flow},
        "total": len(entries),
        "shown": min(limit, len(entries)),
        "entries": entries[:limit],
    }


def render_logs(d: dict) -> str:
    lines = [
        "LOGS — raw error records as reported by the desktop",
        f"As of {_dt(d['now'])}. Window: last {d['window_hours']}h.",
        "",
    ]
    if not d["total"]:
        lines += [
            "No errors recorded in this window.",
            "",
            "Note that silence here is not proof of health: every record in this "
            "table has to be sent by a posting machine. A machine that is off or "
            "has stopped reporting produces no errors and no work. Check the "
            "Machines section of /agent/status.",
        ]
        return "\n".join(lines)

    lines.append(_showing(d["shown"], d["total"], "Narrow with ?hours=, ?account= or ?flow=."))
    lines.append("")
    for e in d["entries"]:
        head = f"  {_dt(e['ts'])} — {e['flow']}"
        if e.get("step"):
            head += f" failed at step '{e['step']}'"
        lines.append(head)
        who = [b for b in (e.get("account"), e.get("machine")) if b]
        if who:
            lines.append(f"    {' on '.join(who)}")
        if e.get("outcome"):
            lines.append(f"    Outcome: {e['outcome']}")
        lines.append(f"    {e.get('error_type') or 'error'}: {e.get('message') or '(no message)'}")
        if e.get("post_id"):
            lines.append(f"    Post: {e['post_id']} \"{e.get('title') or ''}\"")
        if e.get("artifact_ids"):
            lines.append(
                f"    {len(e['artifact_ids'])} captured artifact(s) — screenshot and "
                "page dump of what Craigslist actually served, in the dashboard."
            )
        lines.append(f"    Record id: {e['id']}")
        lines.append("")

    lines.append(
        "These are unfiltered and undeduplicated on purpose — the same error "
        "three times means something different from once. For the ranked, "
        "explained view, call /agent/problems."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /agent/inventory
# ---------------------------------------------------------------------------

def inventory_report(conn: psycopg.Connection) -> dict:
    stats = images_svc.stats(conn)
    return {
        "now": datetime.now(timezone.utc),
        "buckets": stats["buckets"],
        "stack_health": stats["stack_health"],
        "spend_usd": stats["spend_usd"],
    }


def render_inventory(d: dict) -> str:
    health = d["stack_health"]
    lines = [
        "INVENTORY — the image stacks",
        f"As of {_dt(d['now'])}.",
        "",
        "Images live on the dashboard. Every post takes one cover (slot 1, the "
        "Craigslist thumbnail) and up to 23 photos (slots 2-24). The two stacks "
        "are separate and a cover cannot be used as a photo or vice versa.",
        "",
        f"Photos available: {health['photos_available']}.",
        f"Photos needed to fill the {health['queued_drafts']} queued "
        f"{_plural(health['queued_drafts'], 'draft')}: {health['photo_demand']}.",
    ]

    if health["photos_short"]:
        lines.append(
            f"SHORT BY {health['photos_short']} photos. Drafts will publish with "
            "fewer images rather than blocking — nothing breaks, the ads are just "
            "thinner. Generate or upload more on the Images page."
        )
    else:
        lines.append("The photo stack covers the current queue.")

    lines.append(f"Covers available: {health['covers_available']}.")
    if not health["covers_available"]:
        lines.append(
            "CRITICAL: the cover stack is empty. When a draft reaches posting "
            "without a cover, the first photo becomes the Craigslist thumbnail — "
            "which is a plain roof shot with no call to action."
        )

    # bucket_counts nests per kind: {"cover": {bucket: n}, "photo": {...}}.
    for kind, counts in sorted(d["buckets"].items()):
        lines += ["", f"{kind.capitalize()} stack:"]
        for bucket, n in counts.items():
            lines.append(f"  {bucket}: {n}")
    lines += [
        "",
        "Pending review images cannot be attached to anything until approved. "
        "Assigned means reserved by a queued draft or a staged edit. Published "
        "images are blocked for a 30-day cooldown because Craigslist detects "
        "reuse.",
    ]

    lines += [
        "",
        f"Lifetime image generation spend: ${d['spend_usd']:.2f}.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /agent/locations
# ---------------------------------------------------------------------------

def locations_report(conn: psycopg.Connection) -> dict:
    """Where an ad may be placed, and where the existing ones already are.

    Two things an agent composing a draft cannot work out for itself:

    The county list is closed. `poster._select_subarea()` picks the Craigslist
    subarea radio by substring-matching the county string, and an unrecognised
    value does not raise — it falls through to "pick the first radio" and the ad
    publishes under the wrong subarea with nothing anywhere reporting it. So the
    valid set has to be stated, and `subarea_supported` has to travel with it.

    "Somewhere we are not already posting" is a fact about current inventory,
    not a judgement. Reported here so the agent reads it instead of guessing —
    and so a city that has three live ads is visibly not a fresh location.
    """
    from ..reference import as_payload

    payload = as_payload(conn)

    rows = conn.execute(
        """
        -- `placements`, not `both`: BOTH is a reserved word in Postgres (it is
        -- part of TRIM's grammar) and fails as a bare table alias.
        SELECT county, city, COUNT(*) AS n FROM (
            SELECT county, city FROM drafts
            WHERE status IN ('queued', 'claimed', 'needs_attention')
            UNION ALL
            SELECT county, city FROM posts
            WHERE posted_ts IS NOT NULL AND posted_ts >= NOW() - INTERVAL '90 days'
        ) AS placements
        WHERE COALESCE(city, '') <> ''
        GROUP BY county, city
        """
    ).fetchall()
    in_use = {(r["county"] or "", r["city"] or ""): r["n"] for r in rows}

    counties: list[dict] = []
    for county in payload["counties"]:
        cities = [
            {
                "city": c["city"],
                "zip": c["zip"],
                "in_use": in_use.get((county["name"], c["city"]), 0),
            }
            for c in county["cities"]
        ]
        counties.append({
            "name": county["name"],
            "subarea_supported": county["subarea_supported"],
            "cities": cities,
            "unused_cities": [c["city"] for c in cities if not c["in_use"]],
        })

    return {
        "now": datetime.now(timezone.utc),
        "counties": counties,
        "phone_numbers": payload["phone_numbers"],
        "license_number": payload["license_number"],
        "service_offered": payload["service_offered"],
        "total_cities": sum(len(c["cities"]) for c in counties),
        "unused_total": sum(len(c["unused_cities"]) for c in counties),
    }


def render_locations(d: dict) -> str:
    lines = [
        "LOCATIONS — where an ad may be placed",
        f"As of {_dt(d['now'])}.",
        "",
        "This list is closed, not a suggestion. The posting desktop picks the "
        "Craigslist subarea by matching the county name; a county that is not "
        "below does not fail, it silently files the ad under the wrong subarea. "
        "Use these spellings exactly.",
        "",
        f"'in use' counts queued drafts plus posts published in the last 90 "
        f"days. {d['unused_total']} of {d['total_cities']} cities currently "
        f"carry nothing — those are the ones to pick from if you were asked for "
        f"somewhere we are not already advertising.",
    ]

    for county in d["counties"]:
        lines += ["", f"{county['name']}:"]
        if not county["subarea_supported"]:
            lines.append(
                "  NOT USABLE — the desktop has no subarea mapping for this "
                "county. Do not place an ad here; it would publish under the "
                "wrong subarea."
            )
        for c in county["cities"]:
            used = (
                f"in use by {c['in_use']} ad{'s' if c['in_use'] != 1 else ''}"
                if c["in_use"] else "free"
            )
            lines.append(f"  {c['city']:24s} zip {c['zip']:6s}  {used}")

    lines += [
        "",
        "Licence and phone are not free text either — an ad carries a real "
        "contractor licence and a tracked number.",
        f"  Licence number: {d['license_number']}",
        f"  Service offered: {d['service_offered']}",
        "  Active phone numbers (any one of these is valid):",
    ]
    for phone in d["phone_numbers"]:
        lines.append(f"    {phone}")
    if not d["phone_numbers"]:
        lines.append(
            "    (none active — every number has been retired. Ask the operator "
            "before writing a draft with no contact number.)"
        )
    return "\n".join(lines)
