"""Operator-triggered posting of one named draft ("Post now").

The properties worth protecting, in order of how much they'd cost to get wrong:

1. A targeted claim hands back the draft that was asked for, or nothing. It
   must never fall through to a different draft — the operator clicked one row.
2. Naming a draft id is not permission to post it. The claim requires a live
   request on that draft, so a machine token cannot pull drafts out of order.
3. The guardrails still decide. "Post now" changes which draft and when it is
   attempted, never whether it is allowed.
4. The request ends exactly once, on every outcome, so nothing retries
   unattended.

Needs the scratch database — see tests/README.md.
"""
from datetime import datetime, timedelta, timezone

from app.db import conn, init_pool, tx
from app.services import drafts as drafts_svc
from app.services import ingest as ingest_svc
from app.services import queue as queue_svc
from app.schemas.events import PostAttempt

init_pool()

# Thursday 2026-07-30 14:00 America/New_York — a weekday, inside the window.
NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
ok = []


def reset():
    with tx() as c:
        c.execute("TRUNCATE drafts, posts, post_attempts, ghost_checks, flow_errors CASCADE")
        c.execute(
            "UPDATE guardrail_settings SET max_posts_per_day_total = 3, "
            "min_hours_between_posts_same_account = 20, "
            "max_posts_per_account_per_week = 7, "
            "posting_enabled = TRUE, paused_at = NULL, paused_reason = NULL"
        )


def add_draft(account: str, title: str) -> int:
    with tx() as c:
        return drafts_svc.create_draft(
            c, {"account": account, "title": title, "body": "body", "body_head": title}
        )["id"]


def add_post(account: str, post_id: str, when: datetime):
    with tx() as c:
        c.execute(
            "INSERT INTO posts (post_id, account, title, posted_ts) VALUES (%s,%s,%s,%s)",
            (post_id, account, "old", when),
        )


def get(draft_id: int) -> dict:
    with conn() as c:
        return drafts_svc.get_draft(c, draft_id)


# --- 1. the request is refused synchronously when guardrails say no --------
reset()
add_post("craigs1", "recent", NOW - timedelta(hours=3))  # inside this test's cooldown
d1 = add_draft("craigs1", "blocked by cooldown")
try:
    with tx() as c:
        drafts_svc.request_post(c, d1, requested_by="a@b.c", now=NOW)
    raise AssertionError("a blocked account accepted a post request")
except drafts_svc.NotEligible as e:
    assert any("cooldown" in r for r in e.reasons), e.reasons
assert get(d1)["post_requested_at"] is None, "a refused request still set the flag"
ok.append("a request blocked by the cooldown is refused and writes no flag")

# --- 2. an eligible account accepts, and the flag is what gets written -----
reset()
d2 = add_draft("craigs1", "the one to post")
with tx() as c:
    drafts_svc.request_post(c, d2, requested_by="owen@example.com", now=NOW)
row = get(d2)
assert row["post_requested_at"] is not None
assert row["post_requested_by"] == "owen@example.com"
assert row["status"] == "queued", "requesting changed the draft's status"
ok.append("an eligible request sets the flag and leaves status alone")

# --- 3. THE ONE THAT MATTERS: a targeted claim never serves another draft --
reset()
first = add_draft("craigs1", "head of the queue")
second = add_draft("craigs1", "the one actually requested")
with tx() as c:
    drafts_svc.request_post(c, second, requested_by="a@b.c", now=NOW)
with tx() as c:
    res = queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=second
    )
assert res["draft"] is not None, "the requested draft was not claimed"
assert res["draft"]["id"] == second, f"claimed the wrong draft: {res['draft']['id']}"
assert get(first)["status"] == "queued", "the head of the queue was disturbed"
ok.append("a targeted claim takes the requested draft, not the head of the queue")

# --- 4. naming an id is not permission — the flag is the authorisation -----
reset()
d4 = add_draft("craigs1", "never requested")
with tx() as c:
    res = queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=d4
    )
assert res["draft"] is None, "claimed an unrequested draft by naming its id"
assert res["refused"] == "not_requested", res
assert get(d4)["status"] == "queued"
ok.append("naming a draft id without a live request claims nothing")

# --- 5. guardrails still decide, even for a request already accepted -------
# The request was legal when made; the daily cap fills before the desktop polls.
reset()
d5 = add_draft("craigs1", "requested then capped")
with tx() as c:
    drafts_svc.request_post(c, d5, requested_by="a@b.c", now=NOW)
for i in range(3):
    add_post("craigs3", f"cap{i}", NOW - timedelta(hours=i + 1))
