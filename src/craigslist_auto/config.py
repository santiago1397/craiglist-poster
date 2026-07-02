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

# Ghost-check egress proxy. Visiting CL search from a different IP than the
# posting machine prevents false-negatives (CL shows your own post to you even
# when it's ghosted for the public). Creds come from .env: INSTANTPROXIES_USER/PASS.
GHOST_CHECK_PROXY_HOST = "181.177.78.206"
GHOST_CHECK_PROXY_PORT = 8800

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
