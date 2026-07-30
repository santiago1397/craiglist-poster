"""The prompt library, and the isolation of test renders.

The one that really matters: a test render must be invisible everywhere that
feeds a real Craigslist post. Tuning a prompt should never put an unreviewed
experiment on a live ad.
"""
import os
import tempfile

os.environ.setdefault("IMAGES_DIR", tempfile.mkdtemp())

import io  # noqa: E402
import random  # noqa: E402

from PIL import Image  # noqa: E402

from app.db import conn, init_pool, tx  # noqa: E402
from app.services import drafts as drafts_svc  # noqa: E402
from app.services import generator  # noqa: E402
from app.services import images as images_svc  # noqa: E402
from app.services import prompts as prompts_svc  # noqa: E402

init_pool()
ok = []


def jpeg(c=(80, 120, 160)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (64, 48), c).save(b, format="JPEG")
    return b.getvalue()


with tx() as c:
    c.execute("TRUNCATE draft_images, images, drafts CASCADE")

# --- migration seeded a default per image purpose --------------------------
with conn() as c:
    cover = prompts_svc.get_default_body(c, "cover_image")
    photo = prompts_svc.get_default_body(c, "photo_image")
assert cover and photo, (cover, photo)
assert cover != photo, "cover and photo defaults are identical; the split bought nothing"
assert "lower third" in cover, "the cover default must reserve space for the overlay text"
ok.append("cover and photo have distinct defaults; the cover reserves space for text")

# --- exactly one default per purpose, always -------------------------------
with tx() as c:
    a = prompts_svc.create_prompt(c, purpose="photo_image", name="A", body="prompt a {kind}")
    b = prompts_svc.create_prompt(c, purpose="photo_image", name="B", body="prompt b {kind}",
                                  make_default=True)
assert not a["is_default"] and b["is_default"]
with conn() as c:
    defaults = [p for p in prompts_svc.list_prompts(c, "photo_image") if p["is_default"]]
assert len(defaults) == 1 and defaults[0]["id"] == b["id"], defaults
ok.append("creating a prompt as default demotes the previous one; exactly one survives")

with tx() as c:
    prompts_svc.set_default(c, a["id"])
with conn() as c:
    defaults = [p for p in prompts_svc.list_prompts(c, "photo_image") if p["is_default"]]
assert len(defaults) == 1 and defaults[0]["id"] == a["id"]
ok.append("switching the default leaves exactly one")

# --- deleting the default promotes another rather than leaving none --------
with tx() as c:
    prompts_svc.delete_prompt(c, a["id"])
with conn() as c:
    defaults = [p for p in prompts_svc.list_prompts(c, "photo_image") if p["is_default"]]
assert len(defaults) == 1, f"deleting the default left {len(defaults)} defaults"
ok.append("deleting the default promotes another prompt")

# --- the first prompt for a purpose becomes default automatically ----------
with tx() as c:
    c.execute("DELETE FROM prompts WHERE purpose = 'ad_copy'")
    first = prompts_svc.create_prompt(c, purpose="ad_copy", name="only", body="x")
assert first["is_default"], "a purpose with prompts but no default would fall back silently"
ok.append("the first prompt for a purpose is made default automatically")

# --- generation reads the library default ----------------------------------
with tx() as c:
    prompts_svc.create_prompt(c, purpose="photo_image", name="Studio",
                              body="A {kind} in {city}, studio test", make_default=True)
    res = images_svc.generate_images(c, count=1, city="Davie")
# No API key in tests, so nothing is produced — but the prompt resolution path
# is what is under test, and it must not raise.
assert res["created"] == 0 and res["error"], res
with conn() as c:
    assert images_svc._default_prompt(c, "photo") == "A {kind} in {city}, studio test"
    assert "lower third" in images_svc._default_prompt(c, "cover")
ok.append("generation resolves the library default, separately per kind")

# --- a stray brace must not kill the batch ---------------------------------
with tx() as c:
    prompts_svc.create_prompt(c, purpose="photo_image", name="Braces",
                              body="A roof {kind} with {weird} placeholder", make_default=True)
    res = images_svc.generate_images(c, count=1)
assert "MINIMAX" in (res["error"] or ""), res  # failed on the key, not on formatting
ok.append("an unrecognised placeholder does not crash generation")

# --- the editable kinds list -----------------------------------------------
with tx() as c:
    kinds = prompts_svc.set_image_kinds(c, ["metal roof", " tile roof ", "", "flat roof"])
assert kinds == ["metal roof", "tile roof", "flat roof"], kinds
with tx() as c:
    try:
        prompts_svc.set_image_kinds(c, ["  ", ""])
        raise AssertionError("accepted an empty kinds list")
    except ValueError:
        pass
ok.append("kinds are trimmed and de-blanked; an empty list is refused")

# --- test renders are invisible to everything that feeds a real post -------
with tx() as c:
    test_img = images_svc._store(c, jpeg(), source="generated", kind="photo", status="test")
    approved = images_svc._store(c, jpeg((10, 200, 10)), source="generated", kind="photo",
                                 status="approved")
    d = drafts_svc.create_draft(c, {"account": "craigs1", "title": "t", "body": "b"})

with conn() as c:
    pool = [i["id"] for i in images_svc.pick_for_draft(c, account="craigs1", count=20)]
assert test_img["id"] not in pool, "a test render was offered to a real draft"
assert approved["id"] in pool
ok.append("the draft picker never offers a test render")

with tx() as c:
    got = images_svc.autoattach(c, draft_id=d["id"], account="craigs1", rng=random.Random(0))
assert all(i["id"] != test_img["id"] for i in got), "autoattach used a test render"
ok.append("auto-attach never uses a test render")

with tx() as c:
    try:
        images_svc.attach(c, draft_id=d["id"], image_id=test_img["id"], slot=2)
        raise AssertionError("attached a test render to a draft")
    except ValueError as e:
        assert "not approved" in str(e), e
ok.append("a test render cannot be attached by hand either")

with conn() as c:
    listed = [i["id"] for i in images_svc.list_images(c, status="pending")["images"]]
    listed += [i["id"] for i in images_svc.list_images(c, status="approved")["images"]]
assert test_img["id"] not in listed, "a test render showed up on the Images page"
ok.append("test renders do not appear on the Images page")

# --- keep promotes; discard removes ----------------------------------------
with tx() as c:
    images_svc.set_status(c, test_img["id"], "pending")
with conn() as c:
    assert test_img["id"] in [i["id"] for i in images_svc.list_images(c, status="pending")["images"]]
ok.append("keeping a render promotes it onto the pending shelf")

with tx() as c:
    leftover = images_svc._store(c, jpeg((1, 2, 3)), source="generated", kind="photo",
                                 status="test")
    n = images_svc.purge_test_renders(c, older_than_hours=0)
assert n >= 1
with conn() as c:
    assert c.execute("SELECT COUNT(*) AS n FROM images WHERE status='test'").fetchone()["n"] == 0
ok.append("discarding sweeps every abandoned test render")

# --- no generated purpose opens with an empty tab --------------------------
# An empty tab reads as "there is no prompt", which is the opposite of true:
# generation would be falling through to a constant the operator cannot see.
# keyword_tail is excluded deliberately — it is literal text imported from the
# workbook, so a fresh install genuinely has none until seeds are loaded.
with conn() as c:
    for purpose in ("cover_image", "photo_image", "ad_copy"):
        assert prompts_svc.get_default_body(c, purpose), \
            f"{purpose} has no default; its studio tab would open empty"
ok.append("every generated purpose has a default, so no studio tab opens empty")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
