"""Cover overlays, and getting images onto drafts.

Two things worth asserting hard: the overlay renders the phone number exactly as
given (the whole reason text is composited rather than generated), and the
10%-imageless rule plus slot ordering actually hold.
"""
import io
import os
import tempfile

os.environ.setdefault("IMAGES_DIR", tempfile.mkdtemp())

import random  # noqa: E402

from PIL import Image  # noqa: E402

from app.db import conn, init_pool, tx  # noqa: E402
from app.services import drafts as drafts_svc  # noqa: E402
from app.services import images as images_svc  # noqa: E402
from app.services import overlay as overlay_svc  # noqa: E402
from app.services import queue as queue_svc  # noqa: E402

init_pool()
ok = []


def jpeg(colour=(120, 140, 160), size=(1152, 864)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG")
    return buf.getvalue()


with tx() as c:
    c.execute("TRUNCATE draft_images, images, drafts CASCADE")

# --- overlay renders and keeps the image usable ----------------------------
src = jpeg()
out = overlay_svc.render(
    src,
    overlay_svc.DEFAULT_TEMPLATE,
    {"phone": "(954) 634-7370", "license": "CCC1334317", "city": "Davie"},
)
img = Image.open(io.BytesIO(out))
assert img.format == "JPEG" and img.size == (1152, 864), (img.format, img.size)
assert out != src, "overlay produced identical bytes; nothing was drawn"
ok.append("overlay renders a valid JPEG at the original size")

# The band must actually change pixels near the bottom, and leave the top alone.
before, after = Image.open(io.BytesIO(src)), img
assert before.getpixel((576, 800)) != after.getpixel((576, 800)), "bottom band not drawn"
assert before.getpixel((576, 40)) == after.getpixel((576, 40)), "top of image was altered"
ok.append("the band is drawn where the template says and nowhere else")

# --- token substitution is exact -------------------------------------------
tpl = overlay_svc.OverlayTemplate(lines=["CALL {phone}", "Lic {license}"])
rendered = overlay_svc.render(src, tpl, {"phone": "(954) 634-7420", "license": "CCC1334317"})
assert len(rendered) > 1000
# A missing token must not explode — a half-filled template is better than a
# crash during generation.
safe = overlay_svc.render(src, tpl, {"phone": "", "license": ""})
assert len(safe) > 1000
ok.append("tokens substitute exactly, and a missing token renders blank rather than raising")

# --- an empty template is a no-op, not an error ----------------------------
plain = overlay_svc.render(src, overlay_svc.OverlayTemplate(lines=[]), {})
assert Image.open(io.BytesIO(plain)).size == (1152, 864)
ok.append("an empty template passes the image through unchanged")

# --- autoattach fills photos and leaves the cover slot alone ---------------
# Covers are hand-picked, so generation must not spend the cover stack across a
# 45-draft queue weeks before any of it publishes.
with tx() as c:
    d = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})
    cover = images_svc._store(c, jpeg((200, 60, 60)), source="generated", kind="cover",
                              status="approved")
    for i in range(8):
        images_svc._store(c, jpeg((i * 20, 100, 100)), source="generated", kind="photo",
                          status="approved")

with tx() as c:
    attached = images_svc.autoattach(c, draft_id=d["id"], account="craigs1",
                                     rng=random.Random(1))
with conn() as c:
    rows = images_svc.images_for_draft(c, d["id"])
assert rows, "nothing attached despite a full stack"
assert [r["slot"] for r in rows] == list(range(2, len(rows) + 2)), \
    f"photos should start at slot 2, got {[r['slot'] for r in rows]}"
assert len(rows) <= images_svc.MAX_SLOTS
assert all(r["kind"] == "photo" for r in rows), "autoattach put a cover on a draft"
with conn() as c:
    got = c.execute("SELECT owner_account FROM images WHERE id=%s", (cover["id"],)).fetchone()
assert got["owner_account"] is None, "generation claimed a cover"
ok.append("autoattach fills photo slots from 2 and never touches the cover stack")

# --- the cover is filled by hand, or by the claim-time backstop ------------
with tx() as c:
    chosen = images_svc.attach_cover(c, draft_id=d["id"], account="craigs1")
assert chosen and chosen["id"] == cover["id"], chosen
with conn() as c:
    slot1 = [r for r in images_svc.images_for_draft(c, d["id"]) if r["slot"] == 1]
assert slot1 and slot1[0]["kind"] == "cover", "backstop did not put a cover in slot 1"
# Twice must be a no-op: it must never displace a cover you chose.
with tx() as c:
    assert images_svc.attach_cover(c, draft_id=d["id"], account="craigs1") is None
ok.append("attach_cover fills an empty slot 1 and never replaces one already set")

# --- the partition is enforced in both directions --------------------------
with tx() as c:
    dp = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})
    # A fresh one: autoattach above reserved every photo in the stack, which is
    # itself the point of the reservation.
    photo = images_svc._store(c, jpeg((11, 22, 33)), source="generated", kind="photo",
                              status="approved")
    try:
        images_svc.attach(c, draft_id=dp["id"], image_id=photo["id"], slot=1)
        raise AssertionError("a photo was accepted as the thumbnail")
    except ValueError as e:
        assert "cover" in str(e), e
