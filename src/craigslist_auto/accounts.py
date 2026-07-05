from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import (
    ACCOUNTS,
    Account,
    MAX_POSTS_PER_ACCOUNT_PER_WEEK,
    MAX_POSTS_PER_DAY_TOTAL,
    MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT,
    POST_WEEKDAYS_ONLY,
    POST_WINDOW_END_HOUR,
    POST_WINDOW_START_HOUR,
    STATE_FILE,
)

# All eligibility math (window + weekday) is *local* to the posting machine.
# The dashboard renders in ET (America/New_York) per the product decision.
LOCAL_TZ = ZoneInfo("America/New_York")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"posts": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def record_post(account: Account, ad_title: str, post_url: str | None) -> None:
    state = _load_state()
    state.setdefault("posts", []).append(
        {
            "account": account.name,
            "at": _now().isoformat(),
            "title": ad_title,
            "url": post_url,
            "ghosted": None,  # filled later by ghost-check
        }
    )
    _save_state(state)


def mark_ghosted(account_name: str, at_iso: str, ghosted: bool) -> None:
    state = _load_state()
    for p in state.get("posts", []):
        if p["account"] == account_name and p["at"] == at_iso:
            p["ghosted"] = ghosted
            break
    _save_state(state)


def _last_post_for(account_name: str) -> datetime | None:
    state = _load_state()
    posts = [p for p in state.get("posts", []) if p["account"] == account_name]
    if not posts:
        return None
    return max(datetime.fromisoformat(p["at"]) for p in posts)


def _posts_in_last_24h_total() -> int:
    state = _load_state()
    cutoff = _now() - timedelta(hours=24)
    return sum(1 for p in state.get("posts", []) if datetime.fromisoformat(p["at"]) >= cutoff)


def _posts_in_last_week(account_name: str) -> int:
    state = _load_state()
    cutoff = _now() - timedelta(days=7)
    return sum(
        1
        for p in state.get("posts", [])
        if p["account"] == account_name and datetime.fromisoformat(p["at"]) >= cutoff
    )


def _in_posting_window() -> bool:
    h = datetime.now().hour
    return POST_WINDOW_START_HOUR <= h < POST_WINDOW_END_HOUR


def _is_allowed_weekday() -> bool:
    # Monday=0 .. Sunday=6
    if not POST_WEEKDAYS_ONLY:
        return True
    return datetime.now().weekday() < 5


def eligibility_report() -> list[dict]:
    """Diagnostic — who can post right now and why not."""
    out = []
    weekend_block = not _is_allowed_weekday()
    for a in ACCOUNTS:
        reasons = []
        if weekend_block:
            reasons.append("weekend: posting restricted to Mon-Fri")
        last = _last_post_for(a.name)
        if last is not None:
            hrs = (_now() - last).total_seconds() / 3600
            if hrs < MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT:
                reasons.append(
                    f"cooldown: {hrs:.1f}h since last (need {MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT}h)"
                )
        wk = _posts_in_last_week(a.name)
        if wk >= MAX_POSTS_PER_ACCOUNT_PER_WEEK:
            reasons.append(f"weekly cap: {wk}/{MAX_POSTS_PER_ACCOUNT_PER_WEEK}")
        out.append({"account": a.name, "eligible": not reasons, "reasons": reasons})
    return out


