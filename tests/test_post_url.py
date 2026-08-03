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

from craigslist_auto.poster import _pick_posting_row, _url_matches_title  # noqa: E402
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

# The run that made this necessary: craigs2, 2026-08-03. The receipt page had
# no /d/ link, PostingID=7951217641 was resolved through
# /search/sss?postingID=..., and that search page — which ignores the parameter
# entirely — offered a stranger's appliance ad. Recorded URL: the search page.
print("\nthe posting id is resolved on the account's own postings page")
ROWS = [
    {"post_id": "7951217641",
     "title": "Roof Repair Handyman -> Emergency -> Leak -> Near Me -> Same Day",
     "href": "https://www.craigslist.org/view/d/miami-roof-repair-handyman-emergency/9xKtRmQpLvN3wYb7cHs2Tj"},
    {"post_id": "7950716823", "title": TITLE, "href": REAL},
]
check(
    "the row carrying our PostingID wins",
    _pick_posting_row(ROWS, post_id="7951217641") == ROWS[0]["href"],
)
check(
    "an id that is not in the table resolves to nothing, never a neighbour",
    _pick_posting_row(ROWS, post_id="7912345678") is None,
)
check(
    "with no id, the ad is found by its title",
    _pick_posting_row(ROWS, expected_title=TITLE) == REAL,
)
check(
    "a title Craigslist truncated in the table still matches",
    _pick_posting_row(
        [{"post_id": "7947735493",
          "title": "Roof Leak Repair Specialist Serving Miami-Dade, Broward, Palm Beach &",
          "href": "https://www.craigslist.org/view/d/pompano-beach-roof-leak-repair/2idMXkBtfX7E43474ecd8j"}],
        expected_title=(
            "Roof Leak Repair Specialist Serving Miami-Dade, Broward, Palm Beach "
            "& The Treasure Coast"
        ),
    ) is not None,
)
check(
    "a different roofing ad on the same account is not mistaken for ours",
    _pick_posting_row(ROWS, expected_title="Tile Roof Replacement - Free Estimate") is None,
)
check(
    "a row with no posting link is skipped",
    _pick_posting_row(
        [{"post_id": "7951217641", "title": TITLE, "href": ""}], post_id="7951217641"
    ) is None,
)
check("an empty table resolves to nothing", _pick_posting_row([], post_id="7951217641") is None)

print("")
for line in ok:
    print(f"  OK  {line}")
print(f"\n{len(ok)} checks passed")
