from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
PHOTOS_DIR = DATA_DIR / "photos"
COVERS_DIR = DATA_DIR / "covers"
EXCEL_PATH = DATA_DIR / "ads.xlsx"
PROFILES_DIR = ROOT / "profiles"
LOGS_DIR = ROOT / "logs"
STATE_FILE = LOGS_DIR / "state.json"
POST_LOG = LOGS_DIR / "posts.jsonl"
GHOST_LOG = LOGS_DIR / "ghost_checks.jsonl"

CL_SITE = "https://miami.craigslist.org"
CL_POST_URL = "https://post.craigslist.org/c/mia"
CL_SEARCH_URL = "https://miami.craigslist.org/search/sss"

# Anti-ban guardrails. Do NOT raise these without proving accounts survive.
MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT = 20
MAX_POSTS_PER_DAY_TOTAL = 3
MAX_POSTS_PER_ACCOUNT_PER_WEEK = 7

# Posting window (local time, 24h). Roofing buyers browse during business hours.
POST_WINDOW_START_HOUR = 8
POST_WINDOW_END_HOUR = 19

# Restrict posting to Mon-Fri (local time). Roofing buyers are B2C but lead
# follow-up happens during the business week; weekend posts also stand out as
# automation when the account otherwise only posts on weekdays.
POST_WEEKDAYS_ONLY = True


# ---------------------------------------------------------------------------
# Compiled ceilings (decision 14)
#
# The dashboard owns the live throttles, but these are the outer bounds it can
# never talk this machine past. A fat-fingered "30 posts/day" on the server
# arrives here, gets clamped to CEILING_MAX_POSTS_PER_DAY_TOTAL, and emits a
# flow_error so the mistake is visible instead of silent.
#
# Changing a ceiling is a deliberate code change + redeploy. That is the point.
# ---------------------------------------------------------------------------

CEILING_MAX_POSTS_PER_DAY_TOTAL = 5
CEILING_MAX_POSTS_PER_ACCOUNT_PER_WEEK = 10
FLOOR_MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT = 18
EARLIEST_POST_HOUR = 6
LATEST_POST_HOUR = 22

# Editing ceilings (DESIGN_EDITS.md decision 30). Editing a live posting is not
# obviously safer than creating one — it is browser activity on an aged account
# that Craigslist may re-review — so it gets the same treatment: the dashboard
# owns the live values, these are the outer bounds it cannot talk us past.
CEILING_MAX_EDITS_PER_ACCOUNT_PER_DAY = 5
CEILING_MAX_EDITS_PER_POST_LIFETIME = 10
FLOOR_MIN_HOURS_BETWEEN_EDITS_SAME_POST = 24

# Defaults used when the server says nothing. `edits_enabled` is False on both
# sides until the DESIGN_EDITS phase-0 spike has confirmed how Craigslist's edit
# form actually behaves.
EDITS_ENABLED = False
MIN_HOURS_BETWEEN_EDITS_SAME_POST = 48
MAX_EDITS_PER_ACCOUNT_PER_DAY = 3
MAX_EDITS_PER_POST_LIFETIME = 5
EDIT_WINDOW_START_HOUR = 8
EDIT_WINDOW_END_HOUR = 19


@dataclass(frozen=True)
class Guardrails:
    min_hours_between_posts_same_account: int
    max_posts_per_day_total: int
    max_posts_per_account_per_week: int
    post_window_start_hour: int
    post_window_end_hour: int
    post_weekdays_only: bool

    # Editing (DESIGN_EDITS.md decision 30)
    edits_enabled: bool = EDITS_ENABLED
    min_hours_between_edits_same_post: int = MIN_HOURS_BETWEEN_EDITS_SAME_POST
    max_edits_per_account_per_day: int = MAX_EDITS_PER_ACCOUNT_PER_DAY
    max_edits_per_post_lifetime: int = MAX_EDITS_PER_POST_LIFETIME
    edit_window_start_hour: int = EDIT_WINDOW_START_HOUR
    edit_window_end_hour: int = EDIT_WINDOW_END_HOUR


def compiled_guardrails() -> Guardrails:
    """The values baked into this file — used when the server has nothing to say."""
    return Guardrails(
        min_hours_between_posts_same_account=MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT,
        max_posts_per_day_total=MAX_POSTS_PER_DAY_TOTAL,
        max_posts_per_account_per_week=MAX_POSTS_PER_ACCOUNT_PER_WEEK,
        post_window_start_hour=POST_WINDOW_START_HOUR,
        post_window_end_hour=POST_WINDOW_END_HOUR,
        post_weekdays_only=POST_WEEKDAYS_ONLY,
        edits_enabled=EDITS_ENABLED,
        min_hours_between_edits_same_post=MIN_HOURS_BETWEEN_EDITS_SAME_POST,
        max_edits_per_account_per_day=MAX_EDITS_PER_ACCOUNT_PER_DAY,
        max_edits_per_post_lifetime=MAX_EDITS_PER_POST_LIFETIME,
        edit_window_start_hour=EDIT_WINDOW_START_HOUR,
        edit_window_end_hour=EDIT_WINDOW_END_HOUR,
    )


