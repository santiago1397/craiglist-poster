"""Call-tracking numbers: validation, retirement, and rotation.

The rule that matters is the last one. A number added here has to reach
*generated* drafts, not just hand-written ones — the generator produces almost
everything, so a list the generator ignores is a list that does nothing.
"""
import os
import tempfile

os.environ.setdefault("IMAGES_DIR", tempfile.mkdtemp())

import random  # noqa: E402

from app.db import conn, init_pool, tx  # noqa: E402
from app import reference  # noqa: E402
from app.services import contacts  # noqa: E402
from app.services import drafts as drafts_svc  # noqa: E402

init_pool()
ok = []

with tx() as c:
    c.execute("TRUNCATE draft_images, images, drafts CASCADE")
    c.execute("DELETE FROM contact_numbers")
    contacts.create(c, number="(954) 634-7370")
    contacts.create(c, number="(954) 634-7420")

# --- the format you type is the format that is stored ----------------------
# A number is also a tracking identity; reformatting it would make it look like
# a different number next to a call log.
with tx() as c:
    row = contacts.create(c, number="  954-555-0123  ", label="  yard signs  ")
assert row["number"] == "954-555-0123", row["number"]
assert row["label"] == "yard signs", row["label"]
ok.append("numbers are trimmed but never reformatted; labels are trimmed")

# --- nonsense is refused ---------------------------------------------------
for bad in ("", "12345", "call me", "1" * 20):
    try:
        contacts.normalise(bad)
        raise AssertionError(f"accepted {bad!r} as a phone number")
    except contacts.InvalidNumber:
        pass
ok.append("too few digits, too many, and non-numeric text are all refused")

# --- adding a duplicate is an error, not a second row ----------------------
with tx() as c:
    try:
        contacts.create(c, number="954-555-0123")
        raise AssertionError("added the same number twice")
    except contacts.InvalidNumber as e:
        assert "already" in str(e), e
ok.append("adding a number already in the list is refused")

# --- retiring hides it from the composer but keeps the row -----------------
with conn() as c:
    live = [p for p in contacts.list_numbers(c) if p["number"] == "954-555-0123"]
with tx() as c:
    contacts.update(c, live[0]["id"], {"active": False})
with conn() as c:
    assert "954-555-0123" not in contacts.active_numbers(c)
    assert any(p["number"] == "954-555-0123" for p in contacts.list_numbers(c)), \
        "retiring deleted the row"
ok.append("a retired number leaves the rotation but stays on record")

# --- re-adding a retired number revives it rather than duplicating ---------
with tx() as c:
    revived = contacts.create(c, number="954-555-0123", label="second run")
assert revived["active"] and revived["label"] == "second run"
with conn() as c:
    assert len([p for p in contacts.list_numbers(c) if p["number"] == "954-555-0123"]) == 1
ok.append("re-adding a retired number reactivates the existing row")

# --- the composer payload reflects the table, not the compiled list --------
with tx() as c:
    c.execute("DELETE FROM contact_numbers")
    contacts.create(c, number="(305) 555-0100")
with conn() as c:
    payload = reference.as_payload(c)
assert payload["phone_numbers"] == ["(305) 555-0100"], payload["phone_numbers"]
ok.append("the composer offers what is in the table, not reference.PHONE_NUMBERS")

# --- an empty table must not resurrect the compiled-in numbers -------------
# "every number is retired" is a real state and has to be visible as one.
with tx() as c:
    c.execute("DELETE FROM contact_numbers")
with conn() as c:
    assert reference.as_payload(c)["phone_numbers"] == []
ok.append("an empty table yields no numbers rather than the compiled fallback")

# --- rotation prefers the least-used number --------------------------------
with tx() as c:
    for n in ("(954) 111-1111", "(954) 222-2222"):
        contacts.create(c, number=n)
    # Nine drafts already carry the first number; the second has none.
    for i in range(9):
        drafts_svc.create_draft(c, {
            "account": "craigs1", "title": f"t{i}", "body": "b",
            "phone_number": "(954) 111-1111",
        })
with conn() as c:
    picks = {contacts.pick(c, random.Random(s)) for s in range(30)}
assert picks == {"(954) 222-2222"}, picks
ok.append("rotation picks the least-used number, so a new one enters circulation at once")

# --- and ties are broken randomly rather than marching down the list -------
with tx() as c:
    c.execute("DELETE FROM drafts")
with conn() as c:
    spread = {contacts.pick(c, random.Random(s)) for s in range(40)}
assert len(spread) > 1, f"all picks identical when every number is unused: {spread}"
ok.append("with no history the pick spreads across every active number")

# --- no active numbers is survivable, not a crash --------------------------
with tx() as c:
    c.execute("UPDATE contact_numbers SET active = FALSE")
with conn() as c:
    assert contacts.pick(c) is None
ok.append("with everything retired the picker returns None so generation can fall back")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
