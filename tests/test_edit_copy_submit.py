"""A refused copy edit must not report itself as applied.

Learned from a live run on 2026-08-05. A post was staged with five changed text
fields and a 24-image gallery. The attempt reported `applied` at
`applied_rev=25`, `images_live_count=24`, no failed step — and the live posting
came back from a fresh hydrate with its original title, its original two-word
body, its original ZIP and phone, and a `content_hash` byte-identical to the one
captured before the edit began. Only the images had changed.

The step trail said it plainly, and nothing was reading it:

    [ok] submit_copy      0.91s   ... | posting details | .../k/<id>?s=edit
    [ok] open_images_step 4.55s   ... | choose images    | .../k/<id>?s=editimage

`submit_copy` is documented as returning to the hub. It came back to `?s=edit`,
the copy page, because Craigslist refuses an edit form the way it refuses a
posting form — HTTP 200, same page, errors inline — and `wait_for_load_state` is
satisfied either way. The flow then `goto`s the image step, which discards the
typed copy, replaces the gallery, publishes, and calls the whole thing applied.

The gallery landing is what made it convincing: something visibly changed, so
the text not changing read as "we staged it wrong" rather than "the edit was
refused". This is the same bug class as `_assert_form_accepted` and
`_assert_left_map` in poster.py, one flow over.

Desktop test — run with the root venv (needs patchright):
    uv run python tests/test_edit_copy_submit.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from craigslist_auto import editor  # noqa: E402

ok = []


def check(label, cond):
    if not cond:
        print(f"  FAIL  {label}")
        raise SystemExit(1)
    ok.append(label)


class FakeLocator:
    def __init__(self, texts=()):
        self._texts = list(texts)

    def count(self):
        return len(self._texts)

    def nth(self, i):
        return FakeLocator([self._texts[i]])

    def inner_text(self):
        return self._texts[0]


class FakePage:
    """Answers the two questions _copy_rejection asks: is the body textarea
    still here, and what errors does the page show."""

    def __init__(self, *, on_copy_page, errors=()):
        self.counts = {editor.SEL["edit_body"]: 1 if on_copy_page else 0}
        self._errors = list(errors)

    def locator(self, sel):
        if sel in self.counts:
            return FakeLocator(["x"] * self.counts[sel])
        if "err" in sel:
            return FakeLocator(self._errors)
        return FakeLocator()


# ---------------------------------------------------------------------------
print("\nleaving the copy page is the only thing that counts as saved")
# ---------------------------------------------------------------------------

check(
    "a page without the body textarea is accepted",
    editor._copy_rejection(FakePage(on_copy_page=False)) is None,
)
check(
    "the images step is never mistaken for a refusal",
    editor._copy_rejection(FakePage(on_copy_page=False, errors=["ignored"])) is None,
)

# The live failure: back on the copy page, so the save did not take.
msg = editor._copy_rejection(FakePage(on_copy_page=True))
check("still on the copy form is a refusal", msg is not None)
check(
    "a refusal with no error markup still reports something actionable",
    "no visible reason" in msg,
)

msg = editor._copy_rejection(FakePage(
    on_copy_page=True,
    errors=["your posting body is too long", "your posting body is too long", "zip code is invalid"],
))
check("Craigslist's own wording is carried out", "too long" in msg)
check("and a second, different error too", "zip code is invalid" in msg)
check("duplicates are collapsed", msg.count("too long") == 1)
check("the message is capped", len(msg) <= 400)


# ---------------------------------------------------------------------------
print("\nthe check is wired in before anything is destroyed")
# ---------------------------------------------------------------------------

src = Path(editor.__file__).read_text(encoding="utf-8")
submit = re.search(r'with log\.step\("submit_copy"\):(.*?)\n            final_images',
                   src, re.S).group(1)
check("submit_copy calls the check", "_copy_rejection(page)" in submit)
check("and raises on a refusal", "EditFailure(" in submit)
check(
    "as mutated=False, so a refused edit requeues instead of parking",
    "mutated=False" in submit,
)
check("and captures the page it was refused on", "copy_rejected" in submit)

# Order is the whole point: the gallery must not be touched by an edit whose
# first page was refused. A 24-image replace is 48 operations against a live
# posting, and every one of them is a chance to end up `degraded_live`.
check(
    "the refusal is raised before the images step is opened",
    src.index("_copy_rejection(page)") < src.index('log.step("open_images_step")'),
)
check(
    "and before the publish",
    src.index("_copy_rejection(page)") < src.index('log.step("publish")'),
)


print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
