"""The agent reports, against a real database.

`test_agent_api.py` covers auth and the prose. This covers the SQL, which is
where the interesting failures live — every report joins several tables and
three of them were written for this feature.

The regression worth naming is the stats window. Counters are cumulative, so a
period figure is a difference between two snapshots. The obvious way to pick
the baseline is `snapshot_date <= CURRENT_DATE - N` — and it is wrong, because
`snapshot_date` is written in America/New_York by the desktop scraper while
CURRENT_DATE is evaluated in the database's timezone, which is UTC in
production. Every evening after 20:00 Eastern those disagree by a day, the
baseline selects the same row as the latest snapshot, and every post reports
exactly zero views. Silently: no error, no empty result, just plausible zeros.

`test_stats_window_is_anchored_to_the_data` pins the fix. It places every
snapshot far enough in the past that a CURRENT_DATE-relative baseline would
collapse onto the newest row no matter what timezone either side is in, so the
test fails on the old query regardless of when it runs.

Needs the scratch database — see tests/README.md.
"""
from datetime import date, datetime, timedelta, timezone

from app.db import conn, init_pool, tx
from app.security import _resolve, issue_api_key, revoke_api_key
from app.services import agent as agent_svc
from app.services import drafts as drafts_svc

init_pool()
NOW = datetime.now(timezone.utc)
ok = []

REPORTS = [
    ("status", lambda c: agent_svc.status_report(c), agent_svc.render_status),
    ("queue", lambda c: agent_svc.queue_report(c), agent_svc.render_queue),
    ("posts", lambda c: agent_svc.posts_report(c), agent_svc.render_posts),
    ("stats/yesterday", lambda c: agent_svc.stats_report(c, window="yesterday"), agent_svc.render_stats),
    ("stats/7d", lambda c: agent_svc.stats_report(c, window="7d"), agent_svc.render_stats),
    ("stats/30d", lambda c: agent_svc.stats_report(c, window="30d"), agent_svc.render_stats),
    ("problems", lambda c: agent_svc.problems_report(c), agent_svc.render_problems),
    ("logs", lambda c: agent_svc.logs_report(c), agent_svc.render_logs),
    ("inventory", lambda c: agent_svc.inventory_report(c), agent_svc.render_inventory),
]


def reset():
    with tx() as c:
        c.execute(
            "TRUNCATE posts, snapshots, drafts, post_attempts, flow_errors, "
            "account_states, api_keys CASCADE"
        )


def run_all(phase: str):
    """Every report must render. An empty database is the case most likely to
    raise, and the one an agent hits first on a fresh install."""
    for name, build, render in REPORTS:
        with conn() as c:
            data = build(c)
        text = render(data)
        assert isinstance(text, str) and text.strip(), f"{name} rendered nothing"
    ok.append(f"reports OK ({len(REPORTS)} reports render against a {phase} database)")


def add_snapshot(c, post_id: str, day: date, views: int, impressions: int):
    c.execute(
        "INSERT INTO snapshots (post_id, snapshot_date, snapshot_ts_utc, status, "
        "impressions, views, shares, favorites) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (post_id, snapshot_date) DO NOTHING",
        (post_id, day, NOW, "Active", impressions, views, 0, 0),
    )


def add_post(c, post_id: str, account: str, title: str, age_days: int):
    c.execute(
        "INSERT INTO posts (post_id, account, title, url, posted_ts, source) "
        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (post_id) DO NOTHING",
        (post_id, account, title, f"https://example.com/{post_id}",
         NOW - timedelta(days=age_days), "stats_sync"),
    )


# ---------------------------------------------------------------------------
reset()
run_all("empty")

with tx() as c:
    add_post(c, "7811111111", "craigs1", "Roof repair Miami", 30)
    # Deliberately old, and deliberately uneven. See the module docstring: a
    # CURRENT_DATE-relative baseline collapses onto the newest row here in any
    # timezone, so this data fails the old query and passes the new one.
    today = date.today()
    add_snapshot(c, "7811111111", today - timedelta(days=25), 5, 100)
    add_snapshot(c, "7811111111", today - timedelta(days=19), 40, 900)
    add_snapshot(c, "7811111111", today - timedelta(days=18), 52, 1180)
    c.execute(
        "INSERT INTO account_states (event_id, account, machine, ts, eligible_now, "
        "posts_last_24h_total, posts_last_7d_this_account) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        ("evt-state-1", "craigs1", "desktop-1", NOW - timedelta(hours=12), False, 1, 2),
    )
    c.execute(
        "INSERT INTO flow_errors (event_id, ts, machine, flow, step, account, "
        "error_type, error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("evt-1", NOW - timedelta(hours=2), "desktop-1", "post", "photo_upload",
         "craigs1", "TimeoutError", "waiting for selector .gallery"),
    )
    c.execute(
        "INSERT INTO post_attempts (event_id, ts, machine, account, outcome, "
        "error_type, error_message, failed_step, ad_title) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("evt-2", NOW - timedelta(hours=3), "desktop-1", "craigs1", "failed",
         "TimeoutError", "form never loaded", "open_form", "Roof repair Miami"),
    )
    drafts_svc.create_draft(c, {
        "account": "craigs1", "title": "New roof Boca", "body": "b" * 50,
        "body_head": "New roof Boca", "city": "Boca Raton",
    })
    reviewed = drafts_svc.create_draft(c, {
        "account": "craigs1", "title": "Reviewed one", "body": "b" * 50,
        "body_head": "Reviewed one", "city": "Miami",
    })
    c.execute("UPDATE drafts SET reviewed = TRUE WHERE id = %s", (reviewed["id"],))

