"""The contact phone must survive Craigslist's re-render of the contact block.

Learned from craigs4 on 2026-08-05 and again on 2026-08-06, both on draft 71.
The run ticked `show_phone_ok`, typed the number into `contact_phone`, and then
ticked `contact_phone_ok` and `contact_text_ok`. Those two carry
`data-depends-on="show_phone_ok"`, and toggling them re-renders the block from
form state — which discarded the number that had just been typed.

Craigslist rejected the submission with "If users can contact you by phone,
please include a contact phone number". The captured HTML from that failure is
unambiguous: `PostingTitle`, `postal`, `geographic_area` and `license_info` all
came back holding what the run typed, and `contact_phone` came back `value=""`.

Accounts with saved contact preferences never hit it — their checkboxes arrive
already ticked, so nothing toggles and nothing re-renders. craigs4 had been
re-logged-in the previous day after a `login_check` failure, which is exactly
what clears those preferences. That is why a flow that had run for months
started failing on one account only.

Three properties, each of which alone would have saved the slot:
  1. Every checkbox is ticked before the number is typed, so the re-render
     happens while the field is still empty.
  2. The value is read back after typing, not assumed.
  3. A field that will not hold the number fails at `form_phone` — a pre-upload
     step — rather than at `form_validation` after the whole form is walked.

Desktop test — run with the root venv:
    uv run python tests/test_contact_phone.py
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


# --- a contact block that behaves the way Craigslist's does ------------------

PHONE = "(954) 366-9796"


class FakeField:
    """The contact_phone input. Typing appends, exactly as human_type does."""

    def __init__(self, block):
        self.block = block

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def click(self):
        pass

    def type(self, ch, delay=None):
        self.block.phone += ch

    def press(self, _key):
        self.block.phone = self.block.phone[:-1]

    def fill(self, value):
        self.block.phone = value

    def input_value(self):
        return self.block.phone


class FakeCheckbox:
    def __init__(self, block, name, checked, present=True):
        self.block = block
        self.name = name
        self.checked = checked
        self.present = present

    def count(self):
        return 1 if self.present else 0

    @property
    def first(self):
        return self

    def is_checked(self):
        return self.checked

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def bounding_box(self):
        return None

    def click(self):
        self.checked = not self.checked
        self.block.on_toggle(self.name)


class FakeMouse:
    def move(self, *_a, **_k):
        pass

    def click(self, *_a, **_k):
        pass


class ContactBlock:
    """`prefilled` models an account with saved contact preferences.

    `reveal_after` models the dependent checkboxes arriving a beat late: they
    carry `data-depends-on="show_phone_ok"`, so they are absent from the DOM
    until it has been ticked and the block has re-rendered. Nothing here is
    slow — `wait_for_selector` just has to be the thing that notices.
    """

    def __init__(self, *, prefilled: bool, has_phone_field: bool = True,
                 reveal_after: int = 0):
        self.phone = ""
        self.mouse = FakeMouse()
        self.rerenders = 0
        self.has_phone_field = has_phone_field
        self.reveal_after = reveal_after
        self.waits = []
        self.polls = {}
        self.boxes = {
            "show_phone_ok": FakeCheckbox(self, "show_phone_ok", prefilled),
            "contact_phone_ok": FakeCheckbox(self, "contact_phone_ok", prefilled),
            "contact_text_ok": FakeCheckbox(self, "contact_text_ok", prefilled),
        }

    def _revealed(self, name):
        if name == "show_phone_ok" or not self.reveal_after:
            return True
        # Present only once something has waited for it. A bare count() has
        # polled nothing and sees an empty DOM — which is precisely the old
        # ordering's silent skip.
        return self.polls.get(name, 0) >= self.reveal_after

    def on_toggle(self, _name):
        # The bug, reproduced: any toggle in the block restores it from form
        # state, and form state has no phone number in it.
        self.rerenders += 1
        self.phone = ""

    def wait_for_selector(self, sel, state=None, timeout=None):
        self.waits.append(sel)
        # A real wait_for_selector polls until the element shows up. Model the
        # polling, so "did the code wait?" is what decides whether it finds it.
        for name in self.boxes:
            if sel == f"input[name='{name}']":
                self.polls[name] = self.polls.get(name, 0) + 1
        loc = self.locator(sel)
        if not loc.count():
            raise TimeoutError(f"{sel} never appeared")
        return loc

    def locator(self, sel):
        for name, cb in self.boxes.items():
            if sel == f"input[name='{name}']":
                if not self._revealed(name):
                    return FakeCheckbox(self, name, False, present=False)
                return cb
        if sel == "input[name='contact_phone']":
            if not self.has_phone_field:
                return FakeCheckbox(self, "contact_phone", False, present=False)
            return FakeField(self)
        raise AssertionError(f"unexpected selector {sel!r}")


class Warned:
    """Stands in for PostRun — only `warn` is ever called."""

    def __init__(self):
        self.warnings = []

    def warn(self, m):
        self.warnings.append(m)


def fill(block, run=None):
    """What post_ad's form_phone step does, in the order it does it."""
    poster._tick_contact_box(block, "show_phone_ok")
    for name in ("contact_phone_ok", "contact_text_ok"):
        poster._tick_contact_box(block, name)
    poster._ensure_contact_phone(block, PHONE, run=run)


