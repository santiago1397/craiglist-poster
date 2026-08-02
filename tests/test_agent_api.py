"""The agent API — auth boundaries and the caveats that keep answers honest.

Two classes of property here, both cheap and both load-bearing.

**Auth.** Every read endpoint must reject an anonymous caller, and the publish
endpoint must refuse a key supplied in the query string. That second one is the
whole reason the read/write asymmetry exists: reads accept `?key=` because many
AI fetch tools cannot set headers, which means a read key lands in access logs.
If the publish endpoint quietly fell back to the header on a request that also
carried `?key=`, the secret would already be in the log by the time it was
honoured.

**Prose.** The renderers are pure functions over a dict, so the conditions that
make a number meaningful — a once-daily scrape, a forecast that moves, silence
that is not health — can be asserted directly. A model drops a `"caveat"` field
when summarising; it does not drop a clause in the sentence it is reading. These
tests exist so that stays true.

No database needed — every assertion below either fails before a query or runs
against a hand-built dict.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import agent as agent_svc

app = create_app()
client = TestClient(app, raise_server_exceptions=False)
paths = set(app.openapi()["paths"])
ok = []

# ---------------------------------------------------------------------------
# Routes exist
# ---------------------------------------------------------------------------

READS = [
    "/agent/help", "/agent/status", "/agent/queue", "/agent/posts",
    "/agent/stats", "/agent/problems", "/agent/logs", "/agent/inventory",
]
required = [*READS, "/agent/post-now", "/settings/api-keys", "/settings/api-keys/{key_id}"]
missing = [r for r in required if r not in paths]
assert not missing, f"missing routes: {missing}"
ok.append(f"routes OK ({len(required)} endpoints registered)")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# No credential at all. A 401 proves the guard runs before anything touches the
# database — there is no database here, so a 500 would mean a missing guard.
for path in READS:
    r = client.get(path)
    assert r.status_code == 401, f"{path} without a key returned {r.status_code}"
ok.append(f"auth OK ({len(READS)} read endpoints reject an anonymous caller)")

# Malformed keys are rejected on shape, before any lookup.
for bad in ("garbage", "12345", ".secret", "abc.secret"):
    r = client.get(f"/agent/status?key={bad}")
    assert r.status_code == 401, f"malformed key {bad!r} returned {r.status_code}"
ok.append("auth OK (malformed keys rejected on shape, before a DB lookup)")

# An admin session cookie is not an agent key and must not open this surface.
r = client.get("/agent/status", headers={"Cookie": "cl_admin_session=whatever"})
assert r.status_code == 401, f"/agent/status accepted a session cookie: {r.status_code}"
ok.append("auth OK (a dashboard session does not substitute for an API key)")

# The publish endpoint must refuse a key in the URL outright. Not fall back to
# the header — refuse, because the log entry has already been written.
r = client.post(
    "/agent/post-now?key=1.somesecret",
    json={"draft_id": 1},
    headers={"X-API-Key": "1.somesecret"},
)
assert r.status_code == 400, f"post-now with ?key= returned {r.status_code}, expected 400"
assert "header" in r.json()["detail"].lower()
ok.append("auth OK (publishing refuses a key in the query string even when a header is also present)")

r = client.post("/agent/post-now", json={"draft_id": 1})
assert r.status_code == 401, f"post-now unauthenticated returned {r.status_code}"
ok.append("auth OK (publishing rejects an anonymous caller)")

# ---------------------------------------------------------------------------
# Access-log redaction
# ---------------------------------------------------------------------------

from app.main import _RedactApiKeyFilter  # noqa: E402
import logging  # noqa: E402

record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s - %s %s %s %s", (
    "1.2.3.4", "GET", "/agent/status?key=42.supersecretvalue&format=json", "1.1", 200,
), None)
_RedactApiKeyFilter().filter(record)
assert "supersecretvalue" not in record.args[2], "access log kept the raw key"
assert "format=json" in record.args[2], "redaction ate the rest of the query string"
ok.append("logging OK (api keys are redacted from the access log, other params survive)")

# ---------------------------------------------------------------------------
# The caveats
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)

status_data = {
    "now": NOW,
    "posting_enabled": True,
    "paused_reason": None,
    "posts_last_24h_total": 1,
    "max_posts_per_day_total": 3,
    "global_blocks": [],
    "accounts": [{
        "account": "craigs1", "eligible_now": False,
        "blocked_by": ["cooldown: 4.0h since last (need 20h)"],
        "last_post_at": NOW - timedelta(hours=4), "posts_last_7d": 2,
        "queue_depth": 5, "reviewed_depth": 2,
        "next_post_at": NOW + timedelta(days=1), "next_post_title": "Roof repair",
        "next_post_draft_id": 7,
    }],
    "next_post": {
        "account": "craigs1", "at": NOW + timedelta(days=1),
        "title": "Roof repair", "draft_id": 7,
    },
    "machines": [{
        "machine": "desktop-1", "last_seen_at": NOW - timedelta(hours=9),
        "silent": True, "silent_hours": 9.0,
    }],
    "problems": {"counts": {"critical": 2, "warning": 1, "info": 0}, "total": 3},
}
text = agent_svc.render_status(status_data)

# The forecast must never read as a commitment.
assert "forecast, not a promise" in text, "status presented a projected time as certain"
# The reason an account is blocked must be the real one, not inferred by the reader.
assert "cooldown: 4.0h since last" in text
# A silent machine is the failure nobody goes looking for.
assert "SILENT" in text, "status did not flag a machine that stopped reporting"
# A status report must never be able to read as "all fine" while things burn.
assert "3 open problems (2 CRITICAL)" in text
ok.append("status OK (forecast hedged, block reason verbatim, silent machine and open problems surfaced)")

# A paused system says so first, above everything else.
paused = dict(status_data, posting_enabled=False, paused_reason="selector broken")
assert "POSTING IS PAUSED" in agent_svc.render_status(paused)
ok.append("status OK (a paused system announces it)")

stats_data = {
    "now": NOW,
    "window": "yesterday",
    "window_days": 1,
    "as_of": NOW.date(),
    "stale": False,
    "totals": {"views": 12, "impressions": 300, "posts": 1},
    "by_account": {"craigs1": {"views": 12, "impressions": 300, "posts": 1}},
    "shown": 1,
    "total": 1,
    "posts": [{
        "post_id": "7811111111", "account": "craigs1", "title": "Roof repair",
        "url": "https://example.com", "posted_ts": NOW - timedelta(days=3),
        "as_of": NOW.date(), "status": "Active",
        "total_views": 40, "total_impressions": 900,
        "d_views": 12, "d_impressions": 300, "partial_window": False,
    }],
}
text = agent_svc.render_stats(stats_data)

# The single most dangerous confusion: a lifetime average read as a daily rate.
assert "views_per_day" not in text, "stats leaked the lifetime-average field"
# Period figures are differences between scrapes, and must say so.
assert "differences between daily snapshots" in text
assert "06:00" in text and "today is never complete" in text
# Impressions and views are not the same thing and get mixed up constantly.
assert "Views = clicks into the full posting" in text
ok.append("stats OK (period deltas explained, lifetime average never surfaced, terms defined)")

stale = dict(stats_data, stale=True, as_of=(NOW - timedelta(days=9)).date())
assert "WARNING" in agent_svc.render_stats(stale), "stale stats were not flagged"
ok.append("stats OK (a stale scrape is called out, not quietly reported)")

# An empty log window must not be reported as health.
empty_logs = {
    "now": NOW, "window_hours": 24, "filter": {"account": None, "flow": None},
    "total": 0, "shown": 0, "entries": [],
}
text = agent_svc.render_logs(empty_logs)
assert "silence here is not proof of health" in text
ok.append("logs OK (no errors is not reported as healthy)")

# Truncation must always announce itself. A list that looks complete and is not
# leads straight to "nothing else is wrong".
assert "Showing 2 of 50" in agent_svc._showing(2, 50, "Narrow it.")
assert "Showing all 3" in agent_svc._showing(3, 3, "Narrow it.")
ok.append("output OK (truncated lists say so; complete ones say that too)")

# Timestamps are emitted in the display zone with the zone named, so a model
# never has to know the server stores UTC.
rendered = agent_svc._dt(NOW)
assert "EDT" in rendered or "EST" in rendered, f"timestamp had no zone: {rendered}"
assert agent_svc._dt(None) == "never"
ok.append("output OK (timestamps carry a named local zone)")

# ---------------------------------------------------------------------------
# Help stays in sync with the routes
# ---------------------------------------------------------------------------

from app.routers.agent import _describe_routes  # noqa: E402

described = _describe_routes("https://example.com")
for path in READS:
    if path == "/agent/help":
        continue
    assert f"https://example.com{path}" in described, f"{path} missing from generated help"
assert "https://example.com/agent/post-now" in described
# Parameters are read off the live signature, so a new one is documented for free.
assert "window (default: 7d)" in described
assert "'yesterday', '7d' or '30d'" in described
ok.append("help OK (generated from the live route table, parameters included)")

print("\n".join(ok))
