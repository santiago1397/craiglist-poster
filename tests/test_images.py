"""Image stack: storage, the pending shelf, and the per-account claim.

The claim used to be absolute — Craigslist notices the same photo appearing
under different sellers, so an image bound to craigs1 could never be used by
craigs2. Migration 0027 made it an operator setting
(`guardrail_settings.image_owner_binding`) and it now ships **off**, so the same
picture can go out under several accounts once its cooldown expires.

That makes the flag itself the thing worth testing, in both positions. The
`image_owner_binding = TRUE` half is not legacy coverage: it is the revert path,
and a revert nobody exercises is not a revert. If duplicate photos ever start
getting ads ghosted, turning that flag back on is the fix, and these assertions
are what say it still works.

The same goes for the cooldown. It was 30 days and is now 7, so the old fixture
— age a row 31 days, assert it is offered — would still pass while asserting
nothing at all. The probes below straddle whatever `reuse_cooldown_days` says.
"""
import os
import tempfile

os.environ.setdefault("IMAGES_DIR", tempfile.mkdtemp())

from app import storage  # noqa: E402
from app.db import conn, init_pool, tx  # noqa: E402
from app.services import drafts as drafts_svc  # noqa: E402
from app.services import images as images_svc  # noqa: E402

init_pool()
ok = []


def set_binding(on: bool) -> None:
    """Flip the account claim, the way Settings → Guardrails does."""
    with tx() as c:
        c.execute(
            "UPDATE guardrail_settings SET image_owner_binding = %s WHERE singleton",
            (on,),
        )


def set_cooldown(days: int) -> None:
    with tx() as c:
        c.execute(
            "UPDATE guardrail_settings SET image_reuse_cooldown_days = %s "
            "WHERE singleton",
            (days,),
        )


with conn() as c:
    _ORIGINAL_BINDING = images_svc.owner_binding_enabled(c)
    _ORIGINAL_COOLDOWN = images_svc.reuse_cooldown_days(c)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes for testing" * 4
PNG2 = b"\x89PNG\r\n\x1a\n" + b"a different picture entirely" * 4

with tx() as c:
    c.execute("TRUNCATE draft_images, images, drafts CASCADE")

# --- storage is content-addressed and idempotent ---------------------------
d1, rel1, size1 = storage.put_bytes(PNG, "image/png")
d2, rel2, _ = storage.put_bytes(PNG, "image/png")
assert d1 == d2 and rel1 == rel2, "same bytes produced different addresses"
assert storage.open_path(rel1).read_bytes() == PNG
assert len(d1) == 64 and size1 == len(PNG)
ok.append("storage is content-addressed; identical bytes reuse one file")

try:
    storage.open_path("../../etc/passwd")
    raise AssertionError("path traversal was not blocked")
except ValueError:
    pass
ok.append("path traversal outside the storage root is refused")

# --- generation without a key fails softly ---------------------------------
with tx() as c:
    res = images_svc.generate_images(c, count=2)
assert res["created"] == 0 and res["error"], res
assert "MINIMAX_API_KEY" in res["error"], res["error"]
assert res["cost_usd"] == 0.0
ok.append("generation without an API key returns an error instead of raising")

# --- uploads land approved; duplicates are rejected ------------------------
with tx() as c:
    up = images_svc.store_upload(c, PNG, mime="image/png")
assert up and up["status"] == "approved" and up["source"] == "uploaded"
with tx() as c:
    dupe = images_svc.store_upload(c, PNG, mime="image/png")
assert dupe is None, "the same bytes were stored twice"
ok.append("uploads are approved immediately; identical bytes are not duplicated")

# --- a pending image cannot be attached ------------------------------------
with tx() as c:
    pending = images_svc._store(c, PNG2, source="generated", kind="photo")
    d_a = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})
    d_b = drafts_svc.create_draft(c, {"account": "craigs2", "title": "t", "body": "b"})