with tx() as c:
    res = queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=d5
    )
assert res["draft"] is None, "a targeted claim bypassed the daily cap"
assert any("daily cap" in b for b in res["eligibility"]["global_blocks"])
assert get(d5)["status"] == "queued", "a refused claim consumed the draft"
ok.append("a targeted claim is still refused by the daily cap")

# --- 6. an account this machine isn't bound to is refused ------------------
reset()
d6 = add_draft("craigs2", "other machine's account")
with tx() as c:
    drafts_svc.request_post(c, d6, requested_by="a@b.c", now=NOW)
with tx() as c:
    res = queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=d6
    )
assert res["draft"] is None
assert res["refused"] == "not_bound", res
ok.append("a draft for an unbound account is refused, not claimed")

# --- 7. the poll only offers requests for the asking machine's accounts ----
reset()
mine = add_draft("craigs1", "mine")
theirs = add_draft("craigs2", "theirs")
with tx() as c:
    drafts_svc.request_post(c, mine, requested_by="a@b.c", now=NOW)
    drafts_svc.request_post(c, theirs, requested_by="a@b.c", now=NOW)
with conn() as c:
    pending = queue_svc.pending_post_requests(c, accounts=["craigs1"])
assert [p["id"] for p in pending] == [mine], pending
ok.append("the machine poll filters requests to that machine's accounts")

# --- 8. the request ends on success -----------------------------------------
reset()
d8 = add_draft("craigs1", "will publish")
with tx() as c:
    drafts_svc.request_post(c, d8, requested_by="a@b.c", now=NOW)
    queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=d8
    )
with tx() as c:
    ingest_svc.ingest_events(c, [PostAttempt(
        event_id="ev-posted", ts=NOW, machine="m1", account="craigs1",
        outcome="posted", post_id="7777", post_url="https://x/7777",
        ad_title="will publish", draft_id=d8,
    )])
row = get(d8)
assert row["status"] == "posted"
assert row["post_requested_at"] is None, "the request outlived the post"
ok.append("a published draft clears its request")

# --- 9. the request ends on failure too, so nothing retries unattended -----
# A pre-upload failure sends the draft back to 'queued'. If the flag survived,
# the daemon would spawn another run on its next 15s beat, forever.
reset()
d9 = add_draft("craigs1", "will fail")
with tx() as c:
    drafts_svc.request_post(c, d9, requested_by="a@b.c", now=NOW)
    queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=d9
    )
with tx() as c:
    ingest_svc.ingest_events(c, [PostAttempt(
        event_id="ev-failed", ts=NOW, machine="m1", account="craigs1",
        outcome="failed_other", ad_title="will fail", draft_id=d9,
        failed_step="launch", error_type="failed_other",
        error_message="chrome would not start",
    )])
row = get(d9)
assert row["status"] == "queued", "a pre-upload failure should requeue"
assert row["post_requested_at"] is None, "a failed request would retry forever"
assert "chrome" in (row["post_request_error"] or ""), row["post_request_error"]
ok.append("a failed draft requeues but its request ends, with the reason kept")

# --- 10. the TTL backstop clears a request nothing collected ---------------
reset()
d10 = add_draft("craigs1", "nobody was listening")
with tx() as c:
    drafts_svc.request_post(c, d10, requested_by="a@b.c", now=NOW)
    c.execute(
        "UPDATE drafts SET post_requested_at = NOW() - make_interval(mins => %s) "
        "WHERE id = %s",
        (queue_svc.POST_REQUEST_TTL_MINUTES + 5, d10),
    )
with tx() as c:
    cleared = queue_svc.expire_post_requests(c)
assert cleared == 1, cleared
row = get(d10)
assert row["post_requested_at"] is None
assert "expired" in (row["post_request_error"] or "").lower(), row["post_request_error"]
ok.append("a request nothing collected expires and says so on the draft")

# --- 11. cancelling withdraws it -------------------------------------------
reset()
d11 = add_draft("craigs1", "changed my mind")
with tx() as c:
    drafts_svc.request_post(c, d11, requested_by="a@b.c", now=NOW)
with tx() as c:
    assert drafts_svc.clear_post_request(c, d11) is True
assert get(d11)["post_requested_at"] is None
with tx() as c:
    res = queue_svc.claim_next(
        c, machine="m1", candidate_accounts=["craigs1"], now=NOW, draft_id=d11
    )
assert res["draft"] is None and res["refused"] == "not_requested"
ok.append("a cancelled request cannot be claimed")

print("")
for line in ok:
    print(f"  OK  {line}")
print(f"\n{len(ok)} checks passed")
