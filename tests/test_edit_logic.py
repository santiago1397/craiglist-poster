"""End-to-end exercise of the post-editing logic against a real Postgres.

Covers what only fails at runtime: the SQL, the desired-state revision model,
the optimistic-concurrency token, the claim's row lock, and — most importantly —
decision 32's routing, where a failure that already mutated a live posting must
never be quietly retried.
"""
from datetime import datetime, timedelta, timezone

from app.db import init_pool, tx
from app.services import edits as edits_svc
from app.services import ingest as ingest_svc
from app.schemas.events import PostContent, PostEditAttempt

init_pool()

# Thursday 2026-07-30 14:00 America/New_York — inside the edit window.
NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
ACCOUNTS = ["craigs1", "craigs2", "craigs3"]
ok = []
failures = []


def check(label, condition, detail=""):
    if condition:
        ok.append(label)
    else:
        failures.append(f"{label}{': ' + detail if detail else ''}")


def reset(enable_edits=True):
    with tx() as c:
        c.execute(
            "TRUNCATE post_desired_state, post_edit_attempts, posts, "
            "post_attempts, ghost_checks, flow_errors, artifacts CASCADE"
        )
        c.execute(
            "UPDATE guardrail_settings SET "
            "edits_enabled = %s, min_hours_between_edits_same_post = 48, "
            "max_edits_per_account_per_day = 3, max_edits_per_post_lifetime = 5, "
            "edit_window_start_hour = 0, edit_window_end_hour = 24, "
            "posting_enabled = TRUE, paused_at = NULL, paused_reason = NULL, "
            "edits_paused_reason = NULL",
            (enable_edits,),
        )


def add_post(post_id, account="craigs1", when=None):
    with tx() as c:
        c.execute(
            "INSERT INTO posts (post_id, account, title, url, posted_ts, source) "
            "VALUES (%s,%s,%s,%s,%s,'test')",
            (post_id, account, "live title", f"https://x/{post_id}.html", when or NOW),
        )


def hydrate(post_id, account="craigs1", *, body="live body", ts=None, ok_=True):
    ev = PostContent(
        ts=ts or NOW, machine="test", account=account, post_id=post_id, ok=ok_,
        title="live title", body=body, city="Hollywood", county="Broward",
        postal_code="33020", license_number="CCC1", phone_number="954",
        service_offered="Roofing", content_hash=f"hash-of-{body}",
    )
    with tx() as c:
        ingest_svc.ingest_events(c, [ev])


def attempt(post_id, outcome, *, account="craigs1", failed_step=None, rev=None, ts=None):
    ev = PostEditAttempt(
        ts=ts or NOW, machine="test", account=account, post_id=post_id,
        outcome=outcome, failed_step=failed_step, desired_rev=rev, applied_rev=rev,
        error_message="boom" if failed_step else None,
    )
    with tx() as c:
        ingest_svc.ingest_events(c, [ev])
    return ev


# ---------------------------------------------------------------------------
# Editing requires hydration first (decision 23)
# ---------------------------------------------------------------------------
reset()
add_post("1001")
try:
    with tx() as c:
        edits_svc.upsert_desired(c, "1001", {"title": "new"})
    check("un-hydrated post refuses edits", False, "upsert succeeded before hydration")
except ValueError:
    check("un-hydrated post refuses edits", True)

hydrate("1001")
with tx() as c:
    d = edits_svc.upsert_desired(c, "1001", {"title": "new title"})
check("first edit creates desired state", d["desired_rev"] == 1 and d["live_rev"] == 0)
check("base_hash captured from hydration", d["base_hash"] == "hash-of-live body")
check("unset fields seeded from live post", d["body"] == "live body",
      f"body was {d['body']!r}")

# ---------------------------------------------------------------------------
# Editing twice supersedes rather than queueing twice (decision 25)
# ---------------------------------------------------------------------------
with tx() as c:
    d = edits_svc.upsert_desired(c, "1001", {"title": "newer title"})
check("second edit bumps the revision", d["desired_rev"] == 2)
with tx() as c:
    n = c.execute("SELECT COUNT(*) AS n FROM post_desired_state").fetchone()["n"]
