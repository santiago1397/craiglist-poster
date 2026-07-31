"""The URL we record after publishing must be the ad we published.

Learned from a live run, not from documentation: Craigslist's confirmation page
carries other people's listings, so taking the first /d/ link recorded a
stranger's car ad as the post URL. That is worse than recording nothing — it
corrupts post history, ghost checks and stats while looking perfectly correct
in the dashboard.

The second half is the posting id. Every pattern wanted 8+ digits, but
Craigslist's current share URL is /view/d/<slug>/<base62 token>, so a real post
yielded no id, wrote no history row, and left the account free to post again
inside its cooldown.

Desktop test — run with the root venv (needs patchright):
    uv run python tests/test_post_url.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from craigslist_auto.poster import _url_matches_title  # noqa: E402
from craigslist_auto.stats import extract_post_id  # noqa: E402

ok = []


def check(label, cond):
    if not cond:
        print(f"  FAIL  {label}")
        raise SystemExit(1)
    ok.append(label)


TITLE = "Emergency Roof Repair Service - Roof Damage, Leak & Storm Repair"
REAL = "https://www.craigslist.org/view/d/miami-emergency-roof-repair-service/xvbywnthPhu59jd5tMPpGP"
# The listing actually captured in production before this was fixed.
FOREIGN = "https://www.craigslist.org/view/d/miami-2020-dodge-charger-police-pursuit/9klGBReN8RGpzrxIv4syug"

print("\nthe URL we record is the ad we published")
check("our own post is recognised by its slug", _url_matches_title(REAL, TITLE))
check("the Dodge Charger that got recorded is rejected", not _url_matches_title(FOREIGN, TITLE))
check("an empty title never matches anything", not _url_matches_title(REAL, ""))
check(
    "a slug sharing no distinctive words is rejected",
    not _url_matches_title("https://x/view/d/miami-boat-trailer/abcdefghijklmnopqrst", TITLE),
)
check(
    "a differently-worded roofing ad still matches its own slug",
    _url_matches_title(
        "https://www.craigslist.org/view/d/davie-commercial-roof-repair-shingle/aBcDeFgHiJkLmNoPqRsT",
        "Commercial Roof repair - Shingle, Flat & Tile",
    ),
)

print("\nposting ids parse from every URL form Craigslist serves")
check("current /view/d/slug/<token> form", extract_post_id(REAL) == "xvbywnthPhu59jd5tMPpGP")
check(
    "legacy numeric form still parses",
    extract_post_id("https://miami.craigslist.org/mdc/skt/d/hollywood-roof/7712345678.html")
    == "7712345678",
)
check(
    "search-by-postingID fallback still parses",
    extract_post_id("https://miami.craigslist.org/search/sss?postingID=7712345678")
    == "7712345678",
)
check(
    "a session-bound compose URL yields no id",
    extract_post_id("https://post.craigslist.org/k/bbfEj6fxDgbS8h6WFxybYu/FdbgK") is None,
)
check("no url, no id", extract_post_id(None) is None)

print("")
for line in ok:
    print(f"  OK  {line}")
print(f"\n{len(ok)} checks passed")