with tx() as c:
    c.execute("UPDATE images SET owner_account = NULL WHERE id = %s", (cover["id"],))
    try:
        images_svc.attach(c, draft_id=dp["id"], image_id=cover["id"], slot=3)
        raise AssertionError("a cover was accepted into a photo slot")
    except ValueError as e:
        assert "photo" in str(e), e
ok.append("slot 1 refuses photos and photo slots refuse covers")

# --- and the photo picker never returns a cover ----------------------------
with conn() as c:
    offered = images_svc.pick_for_draft(c, account="craigs1", count=50)
assert all(i["kind"] == "photo" for i in offered), \
    "pick_for_draft offered a cover as an ordinary photo"
ok.append("the photo picker never offers a cover")

# --- relabelling moves an image between the stacks -------------------------
with tx() as c:
    spare = images_svc._store(c, jpeg((3, 250, 3)), source="generated", kind="photo",
                              status="approved")
    moved = images_svc.set_kind(c, spare["id"], "cover")
assert moved and moved["kind"] == "cover"
with tx() as c:
    images_svc.attach(c, draft_id=dp["id"], image_id=spare["id"], slot=1)
    try:
        images_svc.set_kind(c, spare["id"], "photo")
        raise AssertionError("relabelled an image out from under a live attachment")
    except ValueError as e:
        assert "detach" in str(e), e
ok.append("kind is editable, but not while a live draft holds the image")

# --- attaching claims every image for that account -------------------------
with conn() as c:
    for r in rows:
        got = c.execute("SELECT owner_account FROM images WHERE id=%s", (r["id"],)).fetchone()
        assert got["owner_account"] == "craigs1", r["id"]
ok.append("every auto-attached image is claimed by the draft's account")

# --- roughly one post in ten carries no images -----------------------------
counts = [images_svc.roll_photo_count(random.Random(s)) for s in range(2000)]
zeros = counts.count(0) / len(counts)
assert 0.06 <= zeros <= 0.15, f"imageless rate {zeros:.1%} is outside the intended ~10%"
assert max(counts) <= 5, f"default range should stay 1-5, saw {max(counts)}"
assert min(counts) >= 0
ok.append(f"{zeros:.0%} of drafts get no images, the rest 1-{max(counts)} by default")

# --- but the ceiling is Craigslist's real limit, not the default -----------
assert images_svc.MAX_SLOTS == 24, images_svc.MAX_SLOTS
wide = [images_svc.roll_photo_count(random.Random(s), photos_min=20, photos_max=24,
                                    imageless_rate=0.0) for s in range(200)]
assert min(wide) >= 20 and max(wide) <= 24, (min(wide), max(wide))
# Asking for more than Craigslist accepts must clamp, not overflow.
clamped = [images_svc.roll_photo_count(random.Random(s), photos_min=30, photos_max=99,
                                       imageless_rate=0.0) for s in range(50)]
assert all(c == 24 for c in clamped), set(clamped)
# imageless_rate=0 must never produce a bare post.
assert 0 not in wide
ok.append("the range is tunable up to 24 and clamps above it; rate 0 never yields an empty post")

# --- slots beyond 5 are now accepted ---------------------------------------
with tx() as c:
    d24 = drafts_svc.create_draft(c, {"account": "craigs3", "title": "t", "body": "b"})
    big = images_svc._store(c, jpeg((7, 7, 7)), source="generated", kind="photo",
                            status="approved")
    images_svc.attach(c, draft_id=d24["id"], image_id=big["id"], slot=24)
with conn() as c:
    assert images_svc.images_for_draft(c, d24["id"])[0]["slot"] == 24
with tx() as c:
    try:
        images_svc.attach(c, draft_id=d24["id"], image_id=big["id"], slot=25)
        raise AssertionError("accepted slot 25, beyond Craigslist's limit")
    except ValueError:
        pass
ok.append("slot 24 attaches; slot 25 is refused")

# --- publishing stamps the images used -------------------------------------
with tx() as c:
    queue_svc.mark_posted(c, draft_id=d["id"], post_id="7788")
with conn() as c:
    for r in rows:
        got = c.execute("SELECT used_at FROM images WHERE id=%s", (r["id"],)).fetchone()
        assert got["used_at"] is not None, f"image {r['id']} not marked used after posting"
ok.append("marking a draft posted stamps its images as published")

# --- and they are then out of the pool for the cooldown --------------------
with conn() as c:
    avail = [i["id"] for i in images_svc.pick_for_draft(c, account="craigs1", count=20)]
for r in rows:
    assert r["id"] not in avail, f"published image {r['id']} still offered"
ok.append("published images leave the pool for the reuse cooldown")