check("second edit did not create a second row", n == 1, f"rows={n}")

# ---------------------------------------------------------------------------
# Claim is atomic and hands back what the desktop needs
# ---------------------------------------------------------------------------
with tx() as c:
    claimed = edits_svc.claim_reconcile(c, machine="m1", post_id="1001")
check("claim returns the desired state", claimed is not None and claimed["title"] == "newer title")
check("claim carries the live url", claimed and claimed["url"] == "https://x/1001.html")
check("claim marks it applying", claimed and claimed["status"] == "applying")

with tx() as c:
    again = edits_svc.claim_reconcile(c, machine="m2", post_id="1001")
check("a second machine cannot claim the same edit", again is None)

# ---------------------------------------------------------------------------
# Applied advances live_rev; the post stops being pending
# ---------------------------------------------------------------------------
attempt("1001", "applied", rev=2)
with tx() as c:
    d = edits_svc.get_desired(c, "1001")
check("applied sets status", d["status"] == "applied", d["status"])
check("applied advances live_rev", d["live_rev"] == 2, str(d["live_rev"]))
check("applied clears the claim", d["claimed_by_machine"] is None)

# ---------------------------------------------------------------------------
# Decision 32 — routing depends on how far the reconcile got
# ---------------------------------------------------------------------------
reset()
add_post("2001")
hydrate("2001")
with tx() as c:
    edits_svc.upsert_desired(c, "2001", {"title": "x"})
    edits_svc.claim_reconcile(c, machine="m1", post_id="2001")
attempt("2001", "failed_other", failed_step="open_edit_form")
with tx() as c:
    d = edits_svc.get_desired(c, "2001")
check("pre-mutation failure returns to pending", d["status"] == "pending", d["status"])

reset()
add_post("2002")
hydrate("2002")
with tx() as c:
    edits_svc.upsert_desired(c, "2002", {"title": "x"})
    edits_svc.claim_reconcile(c, machine="m1", post_id="2002")
attempt("2002", "failed_other", failed_step="images_upload")
with tx() as c:
    d = edits_svc.get_desired(c, "2002")
check("post-mutation failure does NOT auto-retry", d["status"] == "failed", d["status"])

reset()
add_post("2003")
hydrate("2003")
with tx() as c:
    edits_svc.upsert_desired(c, "2003", {"title": "x"})
    edits_svc.claim_reconcile(c, machine="m1", post_id="2003")
attempt("2003", "degraded_live", failed_step="images_upload")
with tx() as c:
    d = edits_svc.get_desired(c, "2003")
check("degraded_live is preserved as its own status", d["status"] == "degraded_live", d["status"])

reset()
add_post("2004")
hydrate("2004")
with tx() as c:
    edits_svc.upsert_desired(c, "2004", {"title": "x"})
    edits_svc.claim_reconcile(c, machine="m1", post_id="2004")
attempt("2004", "failed_stale", failed_step="verify_hash")
with tx() as c:
    d = edits_svc.get_desired(c, "2004")
check("stale content parks rather than clobbers", d["status"] == "parked_stale", d["status"])

# A replayed event must not re-park an edit that has since been requeued.
with tx() as c:
    edits_svc.requeue_desired(c, "2004")
ev = PostEditAttempt(
    ts=NOW, machine="test", account="craigs1", post_id="2004",
    outcome="failed_stale", failed_step="verify_hash",
)
with tx() as c:
    ingest_svc.ingest_events(c, [ev])
    ingest_svc.ingest_events(c, [ev])   # duplicate delivery
    d = edits_svc.get_desired(c, "2004")
with tx() as c:
    n = c.execute(
        "SELECT COUNT(*) AS n FROM post_edit_attempts WHERE post_id = '2004'"
    ).fetchone()["n"]
check("duplicate attempt events are ignored", n == 2, f"rows={n}")

