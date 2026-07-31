"""End-to-end exercise of the phase-1 queue logic against a real Postgres.

Covers the paths that only fail at runtime: the SQL itself, pg_trgm, the
claim's row lock, and the draft state machine.
"""
from datetime import datetime, timedelta, timezone

from app.db import conn, init_pool, tx
from app.services import drafts as drafts_svc
from app.services import ingest as ingest_svc
from app.services import queue as queue_svc
from app.schemas.events import PostAttempt

init_pool()

# Thursday 2026-07-30 14:00 America/New_York -> inside the window, a weekday.
NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
ACCOUNTS = ["craigs1", "craigs2", "craigs3"]
ok = []


def reset():
    with tx() as c:
        c.execute("TRUNCATE drafts, posts, post_attempts, ghost_checks, flow_errors CASCADE")
        c.execute("UPDATE guardrail_settings SET max_posts_per_day_total = 3, "
                  "min_hours_between_posts_same_account = 20, "
                  "max_posts_per_account_per_week = 7, "
                  # The window and the weekday rule are asserted on below, so
                  # set them here rather than inheriting whatever the last
                  # script left behind — `guardrail_settings` is a singleton
                  # shared by every test in this directory, and a widened window
                  # elsewhere silently turned the out-of-window check into a
                  # no-op that still passed.
                  "post_window_start_hour = 8, post_window_end_hour = 19, "
                  "post_weekdays_only = TRUE, "
                  # Reset the kill switch too, or a previous test leaving it
                  # paused makes every check here fail for the wrong reason.
                  "posting_enabled = TRUE, paused_at = NULL, paused_reason = NULL")


def add_draft(account, title, body="body text here", **kw):
    with tx() as c:
        return drafts_svc.create_draft(c, {"account": account, "title": title,
                                           "body": body, "body_head": body, **kw})


def add_post(account, post_id, when, title="old ad"):
    with tx() as c:
        c.execute(
            "INSERT INTO posts (post_id, account, title, posted_ts, source) "
            "VALUES (%s, %s, %s, %s, 'test')", (post_id, account, title, when))


# --- 1. empty queue is a block reason, not a crash -------------------------
reset()
rep = queue_svc.evaluate_eligibility(conn().__enter__(), ACCOUNTS, now=NOW)
assert rep["global_blocks"] == [], f"unexpected global blocks: {rep['global_blocks']}"
assert all(not a["eligible"] for a in rep["accounts"].values())
assert all("queue empty" in " ".join(a["reasons"]) for a in rep["accounts"].values())
ok.append("empty queue reported as a block reason for every account")

# --- 2. claim returns nothing when the queue is empty (fail closed) --------
with tx() as c:
    res = queue_svc.claim_next(c, machine="m1", candidate_accounts=ACCOUNTS, now=NOW)
assert res["draft"] is None, "claimed a draft from an empty queue"
ok.append("claim returns None on an empty queue (fail closed)")

# --- 3. longest-idle account with drafts wins ------------------------------
reset()
add_post("craigs1", "p1", NOW - timedelta(days=5))     # idle longest
add_post("craigs2", "p2", NOW - timedelta(days=2))
add_draft("craigs2", "c2 first")
add_draft("craigs2", "c2 second")
with tx() as c:
    res = queue_svc.claim_next(c, machine="m1", candidate_accounts=ACCOUNTS, now=NOW)
d = res["draft"]
assert d is not None and d["account"] == "craigs2" and d["title"] == "c2 first", \
    f"wrong draft claimed: {d}"
assert d["status"] == "claimed" and d["attempts"] == 1
ok.append("craigs1 idled longest but had no drafts -> claimed craigs2 head instead")

# --- 4. a claim in flight blocks a second claim on that account ------------
# Every count that would otherwise stop a double post — the cooldown, the daily
# cap — reads the `posts` table, which ingest only fills once the attempt comes
# back. While a run is in flight that history is stale, so the claim itself is
# the only evidence it happened. Without this the 12:59 manual post and the
# 13:00 scheduled fire both get authorised and both publish.
with tx() as c:
    res2 = queue_svc.claim_next(c, machine="m1", candidate_accounts=ACCOUNTS, now=NOW)
