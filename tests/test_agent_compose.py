"""The compose surface, against a real Postgres.

The boundary this exercises is not "can an agent write a draft" — it obviously
can, that is the feature. It is the two things that must remain impossible
however the routes are called:

**An agent cannot publish what it wrote.** `reviewed` is the flag that decides
whether copy goes out on a live listing under a real contractor licence. No
compose route exposes it, `create_draft` forces it false for any agent-written
draft, and `post-now` refuses an unreviewed draft. All three are asserted here,
because the first two are defence in depth and depth is worth nothing untested.

**An agent cannot approve an image it did not make.** That is the whole of its
authority over the image stack, and it rests on one column. A NULL
`created_by_key_id` — every image a human made — must answer no.

Images are inserted through `_store` rather than `generate_images` on purpose:
the mechanism under test is the attribution column and what reads it, and going
through the provider would spend real money to prove nothing extra.

Runs against a **scratch** database. It TRUNCATEs — never point it at anything
you care about. See tests/README.md.
"""
import os
import sys

from fastapi.testclient import TestClient

from app.db import conn, init_pool, tx
from app.main import create_app
from app.security import issue_api_key
from app.services import drafts as drafts_svc
from app.services import images as images_svc

# This file TRUNCATEs, and it is run by hand — on a laptop, over SSH, sometimes
# on the box that also hosts production. A forgotten `export POSTGRES_DB` is all
# that separates "scratch run" from "deleted every draft and posting record".
# The README says so, but a warning in prose does not stop a tired operator at
# 2am, and there is no undo. So refuse anything that is not visibly a scratch
# database.
_DB = os.environ.get("POSTGRES_DB", "")
if "scratch" not in _DB and "test" not in _DB:
    sys.exit(
        f"REFUSING to run: POSTGRES_DB={_DB!r}.\n"
        "This test TRUNCATEs. Point it at a scratch database whose name contains "
        "'scratch' or 'test' — never at the live one.\n"
        "  createdb cl_scratch && POSTGRES_DB=cl_scratch ..."
    )

init_pool()

app = create_app()
client = TestClient(app, raise_server_exceptions=False)
ok = []

ACCOUNT = "craigs1"
COUNTY, CITY = "Broward", "Davie"


def reset():
    with tx() as c:
        c.execute(
            "TRUNCATE drafts, draft_images, images, posts, post_attempts, "
            "api_keys, flow_errors CASCADE"
        )


def make_key(label, scope):
    """Issue a key and hand back both halves: the plaintext and its row id."""
    key = issue_api_key(label, scope)
    return key, int(key.split(".")[0])


def add_image(kind="photo", status="pending", key_id=None, tag=b""):
    """One image row with distinct bytes.

    `_store` is content-addressed and skips a digest it already holds, so every
    call needs different bytes or the second one silently returns None.
    """
    with tx() as c:
        return images_svc._store(
            c, b"fake-image-bytes-" + tag, source="generated", kind=kind,
            status=status, created_by_key_id=key_id, cost=0.0035,
        )


DRAFT = {
    "account": ACCOUNT, "title": "Metal roof replacement in Davie",
    "body": "We replace roofs. Call today.", "county": COUNTY, "city": CITY,
}


# --- 1. an agent key composes a draft, and it lands unreviewed --------------
reset()
key, key_id = make_key("test agent", "agent")
headers = {"X-API-Key": key}

r = client.post("/agent/drafts", json=DRAFT, headers=headers)
assert r.status_code == 201, f"create returned {r.status_code}: {r.text[:300]}"
draft = r.json()
draft_id = draft["id"]
assert draft["reviewed"] is False, "an agent-written draft was born reviewed"
assert draft["created_by_key_id"] == key_id, "the draft was not attributed to the key"
assert draft["source"] == "agent:test agent", f"unexpected source {draft['source']!r}"
assert draft["postal_code"] == "33314", "the city's zip was not filled in"
assert draft["license_number"], "the licence was not filled in"
assert "unreviewed" in draft["message"].lower(), \
    "the response does not tell the caller the draft cannot publish"
ok.append("compose OK (a draft is created unreviewed, attributed, with defaults filled)")


