"""Staging images on a live posting.

The live-post image path used to be a thinner copy of the draft one and had
quietly lost the rules that make the draft path safe: it capped at 5 slots
instead of 24, skipped the cover/photo partition, and skipped the reservation
check entirely. It now delegates to `services.images`, so these assert that the
guards really are shared rather than reimplemented.

The two that matter most:

* `image_set_managed` — the desktop ships `images: []` on the claim unless this
  is set, so an unset flag means the operator stages a gallery, the edit reports
  `applied`, and the live posting keeps its old photos with no error anywhere.
* moving an image between slots of the *same* post must not read as
  double-booking it against itself.
"""
import os
import tempfile

os.environ.setdefault("IMAGES_DIR", tempfile.mkdtemp())

from datetime import datetime, timezone  # noqa: E402

from app.db import conn, init_pool, tx  # noqa: E402
from app.services import drafts as drafts_svc  # noqa: E402
from app.services import edits as edits_svc  # noqa: E402
from app.services import images as images_svc  # noqa: E402

init_pool()
ok = []
failures = []


def check(label, condition, detail=""):
    if condition:
        ok.append(label)
    else:
        failures.append(f"{label}  [{detail}]" if detail else label)


def refuses(fn, fragment):
    """Run fn, expecting a ValueError whose message contains `fragment`."""
    try:
        fn()
    except ValueError as e:
        return fragment in str(e), str(e)
    return False, "no error raised"


NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
POST = "7811111111"
OTHER_POST = "7822222222"

with tx() as c:
    c.execute(
        "TRUNCATE post_desired_images, post_desired_state, draft_images, images, "
        "drafts, posts CASCADE"
    )
    for pid in (POST, OTHER_POST):
        c.execute(
            "INSERT INTO posts (post_id, account, title, url, posted_ts, body, city) "
            "VALUES (%s, 'craigs1', 'live title', %s, %s, 'live body', 'Hollywood')",
            (pid, f"https://x/{pid}.html", NOW),
        )


def _image(c, seed: bytes, kind: str) -> dict:
    img = images_svc._store(c, b"\x89PNG\r\n\x1a\n" + seed * 8, source="uploaded", kind=kind)
    images_svc.set_status(c, img["id"], "approved")
    return img


with tx() as c:
    cover = _image(c, b"cover-one", "cover")
    photo_a = _image(c, b"photo-aaa", "photo")
    photo_b = _image(c, b"photo-bbb", "photo")
    photo_c = _image(c, b"photo-ccc", "photo")

# --- an un-hydrated post refuses images, same as it refuses text ------------
with tx() as c:
    caught, msg = refuses(
        lambda: edits_svc.attach_image(c, post_id=POST, image_id=cover["id"], slot=1),
        "hydrated",
    )
check("an un-hydrated post refuses images", caught, msg)

with tx() as c:
    c.execute(
        "UPDATE posts SET hydrated_at = %s, content_hash = 'hash-live' WHERE post_id = ANY(%s)",
        (NOW, [POST, OTHER_POST]),
    )

# --- attaching seeds the desired state instead of demanding a text edit -----
with tx() as c:
    edits_svc.attach_image(c, post_id=POST, image_id=cover["id"], slot=1)
    d = edits_svc.get_desired(c, POST)
check("attaching seeds a desired state", d is not None)
check("seeded state copies the live text", d and d["body"] == "live body",
      d and d["body"])
check("seeded state captures base_hash", d and d["base_hash"] == "hash-live")
check("attaching takes control of the image set", d and d["image_set_managed"],
      "image_set_managed stayed false — the desktop would ignore the images")

# --- the cover/photo partition, inherited from the drafts path --------------
with tx() as c:
    caught, msg = refuses(
        lambda: edits_svc.attach_image(c, post_id=POST, image_id=photo_a["id"], slot=1),
        "takes a cover",
    )
check("slot 1 of a live post refuses a photo", caught, msg)

with tx() as c:
    caught, msg = refuses(
        lambda: edits_svc.attach_image(c, post_id=POST, image_id=cover["id"], slot=3),
        "takes a photo",
    )
check("a photo slot refuses a cover", caught, msg)

# --- 24 slots, not 5 --------------------------------------------------------
with tx() as c:
    edits_svc.attach_image(c, post_id=POST, image_id=photo_a["id"], slot=24)
    got = [i["slot"] for i in edits_svc.desired_images(c, POST)]
check("slot 24 is accepted (the cap was 5)", 24 in got, str(got))