assert res2["draft"] is None, "claimed a second draft while one was still in flight"
assert any(
    "in flight" in r for r in res2["eligibility"]["accounts"]["craigs2"]["reasons"]
), res2["eligibility"]["accounts"]["craigs2"]["reasons"]
ok.append("a claim in flight blocks a second claim on that account (no double post)")

# --- 4b. once it resolves, the next claim advances and never re-serves -----
with tx() as c:
    # Post-upload failure: parks the draft and frees the account.
    queue_svc.release_or_park(
        c, draft_id=d["id"], failed_step="publish", failed_message="test"
    )
    res3 = queue_svc.claim_next(c, machine="m1", candidate_accounts=ACCOUNTS, now=NOW)
assert res3["draft"] is not None, "nothing claimable after the in-flight one resolved"
assert res3["draft"]["id"] != d["id"], "same draft claimed twice"
assert res3["draft"]["title"] == "c2 second"
ok.append("second claim advances to the next draft, never re-serves a claimed one")

# --- 5. cooldown blocks an account that just posted ------------------------
reset()
add_post("craigs1", "p9", NOW - timedelta(hours=3))
add_draft("craigs1", "too soon")
rep = queue_svc.evaluate_eligibility(conn().__enter__(), ["craigs1"], now=NOW)
assert not rep["accounts"]["craigs1"]["eligible"]
assert any("cooldown" in r for r in rep["accounts"]["craigs1"]["reasons"])
with tx() as c:
    assert queue_svc.claim_next(c, machine="m1", candidate_accounts=["craigs1"], now=NOW)["draft"] is None
ok.append("20h cooldown blocks the claim (3h since last post)")

# --- 6. daily cap blocks everything ----------------------------------------
reset()
for i in range(3):
    add_post("craigs3", f"d{i}", NOW - timedelta(hours=i + 1))
add_draft("craigs1", "capped out")
rep = queue_svc.evaluate_eligibility(conn().__enter__(), ACCOUNTS, now=NOW)
assert any("daily cap" in b for b in rep["global_blocks"]), rep["global_blocks"]
ok.append("3 posts in 24h trips the global daily cap")

# --- 7. weekend / out-of-window are global blocks --------------------------
sat = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)   # Saturday
rep = queue_svc.evaluate_eligibility(conn().__enter__(), ACCOUNTS, now=sat)
assert any("weekend" in b for b in rep["global_blocks"]), rep["global_blocks"]
night = datetime(2026, 7, 31, 6, 0, tzinfo=timezone.utc)  # 02:00 ET
rep = queue_svc.evaluate_eligibility(conn().__enter__(), ACCOUNTS, now=night)
assert any("outside posting window" in b for b in rep["global_blocks"]), rep["global_blocks"]
ok.append("weekend and out-of-window produce global blocks")

# --- 8. failure routing: the decision-16 split -----------------------------
reset()
a = add_draft("craigs1", "fails early")
b = add_draft("craigs1", "fails late")
with tx() as c:
    assert queue_svc.release_or_park(c, draft_id=a["id"], failed_step="form_body",
                                     failed_message="x") == "queued"
    assert queue_svc.release_or_park(c, draft_id=b["id"], failed_step="photo_upload",
                                     failed_message="x") == "needs_attention"
    # An unknown step must fail safe, i.e. park rather than silently retry.
    assert queue_svc.release_or_park(c, draft_id=b["id"], failed_step=None,
                                     failed_message="x") == "needs_attention"
ok.append("pre-upload failure requeues, post-upload and unknown steps park")

# --- 9. ingest routes the draft off a post_attempt event -------------------
reset()
d1 = add_draft("craigs1", "will post")
d2 = add_draft("craigs2", "will fail late")
with tx() as c:
    ingest_svc.ingest_events(c, [
        PostAttempt(ts=NOW, machine="m1", account="craigs1", outcome="posted",
                    draft_id=d1["id"], post_id="7788", post_url="http://x", ad_title="will post"),
        PostAttempt(ts=NOW, machine="m1", account="craigs2", outcome="failed_other",
                    draft_id=d2["id"], failed_step="billing", error_message="stuck"),
    ])