# --- 2. reviewed cannot be set, by any route -------------------------------
# The model rejects it, and `create_draft` would force it false even if it did
# not — this asserts the service-level guarantee directly, because that is the
# one a future router cannot route around.
with tx() as c:
    forced = drafts_svc.create_draft(
        c, {**DRAFT, "title": "sneaky", "reviewed": True}, created_by_key_id=key_id
    )
assert forced["reviewed"] is False, \
    "create_draft honoured reviewed=True on an agent-written draft"

r = client.patch(f"/agent/drafts/{draft_id}", json={"reviewed": True}, headers=headers)
assert r.status_code == 422, f"patch with reviewed returned {r.status_code}"
with conn() as c:
    assert drafts_svc.get_draft(c, draft_id)["reviewed"] is False
ok.append("gate OK (reviewed cannot be set through the service or the route)")


# --- 3. an unreviewed draft cannot be published ----------------------------
r = client.post("/agent/post-now", json={"draft_id": draft_id}, headers=headers)
assert r.status_code == 403, f"post-now on an unreviewed draft returned {r.status_code}"
assert "reviewed" in r.json()["detail"].lower()
ok.append("gate OK (post-now refuses an unreviewed draft even with an agent key)")


# --- 4. an agent key is refused in the query string, on reads too ----------
# A read key may travel in a URL; an agent key may not, because it can publish.
# This is the assertion that needs a real row — the scope is only knowable after
# the lookup.
r = client.get(f"/agent/status?key={key}")
assert r.status_code == 400, f"an agent key in ?key= on a read returned {r.status_code}"
assert "header" in r.json()["detail"].lower()

read_key, _ = make_key("test reader", "read")
r = client.get(f"/agent/status?key={read_key}")
assert r.status_code == 200, f"a read key in ?key= returned {r.status_code}"
ok.append("auth OK (an agent key is refused in a URL; a read key still works there)")

# And a read key cannot compose.
r = client.post("/agent/drafts", json=DRAFT, headers={"X-API-Key": read_key})
assert r.status_code == 403, f"a read key composing returned {r.status_code}"
ok.append("auth OK (a read-scope key cannot compose)")


# --- 5. an agent approves only what it generated ---------------------------
mine = add_image(kind="cover", key_id=key_id, tag=b"mine")
theirs = add_image(kind="cover", key_id=None, tag=b"theirs")      # a human's
other_key, other_id = make_key("another agent", "agent")
others = add_image(kind="cover", key_id=other_id, tag=b"others")

r = client.post(f"/agent/images/{theirs['id']}/approve", headers=headers)
assert r.status_code == 403, f"approving a human's image returned {r.status_code}"
r = client.post(f"/agent/images/{others['id']}/approve", headers=headers)
assert r.status_code == 403, f"approving another key's image returned {r.status_code}"

r = client.post(f"/agent/images/{mine['id']}/approve", headers=headers)
assert r.status_code == 200, f"approving its own image returned {r.status_code}"
assert r.json()["status"] == "approved"
ok.append("images OK (an agent approves its own generation and nothing else)")


# --- 6. the cover slot keeps its partition --------------------------------
photo = add_image(kind="photo", status="approved", key_id=key_id, tag=b"photo1")
r = client.post(f"/agent/drafts/{draft_id}/cover",
                json={"image_id": photo["id"]}, headers=headers)
assert r.status_code == 409, f"a photo was accepted into slot 1: {r.status_code}"

unapproved = add_image(kind="cover", key_id=key_id, tag=b"pending")
r = client.post(f"/agent/drafts/{draft_id}/cover",
                json={"image_id": unapproved["id"]}, headers=headers)
assert r.status_code == 409, "an unapproved cover was accepted"

r = client.post(f"/agent/drafts/{draft_id}/cover",
                json={"image_id": mine["id"]}, headers=headers)
assert r.status_code == 200, f"attaching an approved cover returned {r.status_code}"
assert r.json()["slot"] == images_svc.COVER_SLOT
ok.append("images OK (slot 1 takes an approved cover, and refuses a photo or a pending one)")


# --- 7. autofill fills the photo slots and leaves the cover alone ----------
for i in range(5):
    add_image(kind="photo", status="approved", key_id=key_id, tag=f"fill{i}".encode())