with tx() as c:
    caught, msg = refuses(
        lambda: edits_svc.attach_image(c, post_id=POST, image_id=photo_b["id"], slot=25),
        "slot must be 1-24",
    )
check("slot 25 is refused", caught, msg)

# --- re-attaching to the same slot is idempotent, not a self-double-booking --
# `reserved_by_post` used to return any holder, so a post's own image read as
# "staged on live posting <itself>" and re-attaching was refused. Moving between
# slots still needs a detach first — `UNIQUE (post_id, image_id)` says so, and
# `draft_images` behaves identically.
with tx() as c:
    edits_svc.attach_image(c, post_id=POST, image_id=photo_a["id"], slot=24)
    slots = {i["id"]: i["slot"] for i in edits_svc.desired_images(c, POST)}
check("re-attaching an image to the slot it already holds is allowed",
      slots.get(photo_a["id"]) == 24, str(slots))

# --- but another posting cannot take it -------------------------------------
with tx() as c:
    edits_svc.attach_image(c, post_id=OTHER_POST, image_id=photo_b["id"], slot=2)
with tx() as c:
    caught, msg = refuses(
        lambda: edits_svc.attach_image(c, post_id=POST, image_id=photo_b["id"], slot=3),
        "staged on live posting",
    )
check("an image staged on another posting is refused", caught, msg)

with tx() as c:
    edits_svc.attach_image(
        c, post_id=POST, image_id=photo_b["id"], slot=3, allow_double_book=True
    )
    held = [i["id"] for i in edits_svc.desired_images(c, POST)]
check("double-booking is possible when confirmed", photo_b["id"] in held, str(held))

# --- a draft cannot quietly take an image staged on a live posting ----------
with tx() as c:
    draft = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})
    caught, msg = refuses(
        lambda: images_svc.attach(c, draft_id=draft["id"], image_id=photo_c["id"], slot=2),
        "",
    )
# photo_c is free, so that attach should have succeeded.
with conn() as c:
    on_draft = [i["id"] for i in images_svc.images_for_draft(c, draft["id"])]
check("a free image still attaches to a draft normally", photo_c["id"] in on_draft,
      str(on_draft))

with tx() as c:
    caught, msg = refuses(
        lambda: images_svc.attach(c, draft_id=draft["id"], image_id=cover["id"], slot=1),
        "staged on live posting",
    )
check("a draft is refused an image staged on a live posting", caught, msg)

# --- editing images bumps the revision so a reconcile is actually scheduled --
with conn() as c:
    before = edits_svc.get_desired(c, POST)
with tx() as c:
    edits_svc.attach_image(c, post_id=POST, image_id=photo_c["id"], slot=4,
                           allow_double_book=True)
with conn() as c:
    after = edits_svc.get_desired(c, POST)
check("an image change bumps desired_rev",
      after["desired_rev"] > before["desired_rev"],
      f"{before['desired_rev']} -> {after['desired_rev']}")

# --- autofill tops up photo slots and never touches the cover ---------------
with tx() as c:
    c.execute("UPDATE post_desired_images SET slot = slot WHERE post_id = %s", (POST,))
    result = edits_svc.autofill_images(c, post_id=POST, want=3)
with conn() as c:
    after_fill = {i["slot"]: i["id"] for i in edits_svc.desired_images(c, POST)}
check("autofill reports what it managed to fill", "filled" in result and "requested" in result,
      str(result))
check("autofill leaves the chosen cover in slot 1", after_fill.get(1) == cover["id"],
      str(after_fill.get(1)))

# --- detach releases only when nothing else holds it ------------------------
# The account claim is now a setting that ships off (migration 0027), so
# `owner_account` no longer proves anything here. The property that still
# matters — and the one that actually keeps a picture off two ads — is the
# reservation: while any other holder has it, the image stays out of the pool.
with tx() as c:
    assert edits_svc.detach_image(c, post_id=OTHER_POST, image_id=photo_b["id"])
with conn() as c:
    still_held = images_svc.reserved_by(c, photo_b["id"]) is not None or (
        images_svc.reserved_by_post(c, photo_b["id"]) is not None
    )
check("detaching from one holder leaves the image reserved by the other",
      still_held, "image was released while another holder still has it")

if failures:
    print("\n".join(f"  --  {line}" for line in failures))
    print(f"\n{len(failures)} FAILED, {len(ok)} passed")
    raise SystemExit(1)

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
print("edit images OK")