print("contact phone")

# 1. A fresh profile — every box starts unticked. This is craigs4 on 08-06.
block = ContactBlock(prefilled=False)
fill(block)
check("fresh profile: the number survives three re-renders", block.phone == PHONE)
check("fresh profile: the block really did re-render", block.rerenders == 3)
check(
    "fresh profile: call and text are both enabled",
    all(block.boxes[n].checked for n in block.boxes),
)

# 2. Saved contact preferences — nothing toggles, nothing re-renders. This is
#    every account that kept working while craigs4 was broken.
block = ContactBlock(prefilled=True)
fill(block)
check("saved prefs: the number is entered", block.phone == PHONE)
check("saved prefs: nothing was toggled", block.rerenders == 0)

# 3. The old ordering, against the same block, still fails. If this ever passes
#    the fake has stopped modelling the bug and tests 1-2 prove nothing.
block = ContactBlock(prefilled=False)
poster._tick_contact_box(block, "show_phone_ok")
poster.human_type(block.locator("input[name='contact_phone']"), PHONE)
for name in ("contact_phone_ok", "contact_text_ok"):
    poster._tick_contact_box(block, name)
check("old ordering still loses the number (the fake models the bug)", block.phone == "")

# 3b. The dependent boxes arrive a beat after show_phone_ok is ticked. The
#     reorder removed the accidental wait the old code had — seconds of typing
#     the phone number — so ticking them must wait rather than sample once.
#     Without the wait this posts an ad nobody can ring, and says nothing.
block = ContactBlock(prefilled=False, reveal_after=1)
run = Warned()
poster._tick_contact_box(block, "show_phone_ok")
for name in ("contact_phone_ok", "contact_text_ok"):
    if not poster._tick_contact_box(block, name):
        run.warn(name)
poster._ensure_contact_phone(block, PHONE, run=run)
check("late-arriving boxes are waited for, not skipped",
      block.boxes["contact_phone_ok"].checked
      and block.boxes["contact_text_ok"].checked)
check("late-arriving boxes produced no warning", run.warnings == [])
check("the number still survives their re-renders", block.phone == PHONE)

# 3c. A box that never arrives is reported rather than skipped in silence.
block = ContactBlock(prefilled=False, reveal_after=99)
run = Warned()
poster._tick_contact_box(block, "show_phone_ok")
for name in ("contact_phone_ok", "contact_text_ok"):
    if not poster._tick_contact_box(block, name):
        run.warn(name)
check("a box that never arrives warns", len(run.warnings) == 2)


# 4. A field that will not hold the value fails loudly, at form_phone, rather
#    than submitting and letting Craigslist reject it.
class StubbornBlock(ContactBlock):
    def on_toggle(self, _name):
        self.rerenders += 1
        self.phone = ""

    def locator(self, sel):
        loc = super().locator(sel)
        if sel == "input[name='contact_phone']":
            # Every write is swallowed, however it is made.
            loc.type = lambda ch, delay=None: None
            loc.press = lambda _k: None
            loc.fill = lambda _v: None
        return loc


raised = None
try:
    fill(StubbornBlock(prefilled=True))
except RuntimeError as e:
    raised = e
check("a field that will not hold the number raises", raised is not None)
check("the error names the field and what it read", "contact_phone" in str(raised))

# 5. show_phone_ok checked but no phone field at all — the form promised
#    somewhere to put the number and did not deliver.
raised = None
try:
    poster._ensure_contact_phone(
        ContactBlock(prefilled=True, has_phone_field=False), PHONE
    )
except RuntimeError as e:
    raised = e
check("checked box with no field raises", raised is not None)


# 6. No phone option on the form at all — not fatal, but recorded.
run = Warned()
block = ContactBlock(prefilled=False, has_phone_field=False)
poster._ensure_contact_phone(block, PHONE, run=run)
check("no phone field: does not raise", True)
check("no phone field: warns instead of passing silently", len(run.warnings) == 1)

# 7. The step this raises under must be one the server routes as pre-upload,
#    or a run that uploaded nothing parks a draft for a human.
check("form_phone is a 'form*' step, so the outcome is failed_form",
      "form_phone".startswith("form"))

for label in ok:
    print(f"  ok    {label}")
print(f"\n{len(ok)} checks passed")
