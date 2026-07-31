"""Reading a live edit form has to leave evidence the operator can reach.

Hydration is the first thing anyone points at Craigslist's edit form and the
only thing that answers the question the whole feature rests on: do the
selectors in `editor.SEL` match the real DOM?

It was also the one path whose evidence never left the posting machine. The
desktop captured a screenshot, uploaded it, and recorded a per-selector census
on its step trail — and `record_content` kept the scraped fields and dropped
both. The census stayed in `logs/run.log` on a Windows box nobody is sitting at.

These assert the round trip: desktop event -> ingest -> the columns the post
detail page reads. The failure case matters more than the success case, because
a read that found nothing is exactly when you need the census and the picture.
"""
from datetime import datetime, timezone

from app.db import conn, init_pool, tx
from app.schemas.events import EditStep, PostContent
from app.services import ingest as ingest_svc

init_pool()
ok = []
failures = []


def check(label, condition, detail=""):
    (ok if condition else failures).append(
        label if condition else (f"{label}  [{detail}]" if detail else label)
    )


NOW = datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 7, 31, 22, 0, tzinfo=timezone.utc)
PID = "7899999999"
CENSUS = "edit_title=1 edit_body=1 edit_city=0 save_button=2"

with tx() as c:
    c.execute("DELETE FROM posts WHERE post_id = %s", (PID,))
    c.execute(
        "INSERT INTO posts (post_id, account, title, url, posted_ts, county, "
        "service_offered) VALUES (%s,'craigs1','t','u',%s,'Broward','Roofing')",
        (PID, NOW),
    )
    c.execute("UPDATE posts SET hydrate_requested_at = NOW() WHERE post_id = %s", (PID,))

# --- a successful read ------------------------------------------------------
with tx() as c:
    ingest_svc.ingest_events(c, [PostContent(
        ts=NOW, machine="desktop-eseva3c", account="craigs1", post_id=PID, ok=True,
        title="live title", body="live body",
        # The edit form's "city" input is Craigslist's free-text area box, which
        # routinely names several towns. It must survive verbatim.
        city="Davie, Plantation",
        content_hash="h1", artifact_ids=["abc12345deadbeef"],
        steps=[
            EditStep(name="open_edit_form", ok=True, duration_seconds=1.2),
            EditStep(name="selectors", ok=True, note=CENSUS),
        ],
    )])

with conn() as c:
    r = c.execute(
        "SELECT hydrated_at, hydrate_requested_at, county, service_offered, city, "
        "hydrate_steps, hydrate_artifact_ids FROM posts WHERE post_id = %s", (PID,)
    ).fetchone()

check("hydration is recorded", r["hydrated_at"] is not None)
check("the pending request is cleared", r["hydrate_requested_at"] is None)
check("the selector census reaches the dashboard",
      any(s.get("note") == CENSUS for s in r["hydrate_steps"]), str(r["hydrate_steps"]))
check("the artifact is linked to the post",
      r["hydrate_artifact_ids"] == ["abc12345deadbeef"], str(r["hydrate_artifact_ids"]))
check("the area box is stored verbatim", r["city"] == "Davie, Plantation", r["city"])
# The form exposes no control for either, so the desktop reports nothing for
# them. Writing that through would erase what the posting flow recorded at
# publish time — the only place these values are ever known.
check("county survives a read that cannot see it", r["county"] == "Broward", r["county"])
check("service type survives a read that cannot see it",
      r["service_offered"] == "Roofing", r["service_offered"])

# --- a read that found nothing ---------------------------------------------
with tx() as c:
    ingest_svc.ingest_events(c, [PostContent(
        ts=LATER, machine="desktop-eseva3c", account="craigs1", post_id=PID, ok=False,
        error_type="selector_miss", error_message="no title input",
        artifact_ids=["ffff0000cafebabe"],
        steps=[EditStep(name="selectors", ok=True, note="edit_title=0")],
    )])

with conn() as c:
    r2 = c.execute(
        "SELECT hydrate_error, hydrate_steps, hydrate_artifact_ids "
        "FROM posts WHERE post_id = %s", (PID,)
    ).fetchone()

check("a failed read keeps its census",
      any(s.get("note") == "edit_title=0" for s in r2["hydrate_steps"]),
      str(r2["hydrate_steps"]))
check("a failed read keeps its screenshot",
      r2["hydrate_artifact_ids"] == ["ffff0000cafebabe"], str(r2["hydrate_artifact_ids"]))
check("the failure reason is recorded",
      "selector_miss" in (r2["hydrate_error"] or ""), str(r2["hydrate_error"]))

# --- out-of-order delivery must not resurrect an older read ----------------
with tx() as c:
    ingest_svc.ingest_events(c, [PostContent(
        ts=NOW, machine="desktop-eseva3c", account="craigs1", post_id=PID, ok=True,
        title="stale title", body="stale body", content_hash="h0",
        steps=[EditStep(name="selectors", ok=True, note="stale census")],
    )])
with conn() as c:
    r3 = c.execute(
        "SELECT hydrate_steps FROM posts WHERE post_id = %s", (PID,)
    ).fetchone()
check("a stale event cannot overwrite newer evidence",
      not any(s.get("note") == "stale census" for s in r3["hydrate_steps"]),
      str(r3["hydrate_steps"]))

with tx() as c:
    c.execute("DELETE FROM posts WHERE post_id = %s", (PID,))

if failures:
    print("\n".join(f"  --  {f}" for f in failures))
    print(f"\n{len(failures)} FAILED, {len(ok)} passed")
    raise SystemExit(1)
print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
print("hydration evidence OK")
