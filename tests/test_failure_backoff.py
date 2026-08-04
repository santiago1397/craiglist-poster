"""One broken account must not consume the whole day's posting.

The failure this guards against: an account whose card is not set up dies at the
`billing` step. Nothing about that reached the rotation, because every count in
`evaluate_eligibility` reads `posts` and a failed attempt never gets there. So
the broken account stayed the longest-idle one, won the very next fire, failed
again, and repeated for all eight fires of the day — parking eight drafts and
burning their images, while the three healthy accounts posted nothing.
"""
from datetime import datetime, timedelta, timezone

from app.db import conn, init_pool, tx
from app.services import drafts as drafts_svc
from app.services import queue as q

init_pool()
ok = []

ACCOUNTS = ["craigs1", "craigs2", "craigs3", "craigs4"]
NOW = datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc)  # Wed 09:00 ET, in window


def reset():
    with tx() as c:
        c.execute("TRUNCATE drafts, posts, post_attempts CASCADE")
        c.execute(
            "UPDATE guardrail_settings SET posting_enabled = TRUE, "
            "failure_backoff_minutes = 60, billing_failure_backoff_minutes = 720 "
            "WHERE singleton"
        )
        for a in ACCOUNTS:
            for i in range(3):
                drafts_svc.create_draft(c, {
                    "account": a, "title": f"{a} draft {i}", "body": "b" * 60,
                })


_seq = iter(range(1, 10_000))


def fail(account, step, at):
    # event_id is the outbox idempotency key: NOT NULL with no default, because
    # ingest relies on it to make a redelivered event a no-op.
    with tx() as c:
        c.execute(
            "INSERT INTO post_attempts (event_id, ts, machine, account, outcome, "
            "error_type, error_message, failed_step) "
            "VALUES (%s, %s, 'm', %s, 'failed_other', 'failed_other', 'x', %s)",
            (f"test-fail-{next(_seq)}", at, account, step),
        )


# --- a billing failure takes that account out, not the day ------------------
reset()
fail("craigs1", "billing", NOW - timedelta(minutes=5))

with conn() as c:
    rep = q.evaluate_eligibility(c, ACCOUNTS, now=NOW)

assert not rep["accounts"]["craigs1"]["eligible"], "the failed account is still eligible"
why = " ".join(rep["accounts"]["craigs1"]["reasons"])
assert "backing off" in why and "billing" in why, why
assert "payment method" in why, "a billing backoff must say what to actually fix"
ok.append("an account that failed at billing is backed off, and told why")

for other in ACCOUNTS[1:]:
    assert rep["accounts"][other]["eligible"], (
        f"{other} was blocked by another account's failure: "
        f"{rep['accounts'][other]['reasons']}"
    )
ok.append("the other three accounts stay eligible")

# --- and the claim goes to one of them --------------------------------------
with tx() as c:
    res = q.claim_next(c, machine="m", candidate_accounts=ACCOUNTS, now=NOW)
assert res["draft"] is not None, f"nothing was claimed: {res.get('eligibility')}"
assert res["draft"]["account"] != "craigs1", "the broken account was handed the fire"
ok.append("the next fire is claimed by a working account")

# --- a transient failure backs off for an hour, not twelve ------------------
reset()
fail("craigs2", "form_title", NOW - timedelta(minutes=90))
with conn() as c:
    rep = q.evaluate_eligibility(c, ACCOUNTS, now=NOW)
assert rep["accounts"]["craigs2"]["eligible"], (
    f"a 90-minute-old transient failure should have cleared a 60-minute backoff: "
    f"{rep['accounts']['craigs2']['reasons']}"
)
ok.append("a transient failure clears on its own after the short backoff")

# The same age at the billing step is still held, because it does not self-heal.
reset()
fail("craigs2", "billing", NOW - timedelta(minutes=90))
with conn() as c:
    rep = q.evaluate_eligibility(c, ACCOUNTS, now=NOW)
assert not rep["accounts"]["craigs2"]["eligible"], \
    "a 90-minute-old billing failure cleared the 12-hour backoff"
ok.append("a billing failure is still held at 90 minutes")

# --- rotation ranks on last attempt, not last success -----------------------
# Without this, craigs1 keeps a null last_post_at, stays longest-idle forever,
# and takes every fire the moment its backoff lapses.
reset()
old = NOW - timedelta(hours=20)
with tx() as c:
    for a in ("craigs2", "craigs3", "craigs4"):
        c.execute(
            "INSERT INTO posts (post_id, account, title, posted_ts) "
            "VALUES (%s, %s, 't', %s)",
            (f"p-{a}", a, old + timedelta(minutes=ACCOUNTS.index(a))),
        )
fail("craigs1", "form_title", NOW - timedelta(minutes=30))

with conn() as c:
    rep = q.evaluate_eligibility(c, ACCOUNTS, now=NOW)
assert rep["accounts"]["craigs1"]["last_failure_at"] is not None
assert not rep["accounts"]["craigs1"]["eligible"], "craigs1 should still be backing off"
ok.append("a failure is recorded against the account that produced it")

# Once it lapses the account comes back on its own - no human, no kill switch.
with conn() as c:
    rep = q.evaluate_eligibility(c, ACCOUNTS, now=NOW + timedelta(minutes=45))
assert rep["accounts"]["craigs1"]["eligible"], (
    f"craigs1 did not recover after the backoff: "
    f"{rep['accounts']['craigs1']['reasons']}"
)
ok.append("the backoff expires by itself; it is not a kill switch")

# --- zero disables it -------------------------------------------------------
reset()
with tx() as c:
    c.execute("UPDATE guardrail_settings SET failure_backoff_minutes = 0 WHERE singleton")
fail("craigs3", "form_title", NOW - timedelta(minutes=1))
with conn() as c:
    rep = q.evaluate_eligibility(c, ACCOUNTS, now=NOW)
assert rep["accounts"]["craigs3"]["eligible"], "zero should disable the backoff"
ok.append("setting the backoff to zero turns it off")

with tx() as c:
    c.execute("UPDATE guardrail_settings SET failure_backoff_minutes = 60 WHERE singleton")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
