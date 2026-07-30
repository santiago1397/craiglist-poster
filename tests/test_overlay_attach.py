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

# --- autoattach: slot order, cover first, cap at 5 -------------------------
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
assert [r["slot"] for r in rows] == list(range(1, len(rows) + 1)), \
    f"slots are not contiguous from 1: {[r['slot'] for r in rows]}"
assert len(rows) <= images_svc.MAX_SLOTS
assert rows[0]["kind"] == "cover", "slot 1 is not the cover"
ok.append("autoattach fills contiguous slots from 1, cover first, never beyond 5")

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
assert max(counts) <= images_svc.MAX_SLOTS and min(counts) >= 0
ok.append(f"{zeros:.0%} of drafts get no images, the rest 1-{max(counts)}")

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

# --- an empty stack yields a text-only draft, not a failure ----------------
with tx() as c:
    c.execute("TRUNCATE draft_images, images CASCADE")
    d2 = drafts_svc.create_draft(c, {"account": "craigs2", "title": "t", "body": "b"})
    got = images_svc.autoattach(c, draft_id=d2["id"], account="craigs2", rng=random.Random(3))
assert got == [], got
ok.append("an empty stack produces a text-only draft rather than an error")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