# ---------------------------------------------------------------------------
# Hydration is idempotent and never regresses (out-of-order outbox delivery)
# ---------------------------------------------------------------------------
reset()
add_post("3001")
hydrate("3001", body="newer body", ts=NOW)
hydrate("3001", body="older body", ts=NOW - timedelta(hours=1))
with tx() as c:
    row = c.execute("SELECT body FROM posts WHERE post_id = '3001'").fetchone()
check("a stale hydration cannot overwrite a newer one", row["body"] == "newer body",
      f"body={row['body']!r}")

# ---------------------------------------------------------------------------
# Guardrails (decision 30) — and the master switch
# ---------------------------------------------------------------------------
reset(enable_edits=False)
add_post("4001")
hydrate("4001")
with tx() as c:
    edits_svc.upsert_desired(c, "4001", {"title": "x"})
    report = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=NOW)
check("edits disabled blocks every account",
      all(not i["eligible"] for i in report["accounts"].values()))
check("edits disabled is explained once, globally",
      any("editing is disabled" in b for b in report["global_blocks"]),
      str(report["global_blocks"]))

reset()
add_post("4002")
hydrate("4002")
with tx() as c:
    edits_svc.upsert_desired(c, "4002", {"title": "x"})
    c.execute("UPDATE guardrail_settings SET posting_enabled = FALSE, "
              "paused_reason = 'holiday', edits_enabled = TRUE")
    report = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=NOW)
# The two switches are independent. Pausing posting used to pause editing as
# well, which meant "stop posting while I fix an ad" was impossible and Settings
# could show editing enabled while nothing would ever edit.
check("pausing posting does not pause editing",
      not any("paused" in b for b in report["global_blocks"]),
      str(report["global_blocks"]))
with tx() as c:
    c.execute("UPDATE guardrail_settings SET edits_enabled = FALSE")
    report = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=NOW)
check("editing has its own switch and it still stops everything",
      any("editing is disabled" in b for b in report["global_blocks"]),
      str(report["global_blocks"]))
with tx() as c:
    c.execute("UPDATE guardrail_settings SET posting_enabled = TRUE, "
              "paused_reason = NULL, edits_enabled = TRUE")

# Failed attempts consume a slot (decision 31), or a broken selector loops all day.
reset()
add_post("5001")
hydrate("5001")
with tx() as c:
    edits_svc.upsert_desired(c, "5001", {"title": "x"})
for i in range(3):
    attempt("5001", "failed_form", failed_step="fill_title",
            ts=NOW - timedelta(minutes=i))
with tx() as c:
    report = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=NOW)
check("failed attempts count against the daily cap",
      not report["accounts"]["craigs1"]["eligible"]
      and any("daily edit cap" in r for r in report["accounts"]["craigs1"]["reasons"]),
      str(report["accounts"]["craigs1"]["reasons"]))

# A dry run must NOT consume a slot, or rehearsing would ration you out of the
# only safe way to verify the feature.
reset()
add_post("5002")
hydrate("5002")
with tx() as c:
    edits_svc.upsert_desired(c, "5002", {"title": "x"})
for i in range(3):
    attempt("5002", "dry_run", ts=NOW - timedelta(minutes=i))
with tx() as c:
    report = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=NOW)
check("dry runs do not consume the daily cap",
      report["accounts"]["craigs1"]["eligible"],
      str(report["accounts"]["craigs1"]["reasons"]))

# Per-post cooldown keeps the same posting from being edited repeatedly.
reset()
add_post("6001")
hydrate("6001")
with tx() as c:
    edits_svc.upsert_desired(c, "6001", {"title": "x"})
attempt("6001", "applied", rev=1, ts=NOW - timedelta(hours=1))
with tx() as c:
    edits_svc.upsert_desired(c, "6001", {"title": "y"})
    work = edits_svc.pending_work(c, accounts=ACCOUNTS, now=NOW)
check("a post edited an hour ago is not offered again",
      not any(r["post_id"] == "6001" for r in work["reconcile"]),
      str(work["reconcile"]))

with tx() as c:
    work = edits_svc.pending_work(
        c, accounts=ACCOUNTS, now=NOW + timedelta(hours=49)
    )
