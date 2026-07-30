"""The pause switch must actually stop claims, and resume must restore order.

The failure that matters is a switch that looks off in the UI but still hands
out drafts, so this asserts on claim_next, not just on the stored flag.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.auth import require_admin
from app.db import conn, init_pool, tx
from app.main import create_app
from app.services import drafts as drafts_svc
from app.services import queue as queue_svc

init_pool()
NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)  # Thu 14:00 ET, in-window
ok = []

app = create_app()
app.dependency_overrides[require_admin] = lambda: "admin@test"
client = TestClient(app, raise_server_exceptions=False)

with tx() as c:
    c.execute("TRUNCATE drafts, posts, post_attempts CASCADE")
    c.execute("UPDATE guardrail_settings SET posting_enabled = TRUE, "
              "paused_at = NULL, paused_reason = NULL")
    a = drafts_svc.create_draft(c, {"account": "craigs1", "title": "first",
                                    "body": "b", "body_head": "b"})
    b = drafts_svc.create_draft(c, {"account": "craigs1", "title": "second",
                                    "body": "b", "body_head": "b"})

# Baseline: with posting on, a claim succeeds.
with tx() as c:
    res = queue_svc.claim_next(c, machine="m1", candidate_accounts=["craigs1"], now=NOW)
assert res["draft"] is not None and res["draft"]["title"] == "first", res
ok.append("baseline: claim works while posting is enabled")

# Put it back so the pause test starts from a full queue.
with tx() as c:
    drafts_svc.update_draft(c, a["id"], {"status": "queued"})

# Pause via the API the button calls.
r = client.put("/settings/posting", json={"enabled": False, "reason": "roof crew away"})
assert r.status_code == 200, (r.status_code, r.text)
assert r.json()["enabled"] is False and r.json()["paused_reason"] == "roof crew away"
ok.append("PUT /settings/posting pauses and records the reason")

# The switch must stop the claim, not merely display as off.
with tx() as c:
    res = queue_svc.claim_next(c, machine="m1", candidate_accounts=["craigs1"], now=NOW)
assert res["draft"] is None, f"paused system still handed out a draft: {res['draft']}"
blocks = res["eligibility"]["global_blocks"]
assert any("paused" in x for x in blocks), blocks
assert any("roof crew away" in x for x in blocks), blocks
ok.append("claim returns nothing while paused, citing the reason")

# The queue must be untouched by pausing.
with conn() as c:
    queued = drafts_svc.list_drafts(c, status="queued", account="craigs1")
assert queued["total"] == 2, f"pausing changed the queue: {queued['total']}"
assert [d["title"] for d in queued["drafts"]] == ["first", "second"]
ok.append("pausing leaves the queue and its order untouched")

# Resume restores service and clears the pause metadata.
r = client.put("/settings/posting", json={"enabled": True})
assert r.status_code == 200 and r.json()["enabled"] is True
assert r.json()["paused_at"] is None and r.json()["paused_reason"] is None
with tx() as c:
    res = queue_svc.claim_next(c, machine="m1", candidate_accounts=["craigs1"], now=NOW)
assert res["draft"] is not None and res["draft"]["title"] == "first", res
ok.append("resume clears the pause and the queue continues from the head")

# GET must reflect state for the UI on load.
r = client.get("/settings/posting")
assert r.status_code == 200 and r.json()["enabled"] is True
ok.append("GET /settings/posting reports current state")

# The switch is admin-only; a machine token must not be able to unpause itself.
unauth = TestClient(create_app(), raise_server_exceptions=False)
assert unauth.put("/settings/posting", json={"enabled": True}).status_code == 401
assert unauth.get("/settings/posting").status_code == 401
ok.append("the switch is admin-only (401 without a session)")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