run_all("populated")

# --- the regression -------------------------------------------------------
# Snapshots at 25/19/18 days old, cumulative views 5/40/52.
#   yesterday -> the two newest rows:            52 - 40 = 12
#   7d        -> newest, back 7 days -> the 25-day row: 52 - 5 = 47
# Both are non-zero. Under the CURRENT_DATE-relative baseline both were 0,
# because every row satisfied `snapshot_date <= CURRENT_DATE - N`.
with conn() as c:
    y = agent_svc.stats_report(c, window="yesterday")
    w = agent_svc.stats_report(c, window="7d")

py = [p for p in y["posts"] if p["post_id"] == "7811111111"][0]
pw = [p for p in w["posts"] if p["post_id"] == "7811111111"][0]
assert py["d_views"] == 12, f"yesterday delta is {py['d_views']}, expected 52-40=12"
assert pw["d_views"] == 47, f"7d delta is {pw['d_views']}, expected 52-5=47"
assert py["total_views"] == 52, "lifetime total should be the newest cumulative value"
assert py["d_views"] != py["total_views"], "a period delta must differ from the lifetime total"
ok.append("stats OK (window anchored to the data, not to CURRENT_DATE — the evening-zeros bug)")

# A post younger than the window reports its whole life, and says so.
with tx() as c:
    add_post(c, "7822222222", "craigs2", "Fresh post", 0)
    add_snapshot(c, "7822222222", date.today(), 2, 10)
with conn() as c:
    report = agent_svc.stats_report(c, window="30d")
fresh = [p for p in report["posts"] if p["post_id"] == "7822222222"][0]
assert fresh["partial_window"], "a post younger than the window was not flagged"
assert "younger than the window" in agent_svc.render_stats(report)
ok.append("stats OK (a post younger than the window is flagged in prose, not silently averaged)")

# --- reports read the state they claim to ---------------------------------
with conn() as c:
    text = agent_svc.render_queue(agent_svc.queue_report(c))
assert "NOT REVIEWED" in text, "queue did not distinguish unreviewed drafts"
assert "Missing: no cover image" in text, "queue did not report a missing cover"
ok.append("queue OK (review state and missing images both surfaced)")

with conn() as c:
    logs = agent_svc.logs_report(c, hours=24)
assert {e["source"] for e in logs["entries"]} == {"flow_error", "post_attempt"}, \
    "logs must merge both places a failure lands"
text = agent_svc.render_logs(logs)
assert "photo_upload" in text and "form never loaded" in text
ok.append("logs OK (flow errors and failed posting runs both present)")

with conn() as c:
    assert agent_svc.logs_report(c, hours=24, account="craigs2")["total"] == 0, \
        "account filter leaked other accounts"
    s = agent_svc.stats_report(c, window="30d", account="craigs1")
assert all(p["account"] == "craigs1" for p in s["posts"]), "stats account filter leaked"
ok.append("filters OK (account filter honoured on logs and stats)")

# `since` must accept what an agent will actually send. posts_page interpolates
# this into `posted_ts >= %s`, so an unconverted '30d' is a 500.
with conn() as c:
    for since in ("30d", "7d", "all", "2026-01-01", "nonsense"):
        agent_svc.posts_report(c, since=since)
ok.append("posts OK ('30d', 'all', a date and junk all resolve rather than erroring")

# --- keys -----------------------------------------------------------------
key = issue_api_key("test key", "read")
assert _resolve(key)["scope"] == "read"
assert _resolve(issue_api_key("publisher", "post"))["scope"] == "post"

key_id = int(key.split(".")[0])
assert revoke_api_key(key_id) is True
try:
    _resolve(key)
    raise AssertionError("a revoked key still authenticated")
except Exception as e:
    assert "Invalid" in str(e) or "401" in str(e), f"unexpected error: {e!r}"
assert revoke_api_key(key_id) is False, "double revoke reported success"

forged = f"{issue_api_key('victim', 'post').split('.')[0]}.wrongsecret"
try:
    _resolve(forged)
    raise AssertionError("a forged secret authenticated against a real key id")
except Exception:
    pass
ok.append("keys OK (scopes stored, revocation sticks and is idempotent, forged secrets rejected)")

reset()
print("\n".join(ok))