check("it becomes available once the cooldown passes",
      any(r["post_id"] == "6001" for r in work["reconcile"]),
      str(work["reconcile"]))

# ---------------------------------------------------------------------------
# Stale claims are recoverable
# ---------------------------------------------------------------------------
reset()
add_post("7001")
hydrate("7001")
with tx() as c:
    edits_svc.upsert_desired(c, "7001", {"title": "x"})
    edits_svc.claim_reconcile(c, machine="dead", post_id="7001")
    c.execute("UPDATE post_desired_state SET claimed_at = NOW() - INTERVAL '2 hours'")
    n = edits_svc.release_stale_claims(c)
    d = edits_svc.get_desired(c, "7001")
check("a claim from a dead machine is released", n == 1 and d["status"] == "pending",
      f"released={n} status={d['status']}")

# ---------------------------------------------------------------------------
# Hydration requests are surfaced even when editing is disabled (decision 24)
# ---------------------------------------------------------------------------
reset(enable_edits=False)
add_post("8001")
with tx() as c:
    edits_svc.request_hydration(c, "8001")
    work = edits_svc.pending_work(c, accounts=ACCOUNTS, now=NOW)
check("hydration is offered even with editing disabled",
      any(h["post_id"] == "8001" for h in work["hydrate"]), str(work["hydrate"]))
check("but no reconcile work is offered", work["reconcile"] == [])

hydrate("8001")
with tx() as c:
    row = c.execute(
        "SELECT hydrate_requested_at, hydrated_at FROM posts WHERE post_id='8001'"
    ).fetchone()
check("hydration clears the pending request",
      row["hydrate_requested_at"] is None and row["hydrated_at"] is not None)

reset()

# --- a live posting is held to the same body limit as a draft ---------------
# `upsert_desired` used to accept any length, so a body Craigslist refuses could
# be staged on a live ad and only fail on the desktop, mid-edit, against the
# real listing — the most expensive place to find out.
from app.services.drafts import POSTING_BODY_LIMIT  # noqa: E402

add_post("8100")
hydrate("8100")
with tx() as c:
    try:
        edits_svc.upsert_desired(c, "8100", {"body": "x" * (POSTING_BODY_LIMIT + 1)})
        check("an over-length body is refused on a live post", False, "no error")
    except ValueError as e:
        check("an over-length body is refused on a live post",
              "limit is" in str(e), str(e))
with tx() as c:
    d = edits_svc.upsert_desired(c, "8100", {"body": "y" * (POSTING_BODY_LIMIT - 10)})
check("a body inside the limit is accepted", d["body"].startswith("y"))

reset()

# --- the posting-slot guard is reported, not just enforced ------------------
# The desktop refuses to start a reconcile within ten minutes of 9, 1 or 5
# (decision 28). The server did not know, so pressing Apply now in that window
# looked exactly like a dead button: accepted, silence, then a twenty-minute
# expiry. It is a safety rule rather than pacing, so a requested edit does not
# overrule it either.
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _Z

_ET = _Z("America/New_York")
reset()
add_post("9001")
hydrate("9001")
with tx() as c:
    edits_svc.upsert_desired(c, "9001", {"title": "x"})

    near = _dt(2026, 7, 30, 13, 2, tzinfo=_ET).astimezone(timezone.utc)
    rep = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=near)
    check("a posting slot blocks editing",
          any("posting slot" in b for b in rep["global_blocks"]),
          str(rep["global_blocks"]))

    rep = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=near, ignore_window=True)
    check("Apply now does not overrule the posting slot",
          any("posting slot" in b for b in rep["global_blocks"]),
          str(rep["global_blocks"]))

    clear = _dt(2026, 7, 30, 11, 30, tzinfo=_ET).astimezone(timezone.utc)
    rep = edits_svc.evaluate_edit_eligibility(c, ACCOUNTS, now=clear)
    check("away from a slot there is no such block",
          not any("posting slot" in b for b in rep["global_blocks"]),
          str(rep["global_blocks"]))

reset()


print(f"{len(ok)} checks passed")
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print("edit logic OK")