def clamp_guardrails(remote: dict) -> tuple[Guardrails, list[str]]:
    """Clamp server-supplied throttles to the compiled ceilings.

    Returns the safe values plus a list of human-readable clamp notes. A
    non-empty list means the server asked for something this machine refused,
    and the caller should report it.
    """
    notes: list[str] = []
    base = compiled_guardrails()

    def _clamp(key: str, value, lo=None, hi=None, default=None):
        if value is None:
            return default
        out = value
        if lo is not None and out < lo:
            notes.append(f"{key}: server sent {value}, clamped up to {lo}")
            out = lo
        if hi is not None and out > hi:
            notes.append(f"{key}: server sent {value}, clamped down to {hi}")
            out = hi
        return out

    return (
        Guardrails(
            min_hours_between_posts_same_account=_clamp(
                "min_hours_between_posts_same_account",
                remote.get("min_hours_between_posts_same_account"),
                lo=FLOOR_MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT,
                default=base.min_hours_between_posts_same_account,
            ),
            max_posts_per_day_total=_clamp(
                "max_posts_per_day_total",
                remote.get("max_posts_per_day_total"),
                lo=0,
                hi=CEILING_MAX_POSTS_PER_DAY_TOTAL,
                default=base.max_posts_per_day_total,
            ),
            max_posts_per_account_per_week=_clamp(
                "max_posts_per_account_per_week",
                remote.get("max_posts_per_account_per_week"),
                lo=0,
                hi=CEILING_MAX_POSTS_PER_ACCOUNT_PER_WEEK,
                default=base.max_posts_per_account_per_week,
            ),
            post_window_start_hour=_clamp(
                "post_window_start_hour",
                remote.get("post_window_start_hour"),
                lo=EARLIEST_POST_HOUR,
                hi=LATEST_POST_HOUR - 1,
                default=base.post_window_start_hour,
            ),
            post_window_end_hour=_clamp(
                "post_window_end_hour",
                remote.get("post_window_end_hour"),
                lo=EARLIEST_POST_HOUR + 1,
                hi=LATEST_POST_HOUR,
                default=base.post_window_end_hour,
            ),
            post_weekdays_only=(
                base.post_weekdays_only
                if remote.get("post_weekdays_only") is None
                else bool(remote["post_weekdays_only"])
            ),
            # Editing (decision 30). A server that has never been migrated sends
            # nothing here, and the compiled defaults keep editing switched off.
            edits_enabled=(
                base.edits_enabled
                if remote.get("edits_enabled") is None
                else bool(remote["edits_enabled"])
            ),
            min_hours_between_edits_same_post=_clamp(
                "min_hours_between_edits_same_post",
                remote.get("min_hours_between_edits_same_post"),
                lo=FLOOR_MIN_HOURS_BETWEEN_EDITS_SAME_POST,
                default=base.min_hours_between_edits_same_post,
            ),
            max_edits_per_account_per_day=_clamp(
                "max_edits_per_account_per_day",
                remote.get("max_edits_per_account_per_day"),
                lo=0,
                hi=CEILING_MAX_EDITS_PER_ACCOUNT_PER_DAY,
                default=base.max_edits_per_account_per_day,
            ),
            max_edits_per_post_lifetime=_clamp(
                "max_edits_per_post_lifetime",
                remote.get("max_edits_per_post_lifetime"),
                lo=0,
                hi=CEILING_MAX_EDITS_PER_POST_LIFETIME,
                default=base.max_edits_per_post_lifetime,
            ),
            edit_window_start_hour=_clamp(
                "edit_window_start_hour",
                remote.get("edit_window_start_hour"),
                lo=EARLIEST_POST_HOUR,
                hi=LATEST_POST_HOUR - 1,
                default=base.edit_window_start_hour,
            ),
            edit_window_end_hour=_clamp(
                "edit_window_end_hour",
                remote.get("edit_window_end_hour"),
                lo=EARLIEST_POST_HOUR + 1,
                hi=LATEST_POST_HOUR,
                default=base.edit_window_end_hour,
            ),
        ),
        notes,
    )


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    profile_dir: Path
    photo_dir: Path
    # Which physical machine this account is allowed to run on.
    # Set in your local config; the script refuses to run on the wrong machine.
    allowed_machine: str


ACCOUNTS: list[Account] = [
    Account(
        name="craigs1",
        email="craigs1@dreamteammetalroofingandsolar.com",
        profile_dir=PROFILES_DIR / "craigs1",
        photo_dir=PHOTOS_DIR / "craigs1",
        allowed_machine="desktop-eseva3c",
    ),
    Account(
        name="craigs2",
        email="craigs2@dreamteammetalroofingandsolar.com",
        profile_dir=PROFILES_DIR / "craigs2",
        photo_dir=PHOTOS_DIR / "craigs2",
        allowed_machine="desktop-eseva3c",
    ),
    Account(
        name="craigs3",
        email="craigs3@dreamteammetalroofingandsolar.com",
        profile_dir=PROFILES_DIR / "craigs3",
        photo_dir=PHOTOS_DIR / "craigs3",
        allowed_machine="desktop-eseva3c",
    ),
]

ACCOUNTS_BY_NAME = {a.name: a for a in ACCOUNTS}


def code_version() -> str:
    """A short identifier for the code this machine is running.

    Prefers the git commit of the checkout the package was imported from, which
    is the only thing that answers "did my pull actually reach the daemon".
    Falls back to the installed version when git is unavailable, and says so
    plainly rather than pretending.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and sha:
            dirty = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            suffix = "+dirty" if (dirty.stdout or "").strip() else ""
            return f"{sha}{suffix} ({root})"
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return f"v{version('craigslist-auto')} ({root})"
    except Exception:
        return f"unknown ({root})"
