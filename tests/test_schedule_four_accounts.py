"""Four accounts, eight ads a day, two per account — morning and afternoon.

This is the test that validates the throughput change, and it exists because the
obvious configuration is wrong in a way nothing else catches.

`max_posts_per_day_total` is counted over a **rolling 24 hours**, not a calendar
day. Posts land at (or just after) their scheduled fire, so at every fire in
steady state yesterday's posts from that hour onward, plus today's so far,
already total eight. Set the cap to 8 — the number of ads you actually want —
and every fire is refused, all day, looking exactly like a broken scheduler. The
cap has to be the calendar target plus one. Same for the weekly cap: 10 posts a
week per account needs 11.

`project_schedule` replays the real claim decision against the real fire grid, so
asserting on its output exercises that arithmetic without publishing anything.
Run it after touching TASK_FIRE_HOURS, any posting guardrail, or the eligibility
rules.

Needs a scratch database — see tests/README.md. It TRUNCATEs.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import init_pool, tx
from app.services import drafts as drafts_svc
from app.services import queue as queue_svc

init_pool()
ok = []
tz = get_settings().display_zoneinfo

ACCOUNTS = ["craigs1", "craigs2", "craigs3", "craigs4"]
# Thursday 2026-07-30, 07:00 ET — before the day's first fire at 08:00, so the
# first projected day is a complete one.
NOW = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)
HORIZON = 21

# The live configuration, set explicitly rather than inherited from the
# migration, so this test states the design it is checking.
COOLDOWN_HOURS = 5
PER_ACCOUNT_PER_DAY = 2
PER_DAY_TOTAL = 9        # rolling 24h -> 8 calendar posts
PER_ACCOUNT_PER_WEEK = 11  # rolling 7d  -> 10 calendar posts

with tx() as c:
    c.execute("TRUNCATE draft_images, images, drafts, posts, post_attempts CASCADE")
    c.execute(
        "UPDATE guardrail_settings SET posting_enabled=TRUE, paused_at=NULL, "
        "max_posts_per_day_total=%s, max_posts_per_account_per_day=%s, "
        "min_hours_between_posts_same_account=%s, max_posts_per_account_per_week=%s, "
        "post_window_start_hour=8, post_window_end_hour=18, post_weekdays_only=TRUE",
        (PER_DAY_TOTAL, PER_ACCOUNT_PER_DAY, COOLDOWN_HOURS, PER_ACCOUNT_PER_WEEK),
    )
    # Deeper than the horizon can consume, so the forecast is bounded by the
    # guardrails rather than by running out of drafts — otherwise a cap that
    # wrongly blocks fires would hide behind an empty queue.
    for acct in ACCOUNTS:
        for i in range(40):
            drafts_svc.create_draft(c, {"account": acct, "title": f"{acct} #{i:02d}",
                                        "body": "b", "body_head": "b"})

with tx() as c:
    plan = queue_svc.project_schedule(c, accounts=ACCOUNTS, horizon_days=HORIZON, now=NOW)

assert plan, "no schedule produced for 160 queued drafts"
ok.append(f"projected {len(plan)} posts over {HORIZON} days across 4 accounts")

for p in plan:
    local = p["at"].astimezone(tz)
    assert local.weekday() < 5, f"scheduled on a weekend: {local}"
    assert (local.hour, local.minute) in queue_svc.TASK_FIRE_TIMES, \
        f"not a task fire time: {local}"
ok.append("every slot is a real weekday fire from TASK_FIRE_TIMES")

by_date: dict[object, list] = defaultdict(list)
for p in plan:
    by_date[p["at"].astimezone(tz).date()].append(p)

# The horizon can cut the final day in half, so it is not expected to be full.
# Every day before it is.
full_days = sorted(by_date)[:-1]
assert full_days, "not enough days projected to check a full one"

for day in full_days:
    posts = by_date[day]
    assert len(posts) == 8, (
        f"{day} has {len(posts)} posts, expected 8. If this says 7 with a hole, "
        f"max_posts_per_day_total is one too low — it is a rolling 24h count, "
        f"so 8 ads a day needs 9."
    )
ok.append(f"every full weekday carries exactly 8 posts ({len(full_days)} days checked)")

for day in full_days:
    per_account: dict[str, list] = defaultdict(list)
    for p in by_date[day]:
        per_account[p["account"]].append(p["at"].astimezone(tz))
    assert set(per_account) == set(ACCOUNTS), (
        f"{day} did not serve every account: {sorted(per_account)}"
    )
    for acct, times in per_account.items():
        assert len(times) == PER_ACCOUNT_PER_DAY, (
            f"{acct} has {len(times)} posts on {day}, expected {PER_ACCOUNT_PER_DAY}"
        )
        morning = [t for t in times if t.hour < 12]
        afternoon = [t for t in times if t.hour >= 12]
        assert len(morning) == 1 and len(afternoon) == 1, (
            f"{acct} on {day} is not one morning + one afternoon: "
            f"{[t.strftime('%H:%M') for t in times]}"
        )
ok.append("every account posts exactly twice a day — once morning, once afternoon")

for acct in ACCOUNTS:
    mine = [p["at"] for p in plan if p["account"] == acct]
    for a, b in zip(mine, mine[1:]):
        gap = (b - a).total_seconds() / 3600
        assert gap >= COOLDOWN_HOURS, (
            f"{acct} scheduled {gap:.1f}h apart, cooldown is {COOLDOWN_HOURS}h"
        )
ok.append(f"each account respects its {COOLDOWN_HOURS}-hour cooldown")

# The rolling windows themselves, checked the way the server counts them.
times = sorted(p["at"] for p in plan)
assert times == [p["at"] for p in plan], "schedule is not in chronological order"
for t in times:
    window = [x for x in times if t - timedelta(hours=24) < x <= t]
    assert len(window) <= PER_DAY_TOTAL, (
        f"{len(window)} posts within a rolling 24h at {t}, cap is {PER_DAY_TOTAL}"
    )
for acct in ACCOUNTS:
    mine = [p["at"] for p in plan if p["account"] == acct]
    for t in mine:
        window = [x for x in mine if t - timedelta(days=7) < x <= t]
        assert len(window) <= PER_ACCOUNT_PER_WEEK, (
            f"{acct} has {len(window)} posts in a rolling 7 days at {t}"
        )
ok.append("rolling 24h and 7d caps hold at every projected post")

# Queue order is preserved within an account — two posts a day must not reorder
# what Review shows you.
for acct in ACCOUNTS:
    titles = [p["title"] for p in plan if p["account"] == acct]
    assert titles == sorted(titles), f"{acct} scheduled out of queue order: {titles}"
ok.append("drafts publish in queue order within each account")

# The per-account daily cap must bind on its own, not merely be implied by the
# cooldown — that is the whole reason it exists. Widen the window past the point
# where the arithmetic protects us and it must still hold.
with tx() as c:
    c.execute("UPDATE guardrail_settings SET post_window_end_hour=22 WHERE singleton")
    wide = queue_svc.project_schedule(c, accounts=ACCOUNTS, horizon_days=7, now=NOW)
    c.execute("UPDATE guardrail_settings SET post_window_end_hour=18 WHERE singleton")

wide_by_acct_day: dict[tuple, int] = defaultdict(int)
for p in wide:
    local = p["at"].astimezone(tz)
    wide_by_acct_day[(p["account"], local.date())] += 1
worst = max(wide_by_acct_day.values())
assert worst <= PER_ACCOUNT_PER_DAY, (
    f"widening the posting window let an account post {worst} times in a day; "
    f"the per-account daily cap is not actually binding"
)
ok.append("the per-account daily cap still holds when the window is widened")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