r = client.post(f"/agent/drafts/{draft_id}/autofill", json={"count": 23}, headers=headers)
assert r.status_code == 200, f"autofill returned {r.status_code}"
body = r.json()
# The stack is deliberately short here: filling fewer than asked is an ordinary
# state, and the response has to say so rather than read as a failure.
assert body["filled"] < body["requested"]
assert "not an error" in body["message"] or "rather than failing" in body["message"]

with conn() as c:
    attached = images_svc.images_for_draft(c, draft_id)
slots = sorted(i["slot"] for i in attached)
assert slots[0] == images_svc.COVER_SLOT, "slot 1 lost its cover"
cover_row = next(i for i in attached if i["slot"] == images_svc.COVER_SLOT)
assert cover_row["id"] == mine["id"], "autofill overwrote the chosen cover"
assert all(s > 1 for s in slots[1:]), "autofill wrote outside the photo slots"
ok.append("images OK (autofill tops up photo slots and never touches the cover)")


# --- 8. an agent cannot edit a draft it did not write ----------------------
with tx() as c:
    human_draft = drafts_svc.create_draft(c, {**DRAFT, "title": "written by hand"})
r = client.patch(f"/agent/drafts/{human_draft['id']}",
                 json={"title": "hijacked"}, headers=headers)
assert r.status_code == 403, f"an agent edited a human's draft: {r.status_code}"
with conn() as c:
    assert drafts_svc.get_draft(c, human_draft["id"])["title"] == "written by hand"
ok.append("compose OK (an agent cannot edit a draft it did not write)")


# --- 9. an unroutable location is refused at authoring time ----------------
# A county the desktop cannot map does not fail at posting — it silently files
# the ad under the wrong subarea. This is the only place that can be caught.
for bad in ({"county": "Orange", "city": "Orlando"},
            {"county": COUNTY, "city": "Nowhere"},
            {"county": "Monroe", "city": "Key West"}):   # real, but unsupported
    r = client.post("/agent/drafts", json={**DRAFT, **bad}, headers=headers)
    assert r.status_code == 422, f"{bad} was accepted: {r.status_code}"
ok.append("compose OK (an unknown or unroutable county/city is refused)")


# --- 10. spend and output are attributed per key --------------------------
with conn() as c:
    usage = images_svc.key_usage(c)
assert usage[key_id]["images_generated"] >= 8
assert usage[key_id]["cost_usd"] > 0, "generation cost was not recorded against the key"
assert usage[key_id]["drafts_created"] >= 2
assert usage[other_id]["images_generated"] == 1
assert usage[other_id]["drafts_created"] == 0
ok.append("logging OK (images, spend and drafts are attributed to the key that made them)")


# --- 11. locations reports what is already in use -------------------------
r = client.get("/agent/locations", headers=headers)
assert r.status_code == 200
text = r.text
assert "Davie" in text, "the city just used is missing from the report"
assert "in use by" in text, "nothing was reported as in use despite live drafts"
assert "closed" in text, "the report does not say the county list is closed"

r = client.get("/agent/locations?format=json", headers=headers)
payload = r.json()
counties = {c["name"]: c for c in payload["counties"]}
davie = next(c for c in counties[COUNTY]["cities"] if c["city"] == CITY)
assert davie["in_use"] >= 1, "Davie is carrying drafts but reports as free"
assert CITY not in counties[COUNTY]["unused_cities"]
assert payload["license_number"], "no licence number was offered"

# Monroe (the Keys) is not merely flagged, it is absent: Craigslist puts the
# Keys on their own site, so `_select_subarea` cannot route them and the seed
# rows were deleted. Check 9 already proved it is refused at authoring time;
# this pins that it is never *offered* either, which is the stronger property.
assert "Monroe" not in counties, "Monroe is being offered as a placeable county"

# Everything still offered must be routable. `render_locations` carries a
# NOT USABLE branch for a county with no subarea mapping — there is nothing to
# flag today, and this is what fails if one is ever added without routing.
unroutable = [n for n, c in counties.items() if not c["subarea_supported"]]
assert not unroutable, f"offered but unroutable: {unroutable}"
ok.append("locations OK (in-use cities counted; every offered county is routable)")


reset()
print("\n".join(ok))
