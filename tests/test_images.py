"""Image stack: storage, the pending shelf, and the per-account claim.

The claim is the one with real consequences — Craigslist notices the same photo
appearing under different sellers, so an image bound to craigs1 must never be
usable by craigs2. These assert that, not just that the column gets written.
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
        images_svc.attach(c, draft_id=d_a["id"], image_id=pending["id"], slot=1)
        raise AssertionError("attached an unapproved image")
    except ValueError as e:
        assert "not approved" in str(e), e
ok.append("an image on the pending shelf cannot be attached to a draft")

# --- attaching claims the image for that account, permanently --------------
with tx() as c:
    images_svc.set_status(c, pending["id"], "approved")
    r = images_svc.attach(c, draft_id=d_a["id"], image_id=pending["id"], slot=1)
assert r["owner_account"] == "craigs1"
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s", (pending["id"],)).fetchone()
assert got["owner_account"] == "craigs1"
ok.append("attaching binds the image to that draft's account")

# --- and another account is refused ----------------------------------------
with tx() as c:
    try:
        images_svc.attach(c, draft_id=d_b["id"], image_id=pending["id"], slot=1)
        raise AssertionError("a second account reused a claimed image")
    except ValueError as e:
        assert "craigs1" in str(e), e
ok.append("a claimed image is refused to any other account")

# --- pick_for_draft respects ownership -------------------------------------
with conn() as c:
    for_a = images_svc.pick_for_draft(c, account="craigs1", count=10)
    for_b = images_svc.pick_for_draft(c, account="craigs2", count=10)
assert pending["id"] in [i["id"] for i in for_a]
assert pending["id"] not in [i["id"] for i in for_b], "claimed image offered to the wrong account"
ok.append("the picker never offers another account's claimed image")

# --- detach releases only while unpublished --------------------------------
with tx() as c:
    assert images_svc.detach(c, draft_id=d_a["id"], image_id=pending["id"])
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s", (pending["id"],)).fetchone()
assert got["owner_account"] is None, "detach did not release an unpublished image"

with tx() as c:
    images_svc.attach(c, draft_id=d_a["id"], image_id=pending["id"], slot=1)
    images_svc.mark_used(c, [pending["id"]])
    images_svc.detach(c, draft_id=d_a["id"], image_id=pending["id"])
with conn() as c:
    got = c.execute("SELECT owner_account, used_at FROM images WHERE id=%s",
                    (pending["id"],)).fetchone()
assert got["used_at"] is not None
assert got["owner_account"] == "craigs1", \
    "a published image was released back to the pool - it could reappear under another account"
ok.append("detach releases an unpublished image but never a published one")

# --- published images stay inside the reuse cooldown -----------------------
with conn() as c:
    fresh = images_svc.pick_for_draft(c, account="craigs1", count=10)
assert pending["id"] not in [i["id"] for i in fresh], "just-published image offered again"
with tx() as c:
    c.execute("UPDATE images SET used_at = NOW() - INTERVAL '31 days' WHERE id=%s",
              (pending["id"],))
with conn() as c:
    aged = images_svc.pick_for_draft(c, account="craigs1", count=10)
assert pending["id"] in [i["id"] for i in aged], "image still blocked after 30 days"
ok.append("reuse cooldown blocks for 30 days, then allows it again")

# --- deleting keeps the bytes of anything already published ----------------
with conn() as c:
    path = storage.open_path(
        c.execute("SELECT storage_path FROM images WHERE id=%s", (pending["id"],)).fetchone()["storage_path"]
    )
with tx() as c:
    images_svc.delete_image(c, pending["id"])
assert path.exists(), "bytes of a published image were deleted; the live ad is now unauditable"
ok.append("deleting a published image keeps its bytes for audit")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