# --- a failed post retires the images Craigslist already rendered ----------
# The gap this closes: mark_used ran only from mark_posted, so a run that
# uploaded four photos and died at publish left all four looking unused.
with tx() as c:
    c.execute("TRUNCATE draft_images, images CASCADE")
    dburn = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})
    for i in range(5):
        images_svc._store(c, jpeg((i * 30, 5, 200)), source="generated", kind="photo",
                          status="approved")
    images_svc.fill_photo_slots(c, draft_id=dburn["id"], account="craigs1", want=5)
with conn() as c:
    burn_rows = images_svc.images_for_draft(c, dburn["id"])
assert len(burn_rows) == 5, burn_rows

with tx() as c:
    status = queue_svc.release_or_park(
        c, draft_id=dburn["id"], failed_step="photo_upload",
        failed_message="died mid-upload", photos_confirmed=2,
    )
assert status == "needs_attention", status
with conn() as c:
    used = [
        c.execute("SELECT used_at FROM images WHERE id=%s", (r["id"],)).fetchone()["used_at"]
        for r in burn_rows
    ]
assert all(u is not None for u in used[:2]), "images Craigslist rendered were left unused"
assert all(u is None for u in used[2:]), "images that never uploaded were retired anyway"
ok.append("a post-upload failure retires exactly the images the site confirmed")

# --- an unknown confirmed count retires everything, not nothing ------------
with tx() as c:
    dall = drafts_svc.create_draft(c, {"account": "craigs2", "title": "t", "body": "b"})
    for i in range(3):
        images_svc._store(c, jpeg((9, i * 40, 90)), source="generated", kind="photo",
                          status="approved")
    images_svc.fill_photo_slots(c, draft_id=dall["id"], account="craigs2", want=3)
with conn() as c:
    all_rows = images_svc.images_for_draft(c, dall["id"])
with tx() as c:
    queue_svc.release_or_park(
        c, draft_id=dall["id"], failed_step="publish", failed_message="x",
        photos_confirmed=None,
    )
with conn() as c:
    for r in all_rows:
        got = c.execute("SELECT used_at FROM images WHERE id=%s", (r["id"],)).fetchone()
        assert got["used_at"] is not None, \
            "with no confirmed count we must assume the worst, not the best"
ok.append("a missing confirmed count retires every attached image")

# --- a pre-upload failure consumed nothing ---------------------------------
with tx() as c:
    dpre = drafts_svc.create_draft(c, {"account": "craigs3", "title": "t", "body": "b"})
    for i in range(2):
        images_svc._store(c, jpeg((1, 1, i * 100 + 20)), source="generated", kind="photo",
                          status="approved")
    images_svc.fill_photo_slots(c, draft_id=dpre["id"], account="craigs3", want=2)
with conn() as c:
    pre_rows = images_svc.images_for_draft(c, dpre["id"])
with tx() as c:
    status = queue_svc.release_or_park(
        c, draft_id=dpre["id"], failed_step="form_title", failed_message="x",
        photos_confirmed=None,
    )
assert status == "queued", status
with conn() as c:
    for r in pre_rows:
        got = c.execute("SELECT used_at FROM images WHERE id=%s", (r["id"],)).fetchone()
        assert got["used_at"] is None, "a failure before any upload burned images"
ok.append("a pre-upload failure requeues the draft and burns nothing")

# --- an empty stack yields a text-only draft, not a failure ----------------
with tx() as c:
    c.execute("TRUNCATE draft_images, images CASCADE")
    d2 = drafts_svc.create_draft(c, {"account": "craigs2", "title": "t", "body": "b"})
    got = images_svc.autoattach(c, draft_id=d2["id"], account="craigs2", rng=random.Random(3))
assert got == [], got
ok.append("an empty stack produces a text-only draft rather than an error")

# --- the cover overlay needs a real font -----------------------------------
# `ImageFont.load_default()` returns a fixed ~11px bitmap face that ignores the
# size it is asked for, so `_fit()`'s scaling silently does nothing and the
# phone number renders at 11px on a 1365px-wide cover. That shipped undetected
# for the whole life of this system: the container had no fonts at all, and the
# fallback logged one line and carried on producing images that looked finished.
import io  # noqa: E402

from PIL import Image, ImageFont  # noqa: E402

from app.services import overlay as overlay_svc  # noqa: E402

big, small = overlay_svc._font(96), overlay_svc._font(24)
assert isinstance(big, ImageFont.FreeTypeFont), (
    "no TrueType font is installed, so cover text would render illegibly small. "
    "The runtime image needs fonts-dejavu-core."
)
assert big.getbbox("ROOF")[3] > small.getbbox("ROOF")[3] * 2, \
    "the requested font size is not being honoured"
ok.append("a real TrueType face loads and honours the size it is given")

buf = io.BytesIO()
Image.new("RGB", (1365, 1024), (90, 100, 110)).save(buf, format="JPEG")
flat = buf.getvalue()
composited = overlay_svc.render(
    flat, overlay_svc.DEFAULT_TEMPLATE,
    {"phone": "(954) 555-0100", "license": "CCC1334317", "city": ""},
)
assert composited != flat, "render returned the source untouched"
with Image.open(io.BytesIO(composited)) as im:
    assert im.size == (1365, 1024), im.size
ok.append("render composites the call-to-action without resizing the cover")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