def _next_window_open(after: datetime) -> datetime:
    """Return the next datetime >= `after` (in LOCAL_TZ) that sits inside the
    posting window (weekday + hour range)."""
    local = after.astimezone(LOCAL_TZ)
    cursor = local
    for _ in range(14):  # bounded search — worst case a weekend + 1
        # Advance to a weekday if needed
        if POST_WEEKDAYS_ONLY and cursor.weekday() >= 5:
            cursor = cursor.replace(hour=POST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
            cursor = cursor + timedelta(days=(7 - cursor.weekday()))
            continue
        # Within-day: bump forward if we're before/after the hour range
        if cursor.hour < POST_WINDOW_START_HOUR:
            cursor = cursor.replace(hour=POST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
            return cursor.astimezone(timezone.utc)
        if cursor.hour >= POST_WINDOW_END_HOUR:
            # tomorrow's opening
            cursor = (cursor + timedelta(days=1)).replace(
                hour=POST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
            )
            continue
        # Already inside the window
        return cursor.astimezone(timezone.utc)
    # Give up gracefully — return the raw cursor
    return cursor.astimezone(timezone.utc)


def next_eligible_at(account_name: str) -> datetime | None:
    """Earliest future UTC timestamp when this account can post again, or None
    if it will be blocked for >1 week (weekly cap not clearing soon)."""
    now = _now()

    # Cooldown-based earliest
    last = _last_post_for(account_name)
    cooldown_open = last + timedelta(hours=MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT) if last else now

    # Weekly-cap check — if we're already at cap, next slot opens when the
    # oldest post in the trailing 7d falls off.
    state = _load_state()
    my_posts = sorted(
        (datetime.fromisoformat(p["at"]) for p in state.get("posts", []) if p["account"] == account_name),
        reverse=True,
    )
    weekly_open = now
    if len(my_posts) >= MAX_POSTS_PER_ACCOUNT_PER_WEEK:
        oldest_in_window = my_posts[MAX_POSTS_PER_ACCOUNT_PER_WEEK - 1]
        weekly_open = oldest_in_window + timedelta(days=7)

    earliest = max(now, cooldown_open, weekly_open)
    return _next_window_open(earliest)


def describe_block_reasons(account_name: str) -> list[str]:
    """Human-readable reasons this account can't post *right now*.
    Empty list == eligible."""
    reasons: list[str] = []
    if not _is_allowed_weekday():
        reasons.append("weekend: posting restricted to Mon-Fri")
    if not _in_posting_window():
        reasons.append(f"outside posting window ({POST_WINDOW_START_HOUR:02d}-{POST_WINDOW_END_HOUR:02d})")
    last = _last_post_for(account_name)
    if last is not None:
        hrs = (_now() - last).total_seconds() / 3600
        if hrs < MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT:
            reasons.append(f"cooldown: {hrs:.1f}h since last (need {MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT}h)")
    wk = _posts_in_last_week(account_name)
    if wk >= MAX_POSTS_PER_ACCOUNT_PER_WEEK:
        reasons.append(f"weekly cap: {wk}/{MAX_POSTS_PER_ACCOUNT_PER_WEEK}")
    if _posts_in_last_24h_total() >= MAX_POSTS_PER_DAY_TOTAL:
        reasons.append(f"daily cap total: {_posts_in_last_24h_total()}/{MAX_POSTS_PER_DAY_TOTAL}")
    return reasons


def account_snapshot(account_name: str) -> dict:
    """Data payload for AccountState events."""
    last = _last_post_for(account_name)
    state = _load_state()
    last_post = None
    for p in reversed(state.get("posts", [])):
        if p["account"] == account_name:
            last_post = p
            break
    return {
        "eligible_now": not describe_block_reasons(account_name),
        "next_eligible_at": next_eligible_at(account_name),
        "block_reasons": describe_block_reasons(account_name),
        "posts_last_24h_total": _posts_in_last_24h_total(),
        "posts_last_7d_this_account": _posts_in_last_week(account_name),
        "last_post_at": last,
        "last_post_url": (last_post or {}).get("url"),
    }


def pick_next_account(machine_name: str) -> Account | None:
    """Return the account that should post next from this machine, or None."""
    if not _in_posting_window():
        return None
    if not _is_allowed_weekday():
        return None
    if _posts_in_last_24h_total() >= MAX_POSTS_PER_DAY_TOTAL:
        return None

    candidates: list[tuple[Account, datetime | None]] = []
    for a in ACCOUNTS:
        if a.allowed_machine != machine_name:
            continue
        if _posts_in_last_week(a.name) >= MAX_POSTS_PER_ACCOUNT_PER_WEEK:
            continue
        last = _last_post_for(a.name)
        if last is not None:
            hrs = (_now() - last).total_seconds() / 3600
            if hrs < MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT:
                continue
        candidates.append((a, last))

    if not candidates:
        return None

    # Prefer the account that has gone longest without posting
    candidates.sort(key=lambda t: t[1] or datetime.min.replace(tzinfo=timezone.utc))
    return candidates[0][0]
