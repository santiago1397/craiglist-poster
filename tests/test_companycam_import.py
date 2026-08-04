"""The CompanyCam importer: normalisation and idempotency.

No network. The photo objects below are the shape the real API returns; nothing
here opens a socket — the point is what the importer does with bytes, not
whether CompanyCam is up.

Three properties are worth more than the rest:

**`normalise` applies EXIF orientation before stripping it.** Phone cameras
write the sensor buffer and set `Orientation`; every viewer un-rotates on the
way out. Strip without transposing and roof photos publish sideways to
Craigslist — permanently, and with nothing in the database to show for it. The
fixture is a deliberately non-square image tagged `Orientation=6`, because a
square one cannot catch this.

**Everything comes out JPEG.** iPhone originals are HEIC, and
`storage.relative_path` maps only jpeg/png/webp before falling through to
`.bin` — which the desktop would later hand to Craigslist's file input at post
time, so the failure would surface on a live account hours after the import.

**Idempotency keys on CompanyCam's photo id, not our sha256.** Two different
remote photos routinely normalise to identical bytes; the second finds no row to
stamp, so keying on sha256 would re-download it on every run, forever.

    PYTHONPATH=backend uv run python tests/test_companycam_import.py
"""
import io
import os
import tempfile

os.environ.setdefault("IMAGES_DIR", tempfile.mkdtemp())

from PIL import Image  # noqa: E402

from app.db import conn, init_pool, tx  # noqa: E402
from app.importers import curate  # noqa: E402
from app.services import companycam  # noqa: E402
from app.services import image_import  # noqa: E402

init_pool()
ok = []

SOURCE = "companycam"


def jpeg(size=(40, 20), colour="red", orientation=None) -> bytes:
    im = Image.new("RGB", size, colour)
    buf = io.BytesIO()
    if orientation is not None:
        exif = im.getexif()
        exif[0x0112] = orientation  # Orientation
        exif[0x010F] = "TestCam"    # Make — something to notice surviving
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


with tx() as c:
    c.execute("DELETE FROM image_sources WHERE source = %s", (SOURCE,))
    c.execute("DELETE FROM images WHERE source = %s", (SOURCE,))

# --- orientation is applied, not discarded ---------------------------------
# Orientation 6 means "rotate 90° clockwise on display", so a 40x20 source must
# come out 20x40. Getting this wrong is invisible in every other assertion.
raw = jpeg(size=(40, 20), orientation=6)
assert Image.open(io.BytesIO(raw)).size == (40, 20), "fixture is not what we think"
out = image_import.normalise(raw)
assert Image.open(io.BytesIO(out)).size == (20, 40), \
    "EXIF orientation was stripped without being applied — photos will publish sideways"
ok.append("EXIF orientation is applied before the metadata is stripped")

# --- and the metadata itself is gone ---------------------------------------
# These are photographs of customers' houses; CompanyCam's own photo objects
# carry lat/lon, and the files carry it too.
after = Image.open(io.BytesIO(out)).getexif()
assert not dict(after), f"EXIF survived normalisation: {dict(after)}"
ok.append("EXIF (including any GPS block) does not survive normalisation")

# --- every format becomes JPEG ---------------------------------------------
png = io.BytesIO()
Image.new("RGBA", (30, 30), (0, 128, 0, 128)).save(png, "PNG")
converted = image_import.normalise(png.getvalue())
assert Image.open(io.BytesIO(converted)).format == "JPEG", "a PNG stayed a PNG"
ok.append("a non-JPEG input is re-encoded to JPEG, so storage never writes .bin")

# --- oversized photos are scaled to the configured edge --------------------
big = image_import.normalise(jpeg(size=(4032, 3024)), max_edge=1600)
w, h = Image.open(io.BytesIO(big)).size
assert max(w, h) == 1600, f"expected a 1600px long edge, got {w}x{h}"
assert len(big) < 900_000, f"a normalised photo should be well under 1MB, got {len(big)}"
ok.append("a phone-sized original is scaled to the configured long edge")

# --- undecodable bytes are one bad photo, not a dead run -------------------
try:
    image_import.normalise(b"this is not an image at all")
    raise AssertionError("garbage bytes were accepted as an image")
except image_import.ImportError_:
    pass
ok.append("undecodable bytes raise ImportError_ rather than crashing the run")

# --- store_external returns the id on conflict, unlike _store --------------
data = image_import.normalise(jpeg(colour="blue"))
with tx() as c:
    first_id, was_new = image_import.store_external(c, data, source=SOURCE)
assert was_new is True
with tx() as c:
    second_id, was_new2 = image_import.store_external(c, data, source=SOURCE)
assert was_new2 is False, "identical bytes were stored twice"
assert second_id == first_id, \
    "a conflict returned no id; the importer could not record the duplicate"
ok.append("store_external reports the existing id on conflict, so duplicates are recordable")

# --- imports land on the pending shelf -------------------------------------
with conn() as c:
    row = c.execute("SELECT status, kind, mime, source FROM images WHERE id=%s",
                    (first_id,)).fetchone()
