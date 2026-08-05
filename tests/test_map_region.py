"""The map step must not be able to strand a run before the uploader.

Learned from a live run on 2026-08-05. craigs3 reached geoverify, Craigslist
asked whether a 33410 ad should be searchable in Treasure Coast or South
Florida, and the run answered nothing: while that question is open the page
hides `#regular_continue_button` behind `style="display: none"`, and a hidden
button still matches `:not([disabled])`. `_continue` clicked it, submitted
nothing, and `optional=True` swallowed the miss.

The run then walked into `photo_upload` still standing on the map — a page with
no file input and no prospect of one — and died on
`Timeout 30000ms exceeded waiting for input[type='file']`. That message names
the uploader, which was never involved. Worse, `photo_upload` is asset-
consuming, so the server parked the draft and retired its images for a run that
never showed Craigslist a photo.

Three properties, each of which alone would have saved the slot:
  1. `_continue` cannot click a button the page is hiding.
  2. `_continue` cannot answer the region question by DOM order — the first
     button is `area_change_ok`, i.e. "move the ad to the other region".
  3. The run cannot enter `photo_upload` from the map page.

Desktop test — run with the root venv (needs patchright):
    uv run python tests/test_map_region.py
"""
import re
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


# --- a page that answers selector counts, and nothing else -------------------

class FakeLocator:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self


class FakePage:
    def __init__(self, counts, url="https://post.craigslist.org/k/x/y?s=geoverify"):
        self.counts = counts
        self.url = url
        self.loads = 0

    def locator(self, sel):
        return FakeLocator(self.counts.get(sel, 0))

    def wait_for_load_state(self, *_a, **_k):
        self.loads += 1


clicks = []
poster.human_click = lambda page, loc: clicks.append(loc)
poster.sleep_jitter = lambda *_a, **_k: None


# ---------------------------------------------------------------------------
print("\n_continue cannot click a hidden button or answer the region question")
# ---------------------------------------------------------------------------

src = Path(poster.__file__).read_text(encoding="utf-8")
# The list is local to `_continue`, so read it out of the source — then resolve
# the f-string placeholder, because what matters is the selector that runs.
candidates = re.search(r"candidates = \[\n(.*?)\n    \]", src, re.S).group(1)
selectors = [
    s.replace("{NOT_REGION}", poster.NOT_REGION)
    for s in re.findall(r'f?"([^"]+)"', candidates)
]
check("_continue still has its candidate selectors", len(selectors) == 3)
for sel in selectors:
    check(f"{sel.split(':')[0]} is restricted to visible buttons", ":visible" in sel)

# The middle candidate names `go`/`continue` explicitly, so it can never match a
# pickbutton; the two generic ones must exclude them by name.
generic = [s for s in selectors if "value='continue'" not in s]
check("both generic candidates exist", len(generic) == 2)
for sel in generic:
    check(
        f"{sel.split(':')[0]} refuses the region pickbuttons",
        "keep_old_area" in sel and "area_change_ok" in sel,
    )


# ---------------------------------------------------------------------------
print("\nthe region question is answered deliberately, and always the same way")
# ---------------------------------------------------------------------------

clean = FakePage({})
check("no prompt on the page is not an error", poster._answer_region_prompt(clean) is False)
check("and clicks nothing", not clicks)

asked = FakePage({poster.REGION_KEEP_VISIBLE: 1})
check("a prompt is reported as answered", poster._answer_region_prompt(asked) is True)
check("exactly one button was clicked", len(clicks) == 1)
check("the page was waited on after answering", asked.loads == 1)

# The one that matters: we keep the region we post from. `area_change_ok` moves
# the ad to a region where the account, the category id and every other queued
# draft do not apply.
check(
    "the answer is 'keep the region we post from', never 'change it'",
    "REGION_CHANGE" not in re.search(
        r"def _answer_region_prompt.*?\n(?=\ndef )", src, re.S
    ).group(0),
)


# ---------------------------------------------------------------------------
print("\nthe run cannot reach photo_upload from the map page")
# ---------------------------------------------------------------------------

def stuck_on(counts, url="https://post.craigslist.org/k/x/y?s=geoverify"):
    try:
        poster._assert_left_map(FakePage(counts, url))
        return None
    except RuntimeError as e:
        return str(e)


err = stuck_on({poster.REGION_PROMPT: 1, "#leafletForm:visible": 1})
check("an unanswered region question stops the run", err is not None)
check("and the error says so, without mentioning the uploader", "region" in err)

err = stuck_on({"#leafletForm:visible": 1})
check("a map page with no uploader stops the run", err is not None)
check("and the error names the map step", "map step" in err)

check(
    "a map form that still has an uploader is left alone",
    stuck_on({"#leafletForm:visible": 1, "input[type='file']": 1}) is None,
)
check(
    "the image page passes untouched",
    stuck_on({"input[type='file']": 1}, url="https://post.craigslist.org/k/x/y?s=editimage") is None,
)
check("a page that reveals nothing is not assumed broken", stuck_on({}) is None)


# ---------------------------------------------------------------------------
print("\nfailing there costs no images")
# ---------------------------------------------------------------------------

# `map_validation` runs before a single photo is uploaded, so it must requeue
# the draft rather than park it. test_failure_reporting.py owns the full table;
# this pins the one step this fix adds, next to the reason it is safe.
poster_steps = set(re.findall(r'^\s+step = "([a-z_]+)"', src, re.M))
check("the poster declares a map_validation step", "map_validation" in poster_steps)
check(
    "map_validation is ordered before photo_upload",
    src.index('step = "map_validation"') < src.index('step = "photo_upload"'),
)
queue_src = (ROOT / "backend" / "app" / "services" / "queue.py").read_text(encoding="utf-8")
pre_upload = re.search(r"PRE_UPLOAD_STEPS = frozenset\(\{(.*?)\}\)", queue_src, re.S).group(1)
check("the server routes map_validation as pre-upload", '"map_validation"' in pre_upload)


# ---------------------------------------------------------------------------
print("\nthe uploader is waited for as attached, not painted")
# ---------------------------------------------------------------------------

# Craigslist styles the file input opacity-0 and absolutely positioned; it is
# driven with set_input_files, which does not need it painted. editor.py has
# always waited this way. This side had the default `visible`.
wait = re.search(r"page\.wait_for_selector\(\s*\"input\[type='file'\]\".*?\)", src, re.S)
check("photo_upload still waits for the file input", wait is not None)
check("and waits for it attached, not visible", 'state="attached"' in wait.group(0))


print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
