"""A post that cannot be found must leave evidence, and titles must survive emoji.

Learned from craigs1 on 2026-08-05 15:33 and 2026-08-06 08:48. Both runs
reported `posted`, both recorded a session-bound `post.craigslist.org/k/<token>`
URL, and both had `artifact_ids = []` — because artifacts were only ever
captured when something raised, and nothing raised. The one outcome nobody can
explain after the fact was the only outcome with no screenshot attached.

Settling whether those two ads existed took a database session and a
`cl scan-ended` two days later. They did not: neither appeared in the 06:00
stats scrape of the active tab, nor in the inactive or deleted tabs. Craigslist
almost certainly never published them.

Two properties here, and the first is the one that pays for itself:

  1. When there is no `/d/` link and no PostingID, the confirmation page is
     photographed **before** resolution navigates away, and the postings page is
     photographed when it yields no match. Two artifacts, the pair that answers
     "did this ad ever exist".
  2. Title comparison ignores emoji and punctuation. `_pick_posting_row`
     compares by prefix, so a leading emoji breaks it outright — and the 08-06
     title was "🏠 Roofer in Hialeah …".

A healthy run must capture nothing. Evidence collection that fires on every
post is noise, and noise is why nobody reads it.

Desktop test — run with the root venv:
    uv run python tests/test_post_unverified.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from craigslist_auto import poster  # noqa: E402

ok = []


def check(label, cond):
    if not cond:
        print(f"  FAIL  {label}")
        raise SystemExit(1)
    ok.append(label)


# The retry path sleeps ~30s across three attempts. Nothing here is about time.
poster.sleep_jitter = lambda *a, **k: None
poster.read_pause = lambda *a, **k: None

CAPTURES = []
poster.artifacts.capture_page = (
    lambda page, flow, label, account: (
        CAPTURES.append({"label": label, "account": account, "url": page.url}),
        [f"artifact-{label}"],
    )[1]
)

TITLE_EMOJI = "🏠 Roofer in Hialeah - Roof Repair, Se Habla Espanol, Free Estimate"
ACCOUNT_URL = "https://accounts.craigslist.org/login/home"
RECEIPT = "https://post.craigslist.org/k/67uLcaZHf1cQZ3jXiQRVqD/OOput?s=pn"


class FakeLinks:
    def __init__(self, hrefs):
        self.hrefs = hrefs

    def count(self):
        return len(self.hrefs)

    def nth(self, i):
        class L:
            def get_attribute(_self, _name):
                return self.hrefs[i]
        return L()


class FakeSimple:
    def __init__(self, n):
        self.n = n

    def count(self):
        return self.n

    @property
    def first(self):
        return self


class FakePage:
    """Just enough page for _extract_post_url, plus an event log for ordering."""

    def __init__(self, *, d_links=(), html="", rows=(), url=RECEIPT):
        self.d_links = list(d_links)
        self.html = html
        self.rows = list(rows)
        self.url = url
        self.events = []

    def locator(self, sel):
        if "/d/" in sel:
            return FakeLinks(self.d_links)
        if "password" in sel:
            return FakeSimple(0)
        if "posting-row" in sel:
            return FakeSimple(len(self.rows))
        return FakeSimple(0)

    def content(self):
        return self.html

    def goto(self, url, **_k):
        self.events.append(("goto", url))
        self.url = url

    def wait_for_selector(self, *_a, **_k):
        return None

    def evaluate(self, _js):
        return self.rows


class Run:
    def __init__(self):
        self.warnings = []
        self.artifact_ids = []

    def warn(self, m):
        self.warnings.append(m)


def extract(page, run, title=TITLE_EMOJI):
    return poster._extract_post_url(
        page, run=run, expected_title=title, account="craigs1"
    )


print("unverified post")

# 1. Healthy free posting: the confirmation page links straight to the ad.
CAPTURES.clear()
run = Run()
page = FakePage(
    d_links=["https://www.craigslist.org/view/d/hialeah-roofer-in-hialeah-roof/abc123"],
)
url = extract(page, run)
check("healthy run returns the /d/ link", "/view/d/" in url)
check("healthy run captures nothing", CAPTURES == [])
check("healthy run warns about nothing", run.warnings == [])

# 2. Paid receipt: no link, but a PostingID that resolves on the postings page.
#    An exact id match needs no picture.
CAPTURES.clear()
run = Run()
page = FakePage(
    html="Purchase Receipt PostingID: 7951866011",
    rows=[{"post_id": "7951866011", "title": "whatever",
           "href": "https://www.craigslist.org/view/d/hialeah-roof/xyz"}],
)
url = extract(page, run)
check("receipt id resolves to the live URL", url.endswith("/xyz"))
check("an exact id match captures nothing", CAPTURES == [])

# 3. The craigs1 case: no link, no id, nothing on the postings page.
CAPTURES.clear()
run = Run()
page = FakePage(html="<html>some hub page</html>", rows=[])
url = extract(page, run)
check("falls back to the receipt URL", url == RECEIPT)
check("two pages were captured", len(CAPTURES) == 2)
check("the confirmation page was captured",
      CAPTURES[0]["label"] == "confirmation_unverified")
check("the postings page was captured",
      CAPTURES[1]["label"] == "postings_page_no_match")
check("artifact ids reached the run", len(run.artifact_ids) == 2)
# The ordering property: resolution navigates away, so a capture taken after it
# photographs the account page and the confirmation page is lost for good.
check("the confirmation page was captured BEFORE navigating away",
      CAPTURES[0]["url"] == RECEIPT)
check("the second capture is the page we navigated to",
      CAPTURES[1]["url"] == poster.CL_ACCOUNT_URL)
check("the warning says the ad may not exist",
      any("never have published" in w for w in run.warnings))

# 3b. Links present but none of them ours, and no id. The confirmation page is
#     demonstrably a confirmation page here, so it is not photographed — but
#     reaching the last resort still photographs the postings page. Any run that
#     ends up unable to name its own ad leaves at least one picture behind.
CAPTURES.clear()
run = Run()
page = FakePage(
    d_links=["https://www.craigslist.org/view/d/miami-dodge-charger-for-sale/zzz"],
    html="<html>hub</html>",
    rows=[],
)
url = extract(page, run)
check("a foreign /d/ link is not recorded as ours", url == RECEIPT)
check("the last resort always captures something", len(CAPTURES) == 1)
check("and it is the postings page",
      CAPTURES[0]["label"] == "postings_page_no_match")

# 4. The emoji fix: the table renders the title without the emoji and truncated.
#    This is what should have resolved the 08-06 post if the ad had existed.
CAPTURES.clear()
run = Run()
page = FakePage(
    html="<html>hub</html>",
    rows=[{"post_id": "", "title": "Roofer in Hialeah - Roof Repair, Se Habla...",
           "href": "https://www.craigslist.org/view/d/hialeah-roofer/tok99"}],
)
url = extract(page, run)
check("an emoji-led title still matches its table row", url.endswith("/tok99"))

# 4b. And the old normalisation genuinely could not do that.
old_normalise = lambda t: " ".join((t or "").lower().split())
a, b = old_normalise(TITLE_EMOJI), old_normalise("Roofer in Hialeah - Roof Repair, Se Habla")
check("the old normalisation failed on the leading emoji",
      not (a.startswith(b) or b.startswith(a)))

# 5. Safety: loosening the comparison must not match a DIFFERENT ad of ours.
#    craigs1 carries many near-identical roofing titles; picking the wrong one
#    would attach today's post record to an old listing.
for other in (
    "Roof Repair Contractor Serving Miami-Dade and Broward",
    "Roof Leak Repair Specialist Serving Miami",
    "Emergency Roof Repair Service - Roof Damage",
):
    got = poster._pick_posting_row(
        [{"post_id": "", "title": other,
          "href": "https://www.craigslist.org/view/d/x/other"}],
        expected_title=TITLE_EMOJI,
    )
    check(f"does not match a different ad: {other[:34]!r}", got is None)

# 6. A title too short to be distinctive is refused rather than guessed at.
got = poster._pick_posting_row(
    [{"post_id": "", "title": "Roofer",
      "href": "https://www.craigslist.org/view/d/x/short"}],
    expected_title=TITLE_EMOJI,
)
check("a too-short table title is not matched", got is None)

for label in ok:
    print(f"  ok    {label}")
print(f"\n{len(ok)} checks passed")
