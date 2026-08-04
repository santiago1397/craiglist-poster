"""The projected calendar must obey the same rules the claim does.

A forecast that ignores a guardrail is worse than none — it would tell you 45
drafts clear in a week when the caps mean three weeks.
"""
from datetime import datetime, timedelta, timezone

from app.db import init_pool, tx
from app.services import drafts as drafts_svc
from app.services import queue as queue_svc

init_pool()
ok = []
ACCOUNTS = ["craigs1", "craigs2", "craigs3"]
# Thursday 2026-07-30, 07:00 ET — before the first fire of the day.
NOW = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)

with tx() as c:
    c.execute("TRUNCATE draft_images, images, drafts, posts, post_attempts CASCADE")
    c.execute("UPDATE guardrail_settings SET posting_enabled=TRUE, paused_at=NULL, "
              "max_posts_per_day_total=3, min_hours_between_posts_same_account=20, "
              "max_posts_per_account_per_week=7")
    for acct in ACCOUNTS:
        for i in range(10):
            drafts_svc.create_draft(c, {"account": acct, "title": f"{acct} #{i}",
                                        "body": "b", "body_head": "b"})

with tx() as c:
    plan = queue_svc.project_schedule(c, accounts=ACCOUNTS, horizon_days=21, now=NOW)

assert plan, "no schedule produced for 30 queued drafts"
ok.append(f"projected {len(plan)} posts over 21 days from 30 queued drafts")

# Chronological, and never more than 3 in any rolling 24h.
times = [p["at"] for p in plan]
assert times == sorted(times), "schedule is not in chronological order"
for i, t in enumerate(times):
    window = [x for x in times if t - timedelta(hours=24) < x <= t]
    assert len(window) <= 3, f"{len(window)} posts within 24h at {t}"
ok.append("never more than 3 posts in any rolling 24 hours")

# Per-account cooldown.
for acct in ACCOUNTS:
    mine = [p["at"] for p in plan if p["account"] == acct]
    for a, b in zip(mine, mine[1:]):
        gap = (b - a).total_seconds() / 3600
        assert gap >= 20, f"{acct} scheduled {gap:.1f}h apart, cooldown is 20h"
ok.append("each account respects its 20-hour cooldown")

# Weekdays only, and only at real task fire times, in the display timezone.
from app.config import get_settings  # noqa: E402

tz = get_settings().display_zoneinfo
for p in plan:
    local = p["at"].astimezone(tz)
    assert local.weekday() < 5, f"scheduled on a weekend: {local}"
    assert (local.hour, local.minute) in queue_svc.TASK_FIRE_TIMES, \
        f"not a task fire time: {local}"
ok.append("every slot is a real weekday task fire from TASK_FIRE_TIMES")

# Queue order is preserved within an account.
for acct in ACCOUNTS:
    titles = [p["title"] for p in plan if p["account"] == acct]
    assert titles == sorted(titles, key=lambda t: int(t.split("#")[1])), \
        f"{acct} drafts scheduled out of queue order: {titles}"
ok.append("drafts publish in queue order within each account")

# A recent post must push that account's first slot out by the cooldown.
with tx() as c:
    c.execute("INSERT INTO posts (post_id, account, posted_ts, source) "
              "VALUES ('p1','craigs1',%s,'test')", (NOW - timedelta(hours=2),))
    plan2 = queue_svc.project_schedule(c, accounts=ACCOUNTS, horizon_days=21, now=NOW)
first_c1 = next(p["at"] for p in plan2 if p["account"] == "craigs1")
assert (first_c1 - (NOW - timedelta(hours=2))).total_seconds() / 3600 >= 20, first_c1
ok.append("a post 2 hours ago pushes that account's next slot past the cooldown")

# not_before is honoured.
with tx() as c:
    c.execute("TRUNCATE drafts CASCADE")
    far = NOW + timedelta(days=10)
    drafts_svc.create_draft(c, {"account": "craigs1", "title": "later", "body": "b",
                                "not_before": far})
    drafts_svc.create_draft(c, {"account": "craigs2", "title": "now", "body": "b"})
    plan3 = queue_svc.project_schedule(c, accounts=ACCOUNTS, horizon_days=21, now=NOW)
later = next(p for p in plan3 if p["title"] == "later")
assert later["at"] >= far, f"not_before ignored: {later['at']} < {far}"
ok.append("a draft with not_before is never scheduled before that date")

# Paused posting means no forecast at all — showing times would be a lie.
with tx() as c:
    c.execute("UPDATE guardrail_settings SET posting_enabled=FALSE WHERE singleton")
    paused = queue_svc.project_schedule(c, accounts=ACCOUNTS, now=NOW)
    c.execute("UPDATE guardrail_settings SET posting_enabled=TRUE WHERE singleton")
assert paused == [], "produced a schedule while posting is paused"
ok.append("no schedule is projected while posting is paused")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