assert pending["status"] == "pending"
with tx() as c:
    try:
        # Slot 2: slot 1 takes a cover, and this is a photo.
        images_svc.attach(c, draft_id=d_a["id"], image_id=pending["id"], slot=2)
        raise AssertionError("attached an unapproved image")
    except ValueError as e:
        assert "not approved" in str(e), e
ok.append("an image on the pending shelf cannot be attached to a draft")

# === WITH BINDING ON — the revert path ======================================
# Everything down to the detach block runs with image_owner_binding = TRUE.
# This is the behaviour an operator gets back by flipping one setting, so it has
# to keep working even though the shipped default is off.
set_binding(True)

# --- attaching claims the image for that account, permanently --------------
with tx() as c:
    images_svc.set_status(c, pending["id"], "approved")
    r = images_svc.attach(c, draft_id=d_a["id"], image_id=pending["id"], slot=2)
assert r["owner_account"] == "craigs1"
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s", (pending["id"],)).fetchone()
assert got["owner_account"] == "craigs1"
ok.append("binding ON: attaching binds the image to that draft's account")

# --- and another account is refused ----------------------------------------
with tx() as c:
    try:
        images_svc.attach(c, draft_id=d_b["id"], image_id=pending["id"], slot=2)
        raise AssertionError("a second account reused a claimed image")
    except ValueError as e:
        assert "craigs1" in str(e), e
ok.append("binding ON: a claimed image is refused to any other account")

# --- an image held by a live draft is out of the pool ----------------------
# draft_images has no uniqueness on image_id, so without this the same photo
# was handed to several queued drafts at once and both published it.
with conn() as c:
    for_a = images_svc.pick_for_draft(c, account="craigs1", count=10)
assert pending["id"] not in [i["id"] for i in for_a], \
    "an image attached to a queued draft was offered to another draft"
with conn() as c:
    assert images_svc.reserved_by(c, pending["id"]) == d_a["id"]
ok.append("an image reserved by a live draft is never offered again")

# --- but the reservation is bypassable on purpose --------------------------
with tx() as c:
    d_c = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})
    try:
        images_svc.attach(c, draft_id=d_c["id"], image_id=pending["id"], slot=2)
        raise AssertionError("double-booked an image without being asked")
    except ValueError as e:
        assert "already attached" in str(e), e
    images_svc.attach(c, draft_id=d_c["id"], image_id=pending["id"], slot=2,
                      allow_double_book=True)
with tx() as c:
    images_svc.detach(c, draft_id=d_c["id"], image_id=pending["id"])
ok.append("double-booking is refused by default and possible on request")

# --- detach releases only while unpublished --------------------------------
with tx() as c:
    assert images_svc.detach(c, draft_id=d_a["id"], image_id=pending["id"])
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s", (pending["id"],)).fetchone()
assert got["owner_account"] is None, "detach did not release an unpublished image"

with tx() as c:
    images_svc.attach(c, draft_id=d_a["id"], image_id=pending["id"], slot=2)
    images_svc.mark_used(c, [pending["id"]])
    images_svc.detach(c, draft_id=d_a["id"], image_id=pending["id"])
with conn() as c:
    got = c.execute("SELECT owner_account, used_at FROM images WHERE id=%s",
                    (pending["id"],)).fetchone()
assert got["used_at"] is not None
assert got["owner_account"] == "craigs1", \
    "a published image was released back to the pool - it could reappear under another account"
ok.append("binding ON: detach releases an unpublished image but never a published one")

# === WITH BINDING OFF — what actually ships =================================
# The published image above is still stamped owner_account='craigs1'. With the
# flag off that stamp must stop being enforced — otherwise every image attached
# before the change stays locked to one account forever and the loosening does
# nothing for the existing pool.
set_binding(False)
with tx() as c:
    c.execute("UPDATE images SET used_at = NULL WHERE id=%s", (pending["id"],))
    r2 = images_svc.attach(c, draft_id=d_b["id"], image_id=pending["id"], slot=2,
                           allow_double_book=True)
assert r2["owner_account"] == "craigs1", \
    "the historic claim was overwritten; flipping binding back on would be a lie"
ok.append("binding OFF: a second account may use an image claimed by the first")