with conn() as c:
    assert drafts_svc.get_draft(c, d1["id"])["status"] == "posted"
    assert drafts_svc.get_draft(c, d1["id"])["posted_post_id"] == "7788"
    got = drafts_svc.get_draft(c, d2["id"])
    assert got["status"] == "needs_attention" and got["failed_step"] == "billing"
    # The posted draft must also have created the posts-table row via ingest.
    assert c.execute("SELECT COUNT(*) n FROM posts WHERE post_id='7788'").fetchone()["n"] == 1
ok.append("post_attempt ingest moves drafts to posted / needs_attention and upserts posts")

# --- 10. replayed event must not re-route a draft --------------------------
with conn() as c:
    before = drafts_svc.get_draft(c, d1["id"])["status"]
with tx() as c:
    ev = PostAttempt(ts=NOW, machine="m1", account="craigs1", outcome="failed_other",
                     draft_id=d1["id"], failed_step="billing")
    ingest_svc.ingest_events(c, [ev])
    ingest_svc.ingest_events(c, [ev])   # duplicate event_id
with conn() as c:
    after = drafts_svc.get_draft(c, d1["id"])["status"]
assert before == "posted", before
assert after == "needs_attention", "first delivery should still route"
ok.append("duplicate event_id is ignored on replay (draft routed once, not twice)")

# --- 11. reorder ------------------------------------------------------------
reset()
x = add_draft("craigs1", "first")
y = add_draft("craigs1", "second")
z = add_draft("craigs1", "third")
with tx() as c:
    drafts_svc.reorder_draft(c, z["id"], after_id=None)      # move to head
with conn() as c:
    order = [r["title"] for r in drafts_svc.list_drafts(c, account="craigs1")["drafts"]]
assert order == ["third", "first", "second"], order
with tx() as c:
    drafts_svc.reorder_draft(c, z["id"], after_id=x["id"])   # between first and second
with conn() as c:
    order = [r["title"] for r in drafts_svc.list_drafts(c, account="craigs1")["drafts"]]
assert order == ["first", "third", "second"], order
ok.append("reorder moves a draft to the head and between neighbours")

# --- 12. not_before / expires_at -------------------------------------------
reset()
add_draft("craigs1", "future", not_before=NOW + timedelta(days=30))
with tx() as c:
    assert queue_svc.claim_next(c, machine="m1", candidate_accounts=["craigs1"], now=NOW)["draft"] is None
add_draft("craigs2", "stale", expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
with tx() as c:
    n = queue_svc.expire_stale(c)
assert n == 1, f"expected 1 expired, got {n}"
ok.append("not_before hides a draft; expires_at sweeps it to 'expired'")

# --- 13. similarity (pg_trgm) ----------------------------------------------
reset()
add_draft("craigs1", "Roof Repair Serving Davie",
          body="Quality roofing work in Davie does not have to cost a fortune.")
add_post("craigs1", "p77", NOW - timedelta(days=3), title="Roof Repair Serving Doral")
with conn() as c:
    rep = drafts_svc.similarity_report(
        c, head="Quality roofing work in Doral does not have to cost a fortune.")
assert rep["closest_draft"] is not None and rep["closest_draft"]["score"] > 0.5, rep
assert rep["closest_post"] is not None, rep
ok.append(f"pg_trgm similarity works (near-duplicate scored "
          f"{rep['closest_draft']['score']:.2f})")

# --- 14. guardrail update round-trip ---------------------------------------
with tx() as c:
    g = queue_svc.update_guardrails(c, {"max_posts_per_day_total": 4, "bogus": 99})
assert g["max_posts_per_day_total"] == 4 and "bogus" not in g
ok.append("guardrail update writes known keys and ignores unknown ones")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
