"""Keeping our own copies of the pictures a posting already published.

`posts.images` is a manifest of Craigslist's own CDN URLs — written by hydration
for live postings, and by `cl scan-ended` for ones that have already finished.
Those URLs resolve right up until Craigslist prunes the ad, which is exactly the
moment somebody wants to look at what was on it. Archiving fetches the bytes
onto the VPS so the record outlives the posting.

Two things have to hold, and the second is the dangerous one:

  - the manifest gets an `image_id` written back, so the UI stops rendering
    Craigslist's URL and starts rendering ours, and a second run is a no-op;
  - an archived image can never be handed to a new draft. These bytes have
    already appeared on Craigslist under one account. Decision 13 exists because
    Craigslist notices the same photo under different sellers, and an archive
    that quietly grew the usable pool would be a way to violate it by accident.

The network is stubbed. This test must not depend on Craigslist being up, and
it must not fetch anything.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx

from app.db import conn, init_pool, tx
from app.services import images as images_svc

init_pool()
ok = []
failures = []


def check(label, condition, detail=""):
    (ok if condition else failures).append(
        label if condition else (f"{label}  [{detail}]" if detail else label)
    )


PID = "7899999123"
ACCOUNT = "craigs1"
# Distinct bytes per slot, so content-addressing produces distinct rows and a
# collision would show up as a missing image rather than passing silently.
BODIES = {
    "https://images.craigslist.org/aaa_600x450.jpg": b"\xff\xd8\xff-one" + b"a" * 64,
    "//images.craigslist.org/bbb_600x450.jpg": b"\xff\xd8\xff-two" + b"b" * 64,
    "https://images.craigslist.org/gone_600x450.jpg": None,  # 404
}
MANIFEST = [
    {"slot": 1, "url": "https://images.craigslist.org/aaa_600x450.jpg", "sha256": None},
    {"slot": 2, "url": "//images.craigslist.org/bbb_600x450.jpg", "sha256": None},
    {"slot": 3, "url": "https://images.craigslist.org/gone_600x450.jpg", "sha256": None},
]


def _handler(request: httpx.Request) -> httpx.Response:
    # The protocol-relative URL must have been rewritten before it got here.
    body = BODIES.get(str(request.url)) or BODIES.get(
        str(request.url).replace("https:", "", 1)
    )
    if body is None:
        return httpx.Response(404)
    return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})


class _StubClient(httpx.Client):
    def __init__(self, *a, **kw):
        kw.pop("timeout", None)
        kw.pop("follow_redirects", None)
        super().__init__(transport=httpx.MockTransport(_handler))


def _cleanup():
    with tx() as c:
        c.execute("DELETE FROM posts WHERE post_id = %s", (PID,))
        c.execute("DELETE FROM images WHERE source = %s", (images_svc.RECOVERED_SOURCE,))


_cleanup()
with tx() as c:
    c.execute(
        """
        INSERT INTO posts (post_id, account, title, url, posted_ts, images)
        VALUES (%s, %s, 'Recovered ad', 'https://x/1.html', NOW(), %s::jsonb)
        """,
        (PID, ACCOUNT, json.dumps(MANIFEST)),
    )

with conn() as c:
    check("a manifest full of Craigslist URLs is reported as needing archive",
          PID in images_svc.posts_needing_archive(c, limit=200))

_real = httpx.Client
httpx.Client = _StubClient
try:
    with tx() as c:
        result = images_svc.archive_post_images(c, PID)
finally:
    httpx.Client = _real

check("both reachable images were stored", result["stored"] == 2, str(result))
check("the pruned one was counted as failed, not raised",
      result["failed"] == 1, str(result))

with conn() as c:
    row = c.execute("SELECT images FROM posts WHERE post_id = %s", (PID,)).fetchone()
stored = {e["slot"]: e for e in row["images"]}

check("slot 1 now points at our copy", bool(stored[1].get("image_id")), str(stored[1]))
check("the protocol-relative URL was fetched too, not skipped",
      bool(stored[2].get("image_id")), str(stored[2]))
check("a fetch that failed leaves the entry alone rather than half-written",
      stored[3].get("image_id") is None, str(stored[3]))
check("each entry carries the digest of what we stored",
      stored[1]["sha256"] and stored[1]["sha256"] != stored[2]["sha256"])
check("the original URL is kept, so we can tell where it came from",
      stored[1]["url"].endswith("aaa_600x450.jpg"))

with conn() as c:
    img = c.execute(
        "SELECT status, source, owner_account, used_at FROM images WHERE id = %s",
        (stored[1]["image_id"],),
    ).fetchone()
check("archived rows are marked archived", img["status"] == images_svc.ARCHIVED_STATUS,
      str(img["status"]))
check("archived rows are marked recovered", img["source"] == images_svc.RECOVERED_SOURCE,
      str(img["source"]))
check("the account that published it owns it permanently",
      img["owner_account"] == ACCOUNT, str(img["owner_account"]))
check("used_at is set, because it has already been on Craigslist",
      img["used_at"] is not None)

# ------------------------------------------------------- the invariant that matters
# `pick_for_draft` is what hands images to new drafts. If an archived image ever
# came back out of it, the same photo would go up under a second account.
with conn() as c:
    picked = images_svc.pick_for_draft(c, account=ACCOUNT, count=50)
picked_ids = {p["id"] for p in picked}
check("an archived image is never offered to a new draft",
      stored[1]["image_id"] not in picked_ids and stored[2]["image_id"] not in picked_ids,
      f"picked {sorted(picked_ids)}")

# ------------------------------------------------------------------ idempotence
with conn() as c:
    check("a post whose manifest is fully archived still lists, because one URL failed",
          PID in images_svc.posts_needing_archive(c, limit=200))

httpx.Client = _StubClient
try:
    with tx() as c:
        again = images_svc.archive_post_images(c, PID)
finally:
    httpx.Client = _real
check("a second run re-fetches nothing it already holds",
      again["already"] == 2 and again["stored"] == 0, str(again))

# ------------------------------------------------------------------- empty cases
with tx() as c:
    c.execute("UPDATE posts SET images = '[]'::jsonb WHERE post_id = %s", (PID,))
    empty = images_svc.archive_post_images(c, PID)
check("an empty manifest is a no-op, not an error", empty["stored"] == 0, str(empty))
with conn() as c:
    check("a post with no images is not listed as needing archive",
          PID not in images_svc.posts_needing_archive(c, limit=200))

try:
    with tx() as c:
        images_svc.archive_post_images(c, "no-such-post")
    check("an unknown post raises", False)
except ValueError:
    check("an unknown post raises ValueError, which the router maps to 404", True)

_cleanup()

if failures:
    print("\n".join(f"  --  {f}" for f in failures))
    print(f"\n{len(failures)} FAILED, {len(ok)} passed")
    raise SystemExit(1)
print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
print("image archive OK")