# --- and the stamp is preserved, so the revert is real ---------------------
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s",
                    (pending["id"],)).fetchone()
assert got["owner_account"] == "craigs1", "existing owner_account was wiped"
set_binding(True)
with tx() as c:
    try:
        images_svc.attach(c, draft_id=d_b["id"], image_id=pending["id"], slot=3,
                          allow_double_book=True)
        raise AssertionError("binding was turned back on but is not enforced")
    except ValueError as e:
        assert "craigs1" in str(e), e
ok.append("the revert works: turning binding back on re-enforces the old claims")

# --- a fresh attach under binding OFF records no claim ---------------------
# Gating the read without gating the write would leave new images silently
# stamped, so flipping binding back on would lock pictures two accounts had
# already both used.
set_binding(False)
with tx() as c:
    unowned = images_svc._store(c, PNG2 + b"third", source="generated", kind="photo")
    images_svc.set_status(c, unowned["id"], "approved")
    r3 = images_svc.attach(c, draft_id=d_a["id"], image_id=unowned["id"], slot=4)
assert r3["owner_account"] is None, "attach reported a claim it did not write"
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s",
                    (unowned["id"],)).fetchone()
assert got["owner_account"] is None, "binding is off but a claim was still written"
ok.append("binding OFF: no claim is written, so the flag reads and writes agree")

# --- published images stay inside the reuse cooldown -----------------------
# Probes straddle the configured value rather than a hardcoded 31 days, which
# at a 7-day cooldown would assert nothing.
#
# Release the cross-account attachment made above first. An image held by a live
# draft is excluded by the reservation whatever its age, so leaving it attached
# would make every probe below pass for the wrong reason — including the one
# asserting the cooldown has expired.
with tx() as c:
    images_svc.detach(c, draft_id=d_b["id"], image_id=pending["id"])
with conn() as c:
    assert images_svc.reserved_by(c, pending["id"]) is None, \
        "the cooldown probes would be measuring the reservation, not the cooldown"

set_cooldown(7)
with conn() as c:
    days = images_svc.reuse_cooldown_days(c)
assert days == 7
with tx() as c:
    c.execute("UPDATE images SET used_at = NOW() WHERE id=%s", (pending["id"],))
with conn() as c:
    fresh = images_svc.pick_for_draft(c, account="craigs1", count=10)
assert pending["id"] not in [i["id"] for i in fresh], "just-published image offered again"

with tx() as c:
    c.execute(
        "UPDATE images SET used_at = NOW() - make_interval(days => %s) WHERE id=%s",
        (days - 3, pending["id"]),
    )
with conn() as c:
    inside = images_svc.pick_for_draft(c, account="craigs1", count=10)
assert pending["id"] not in [i["id"] for i in inside], \
    f"an image used {days - 3} days ago was offered inside a {days}-day cooldown"

with tx() as c:
    c.execute(
        "UPDATE images SET used_at = NOW() - make_interval(days => %s) WHERE id=%s",
        (days + 1, pending["id"]),
    )
with conn() as c:
    aged = images_svc.pick_for_draft(c, account="craigs1", count=10)
assert pending["id"] in [i["id"] for i in aged], \
    f"an image used {days + 1} days ago is still blocked by a {days}-day cooldown"
ok.append("the reuse cooldown tracks the configured value on both sides")

# --- deleting keeps the bytes of anything already published ----------------
with conn() as c:
    path = storage.open_path(
        c.execute("SELECT storage_path FROM images WHERE id=%s", (pending["id"],)).fetchone()["storage_path"]
    )
with tx() as c:
    images_svc.delete_image(c, pending["id"])
assert path.exists(), "bytes of a published image were deleted; the live ad is now unauditable"
ok.append("deleting a published image keeps its bytes for audit")

# Put the operator's settings back. This test drives real guardrails on whatever
# database it is pointed at, and leaving binding flipped would quietly change
# how the next posting run picks pictures.
set_binding(_ORIGINAL_BINDING)
set_cooldown(_ORIGINAL_COOLDOWN)

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