assert row["status"] == "pending", \
    "an imported photo was usable without anybody reviewing it"
assert row["kind"] == "photo" and row["mime"] == "image/jpeg"
assert row["source"] == SOURCE
ok.append("imported photos land pending, as photos, as JPEG")

# --- the ledger keys on the remote id, so N remote photos map to 1 image ----
with tx() as c:
    image_import.record(c, source=SOURCE, external_id="cc-1", state="imported",
                        image_id=first_id)
    image_import.record(c, source=SOURCE, external_id="cc-2", state="duplicate",
                        image_id=first_id)
with conn() as c:
    e1 = image_import.ledger_entry(c, source=SOURCE, external_id="cc-1")
    e2 = image_import.ledger_entry(c, source=SOURCE, external_id="cc-2")
assert e1["image_id"] == e2["image_id"] == first_id
assert image_import.should_import(e1) is False
assert image_import.should_import(e2) is False, \
    "a duplicate would be re-downloaded on every future run"
ok.append("two remote ids sharing one image are both recorded and both skipped")

# --- a deliberate deletion is not undone by the next run -------------------
# ON DELETE SET NULL keeps the ledger row, so an operator throwing a photo away
# stays thrown away.
with tx() as c:
    c.execute("DELETE FROM images WHERE id = %s", (first_id,))
with conn() as c:
    orphan = image_import.ledger_entry(c, source=SOURCE, external_id="cc-1")
assert orphan is not None and orphan["image_id"] is None
assert image_import.should_import(orphan) is False, \
    "a deleted photo would be re-imported, undoing the operator's decision"
ok.append("deleting an image leaves the ledger row, so it is never re-imported")

# --- failures retry, but not forever ---------------------------------------
with tx() as c:
    for _ in range(image_import.MAX_ATTEMPTS):
        image_import.record(c, source=SOURCE, external_id="cc-bad",
                            state="failed", error="boom")
with conn() as c:
    bad = image_import.ledger_entry(c, source=SOURCE, external_id="cc-bad")
assert bad["attempts"] >= image_import.MAX_ATTEMPTS
assert image_import.should_import(bad) is False, "a broken photo retries forever"
assert image_import.should_import(bad, retry_failed=True) is True, \
    "--retry-failed cannot reach a photo past the attempt cap"
ok.append("failed photos retry up to a cap, and --retry-failed overrides it")

# --- variant selection falls back rather than skipping ---------------------
photo = {"uris": [{"type": "web", "url": "https://x/w.jpg"},
                  {"type": "thumbnail", "url": "https://x/t.jpg"}]}
assert companycam.pick_uri(photo, "original") == ("web", "https://x/w.jpg"), \
    "a photo with no original was skipped instead of falling back to web"
assert companycam.pick_uri({"uris": []}) is None
assert companycam.pick_uri({}) is None
ok.append("a missing 'original' falls back to 'web'; no uris at all is skipped")

# --- thumbnail is never silently chosen ------------------------------------
# It is a few hundred pixels wide. Publishing one as an ad photo would be worse
# than publishing nothing, because nothing is visible as a problem.
thumb_only = {"uris": [{"type": "thumbnail", "url": "https://x/t.jpg"}]}
assert companycam.pick_uri(thumb_only) is None, \
    "a thumbnail was selected as an ad photo"
ok.append("a thumbnail-only photo is skipped rather than published tiny")

# --- curate counts images, not ledger rows ---------------------------------
# The ledger is N→1 by design, so a plain LEFT JOIN returns one row per remote
# photo rather than per image. That would eat the --limit and double-count the
# byte total, which is exactly the wrong way for a bulk tool to be wrong.
second = image_import.normalise(jpeg(colour="green"))
with tx() as c:
    other_id, _ = image_import.store_external(c, second, source=SOURCE)
    image_import.record(c, source=SOURCE, external_id="cc-3", state="imported",
                        image_id=other_id)
    # first_id was deleted above; re-create a two-ledger-row image.
    third_id, _ = image_import.store_external(
        c, image_import.normalise(jpeg(colour="orange")), source=SOURCE
    )
    image_import.record(c, source=SOURCE, external_id="cc-4", state="imported",
                        image_id=third_id)
    image_import.record(c, source=SOURCE, external_id="cc-5", state="duplicate",
                        image_id=third_id)

args = curate.build_parser().parse_args(
    ["list", "--source", SOURCE, "--status", "pending", "--limit", "50"]
)
with conn() as c:
    rows = curate._select(c, args)
ids = [r["id"] for r in rows]
assert len(ids) == len(set(ids)), \
    f"curate returned an image more than once: {ids}"
assert set(ids) == {other_id, third_id}, f"expected both images, got {ids}"
ok.append("curate lists one row per image even when several remote ids map to it")

with tx() as c:
    c.execute("DELETE FROM image_sources WHERE source = %s", (SOURCE,))
    c.execute("DELETE FROM images WHERE source = %s", (SOURCE,))

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
