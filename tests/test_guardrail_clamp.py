"""Verify the compiled ceilings actually stop a bad server value."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from craigslist_auto.config import (  # noqa: E402
    CEILING_MAX_POSTS_PER_ACCOUNT_PER_DAY, CEILING_MAX_POSTS_PER_ACCOUNT_PER_WEEK,
    CEILING_MAX_POSTS_PER_DAY_TOTAL,
    FLOOR_MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT, clamp_guardrails, compiled_guardrails,
)

failures = []

# The scenario the ceilings exist for: a fat-fingered 30/day.
g, notes = clamp_guardrails({"max_posts_per_day_total": 30})
if g.max_posts_per_day_total != CEILING_MAX_POSTS_PER_DAY_TOTAL:
    failures.append(f"30/day was not clamped: got {g.max_posts_per_day_total}")
if not notes:
    failures.append("clamping produced no note, so nothing would be reported")

# Cooldown must not be shortened below the floor.
g, notes = clamp_guardrails({"min_hours_between_posts_same_account": 1})
if g.min_hours_between_posts_same_account != FLOOR_MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT:
    failures.append(f"1h cooldown not clamped up: {g.min_hours_between_posts_same_account}")

g, _ = clamp_guardrails({"max_posts_per_account_per_week": 999})
if g.max_posts_per_account_per_week != CEILING_MAX_POSTS_PER_ACCOUNT_PER_WEEK:
    failures.append("weekly cap not clamped")

# "Two per account per day" is the whole shape of the schedule — a morning ad
# and an afternoon one. A server asking for more is asking to change that, and
# unlike the other caps there is no legitimate reason to, so the ceiling sits
# exactly on the live value.
g, notes = clamp_guardrails({"max_posts_per_account_per_day": 6})
if g.max_posts_per_account_per_day != CEILING_MAX_POSTS_PER_ACCOUNT_PER_DAY:
    failures.append(
        f"per-account daily cap not clamped: got {g.max_posts_per_account_per_day}"
    )
if not notes:
    failures.append("per-account daily clamp produced no note")

# A legal tightening must pass through untouched and silently.
g, notes = clamp_guardrails({"max_posts_per_day_total": 2, "min_hours_between_posts_same_account": 24})
if g.max_posts_per_day_total != 2 or g.min_hours_between_posts_same_account != 24:
    failures.append(f"legal values were altered: {g}")
if notes:
    failures.append(f"legal values produced spurious clamp notes: {notes}")

# Missing keys fall back to the compiled defaults rather than None.
g, notes = clamp_guardrails({})
if g != compiled_guardrails():
    failures.append(f"empty payload did not fall back to compiled defaults: {g}")
if notes:
    failures.append("empty payload produced clamp notes")

# Window bounds.
g, _ = clamp_guardrails({"post_window_start_hour": 0, "post_window_end_hour": 24})
if g.post_window_start_hour < 6 or g.post_window_end_hour > 22:
    failures.append(f"window not clamped: {g.post_window_start_hour}-{g.post_window_end_hour}")

if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print("clamp OK: 30/day -> %d, 1h -> %dh, 999/wk -> %d, legal values untouched, "
      "empty payload falls back to compiled defaults"
      % (CEILING_MAX_POSTS_PER_DAY_TOTAL, FLOOR_MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT,
         CEILING_MAX_POSTS_PER_ACCOUNT_PER_WEEK))
