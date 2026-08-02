"""The image stack: generate into a pending shelf, approve into the pool,
attach to drafts.

The rules that matter, from the design decisions:

* Generated images land as `pending` and are unusable until approved, so nothing
  reaches Craigslist that nobody looked at (decision 20).
* Attaching an image to a draft permanently binds it to that draft's account
  (decision 13). Detaching releases it only if it never published — once
  Craigslist has seen a photo under one account, it can never appear under
  another.
* Slot 1 is the cover: the thumbnail, and the highest-leverage visual on the ad.

Two stacks, not one
-------------------
`kind` used to be a label nothing enforced: it picked the generation prompt and
then stopped mattering. `pick_for_draft` filtered on status, ownership and
cooldown but never on kind, so a cover — phone number composited across its
lower third — could be drawn into slot 4 of a post as an ordinary photo.

It is now a hard partition. Slot 1 takes covers, slots 2+ take photos, and the
two stacks are displayed and counted separately. `set_kind` is the escape hatch:
a generated shot you want as a cover is one relabel away.

Assignment is a reservation
---------------------------
`draft_images` is keyed `(draft_id, slot)` and has no uniqueness on `image_id`,
and `pick_for_draft` never checked whether an image was already spoken for. With
a queue 15 drafts deep per account, that handed the same photo to several queued
drafts at once and nothing noticed until two of them published the same picture
— which is the exact thing that gets an account ghosted.

An image attached to a *live* draft is therefore excluded from selection. It is
still offerable by hand, so a deliberate reuse stays possible; it just cannot
happen by accident.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone

import psycopg
from loguru import logger

from .. import storage
from ..config import get_settings
from .imagegen import ImageGenError, build_provider

# Matches the desktop's historic photo rule: an image may be reused within its
# owning account, but not for 30 days.
REUSE_COOLDOWN_DAYS = 30

# Craigslist's real per-posting limit: one thumbnail plus 23 more.
MAX_SLOTS = 24

# Slot 1 is the cover; every other slot is a photo.
COVER_SLOT = 1

# Draft states in which an attachment still means something. A draft that
# expired or already posted has no further claim on its images: the posted one's
# images carry `used_at` instead, which is the stronger signal anyway.
LIVE_DRAFT_STATUSES = ("queued", "claimed", "needs_attention")

# Is this image spoken for? Written once and reused, because selection, display
# and the picker must agree exactly — an image the grid calls "available" that
# selection then skips is a bug report.
#
# Two holders, matching `detach`: a live draft, and a live posting's desired
# image set. The second matters even though editing ships disabled — an image
# staged for a live post is either already on Craigslist or about to be, so
# handing it to a queued draft duplicates it just as surely.
_RESERVED_SQL = """
    (EXISTS (
        SELECT 1 FROM draft_images di
        JOIN drafts d ON d.id = di.draft_id
        WHERE di.image_id = images.id AND d.status = ANY(%(live)s)
     )
     OR EXISTS (
        SELECT 1 FROM post_desired_images pdi WHERE pdi.image_id = images.id
     ))
"""

# The five buckets a stack is shown in. Mutually exclusive by construction:
# `attach` requires 'approved', so a pending image can never be assigned or
# published. Derived rather than stored — a bucket column would be a second
# source of truth able to disagree with the attachment table.
_BUCKET_SQL = f"""
    CASE
        WHEN status = 'pending'   THEN 'pending'
        WHEN status = 'rejected'  THEN 'rejected'
        WHEN status <> 'approved' THEN status
        WHEN used_at IS NOT NULL  THEN 'published'
        WHEN {_RESERVED_SQL}      THEN 'assigned'
        ELSE 'available'
    END
"""

BUCKETS = ("pending", "available", "assigned", "published", "rejected")

DEFAULT_IMAGE_PROMPT = (
    "Professional photograph of a well-maintained {kind} on a single-family "
    "home in {city}, South Florida. Bright natural daylight, clear blue sky, "
    "palm trees, realistic residential architecture. Sharp focus, no text, "
    "no watermarks, no people."
)

ROOF_KINDS = [
    "metal roof", "tile roof", "shingle roof", "flat roof",
    "newly replaced roof", "clay tile roof",
]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _default_prompt(conn: psycopg.Connection, kind: str) -> str:
    """The library's default for this kind, or the built-in if none is set.

    Covers and photos are separate on purpose: a cover has text composited over
    its lower third, so it needs that area kept clear.
    """
    from . import prompts as prompts_svc

    purpose = "cover_image" if kind == "cover" else "photo_image"
    return prompts_svc.get_default_body(conn, purpose) or DEFAULT_IMAGE_PROMPT


def _kinds(conn: psycopg.Connection) -> list[str]:
    from . import prompts as prompts_svc

    return prompts_svc.get_image_kinds(conn) or ROOF_KINDS


def generate_images(
    conn: psycopg.Connection,
    *,
    count: int = 4,
    city: str | None = None,
    kind: str = "photo",
    rng: random.Random | None = None,
    prompt_override: str | None = None,
    status: str = "pending",
    created_by_key_id: int | None = None,
) -> dict:
    """Generate into the pending shelf. Never raises for provider problems —
    returns what succeeded plus the error, because a failed batch must not take
    the caller down with it."""
    rng = rng or random.Random()
    g = conn.execute("SELECT * FROM generation_settings LIMIT 1").fetchone()
    settings = get_settings()

    template = (prompt_override or "").strip() or _default_prompt(conn, kind)
    kinds = _kinds(conn)
    unit_cost = float(g["image_cost_usd"] or 0)

    try:
        provider = build_provider(
            "minimax",
            api_key=settings.minimax_api_key,
            api_base=g["image_api_base"],
            model=g["image_model"],
        )
    except ImageGenError as e:
        return {"created": 0, "images": [], "error": str(e), "cost_usd": 0.0}

    created: list[dict] = []
    error: str | None = None
    for _ in range(count):
        # A prompt is free text, so it may contain braces that are not ours.
        # Fall back to the literal text rather than failing the whole batch on
        # a stray brace the operator typed.
        try:
            prompt = template.format(
                city=city or "South Florida", kind=rng.choice(kinds)
            )
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"prompt has an unrecognised placeholder ({e}); using it literally")
            prompt = template
        try:
            blobs = provider.generate(prompt, aspect=g["image_aspect"], n=1)
        except ImageGenError as e:
            error = str(e)
            logger.warning(f"image generation failed: {e}")
            break
        for data in blobs:
            try:
                row = _store(
                    conn, data,
                    source="generated", kind=kind, prompt=prompt,
                    provider=provider.name, model=g["image_model"], cost=unit_cost,
                    status=status, created_by_key_id=created_by_key_id,
                )
            except OSError as e:
                # Disk full, bad volume permissions, read-only mount. Report it
                # rather than 500 — the caller has already paid for the image
                # and deserves to be told why it vanished.
                error = f"could not store image: {e}"
                logger.error(error)
                break
            if row:
                created.append(row)
        if error:
            break

    if created:
        conn.execute(
            "UPDATE generation_settings SET images_generated = images_generated + %s "
            "WHERE singleton",
            (len(created),),
        )
    return {
        "created": len(created),
        "images": created,
        "error": error,
        "cost_usd": round(len(created) * unit_cost, 5),
    }


def _store(
    conn: psycopg.Connection,
    data: bytes,
    *,
    source: str,
    kind: str,
    prompt: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    cost: float | None = None,
    mime: str = "image/jpeg",
    status: str = "pending",
    created_by_key_id: int | None = None,
) -> dict | None:
    """Persist bytes + row. Returns None if we already hold these exact bytes.

    `created_by_key_id` records which agent key generated this, or NULL for the
    dashboard. It is the sole basis on which an agent is later allowed to
    approve an image — see `was_created_by_key`.
    """
    digest, rel, size = storage.put_bytes(data, mime)
    row = conn.execute(
        """
        INSERT INTO images (sha256, storage_path, mime, bytes_size, source,
                            status, kind, prompt, provider, model, cost_usd,
                            created_by_key_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING *
        """,
        (digest, rel, mime, size, source, status, kind, prompt, provider, model,
         cost, created_by_key_id),
    ).fetchone()
    if row is None:
        logger.debug(f"image {digest[:12]} already stored; skipping duplicate")
        return None
    return dict(row)


def purge_test_renders(conn: psycopg.Connection, older_than_hours: int = 2) -> int:
    """Delete test renders nobody kept.

    The studio deletes them when you close it, but a closed tab or a lost
    connection would otherwise leave paid-for junk on the volume forever.
    """
    if older_than_hours <= 0:
        # "Discard everything." Not expressed as an age filter because NOW() is
        # transaction-start time in Postgres, so a row inserted in this same
        # transaction is never strictly older than it and would survive.
        rows = conn.execute("SELECT id FROM images WHERE status = 'test'").fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM images WHERE status = 'test' "
            "AND created_at < NOW() - make_interval(hours => %s)",
            (older_than_hours,),
        ).fetchall()
    for r in rows:
        delete_image(conn, r["id"])
    if rows:
        logger.info(f"purged {len(rows)} abandoned test render(s)")
    return len(rows)


def store_upload(conn: psycopg.Connection, data: bytes, *, mime: str, kind: str = "photo") -> dict | None:
    """Uploaded images skip the shelf — you chose the file, so it is approved."""
    return _store(conn, data, source="uploaded", kind=kind, mime=mime, status="approved")


# Serialises the stack top-up the same way TOPUP_LOCK_KEY serialises drafts, so
# the loop and a manual run cannot both read a stale depth and double-generate.
STACK_TOPUP_LOCK_KEY = 8_412_337_002


def topup_stack(conn: psycopg.Connection, *, force: bool = False) -> dict:
    """Refill the photo stack toward its target. Ships disabled.

    24-image posts consume roughly 69 photos a day and need about a thousand
    standing, which no amount of clicking Generate sustains. This is the job
    that would do it — written, wired, and off by default, because it spends
    money rendering prompts that are still being tuned. Turn it on under
    Settings once the prompts are settled.

    Photos only: covers stay on the pending shelf and stay yours to approve,
    since they are the ones actually looked at. Generated photos land approved
    and immediately usable, which is the trade this whole arrangement rests on.
    """
    if not conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s) AS ok", (STACK_TOPUP_LOCK_KEY,)
    ).fetchone()["ok"]:
        return {"skipped": "already running", "created": 0}

    g = conn.execute("SELECT * FROM generation_settings LIMIT 1").fetchone()
    if not g["image_topup_enabled"] and not force:
        return {"skipped": "image top-up disabled", "created": 0}

    health = stack_health(conn)
    available = health["photos_available"]
    if available >= g["image_stack_floor"] and not force:
        return {"skipped": "above floor", "created": 0, "available": available}

    want = min(
        int(g["image_topup_batch"]),
        max(0, int(g["image_stack_target"]) - available),
    )
    if want <= 0:
        return {"skipped": "at target", "created": 0, "available": available}

    logger.info(f"image stack at {available}, generating {want} photo(s)")
    return generate_images(conn, count=want, kind="photo", status="approved")


# ---------------------------------------------------------------------------
# Shelf / pool
# ---------------------------------------------------------------------------

def list_images(
    conn: psycopg.Connection,
    *,
    status: str | None = None,
    bucket: str | None = None,
    account: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List one stack. `bucket` supersedes `status` where both are given.

    Every row carries its computed `bucket` and, when assigned, the draft
    holding it — a card that says "reserved" without saying by what is a dead
    end when you are trying to free an image up.
    """
    where = ["TRUE"]
    params: dict = {"limit": limit, "offset": offset, "live": list(LIVE_DRAFT_STATUSES)}
    if bucket:
        where.append(f"({_BUCKET_SQL}) = %(bucket)s")
        params["bucket"] = bucket
    elif status:
        where.append("status = %(status)s")
        params["status"] = status
    if kind:
        where.append("kind = %(kind)s")
        params["kind"] = kind
    if account:
        # Available to this account: unclaimed, or already theirs.
        where.append("(owner_account IS NULL OR owner_account = %(account)s)")
        params["account"] = account
    clause = " AND ".join(where)
    rows = conn.execute(
        f"""
        SELECT images.*, ({_BUCKET_SQL}) AS bucket,
               (SELECT di.draft_id FROM draft_images di
                  JOIN drafts d ON d.id = di.draft_id
                 WHERE di.image_id = images.id AND d.status = ANY(%(live)s)
                 ORDER BY di.draft_id LIMIT 1) AS assigned_draft_id
        FROM images WHERE {clause}
        ORDER BY created_at DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM images WHERE {clause}", params
    ).fetchone()["n"]
    return {"images": [dict(r) for r in rows], "total": total}


def bucket_counts(conn: psycopg.Connection, *, account: str | None = None) -> dict:
    """Per-kind counts for every bucket, for the stack tabs.

    Returns {"cover": {bucket: n}, "photo": {...}} with every bucket present at
    zero rather than absent, so the tabs do not flicker in and out of existence.
    """
    where = ["TRUE"]
    params: dict = {"live": list(LIVE_DRAFT_STATUSES)}
    if account:
        where.append("(owner_account IS NULL OR owner_account = %(account)s)")
        params["account"] = account
    rows = conn.execute(
        f"""
        SELECT kind, ({_BUCKET_SQL}) AS bucket, COUNT(*) AS n
        FROM images WHERE {" AND ".join(where)}
        GROUP BY 1, 2
        """,
        params,
    ).fetchall()
    out: dict[str, dict[str, int]] = {
        k: {b: 0 for b in BUCKETS} for k in ("cover", "photo")
    }
    for r in rows:
        out.setdefault(r["kind"], {b: 0 for b in BUCKETS})[r["bucket"]] = r["n"]
    return out


def was_created_by_key(conn: psycopg.Connection, image_id: int, key_id: int) -> bool:
    """Did this exact API key generate this exact image?

    The whole of the agent's authority over the image stack rests on this
    question. An agent may approve what it made and nothing else — so a NULL
    `created_by_key_id` (every image a human made in the dashboard) answers
    False, and so does an image generated by a different key.

    Note what makes this safe upstream: `_store` inserts `ON CONFLICT (sha256)
    DO NOTHING`, so generating bytes that already exist in the stack returns no
    row rather than re-stamping someone else's image with the caller's key id.
    An agent therefore cannot acquire approval rights over an existing image by
    regenerating it.
    """
    row = conn.execute(
        "SELECT 1 FROM images WHERE id = %s AND created_by_key_id = %s",
        (image_id, key_id),
    ).fetchone()
    return row is not None


def key_usage(conn: psycopg.Connection) -> dict[int, dict]:
    """Per-key totals of what each agent key has generated and written.

    Generation is deliberately uncapped, so this is the whole of the control:
    the spend has to be visible somewhere or "no cap, but logged" means nothing.
    Keyed by api_key id for the Settings table to join against.
    """
    out: dict[int, dict] = {}
    for r in conn.execute(
        """
        SELECT created_by_key_id AS key_id,
               COUNT(*)                       AS images_generated,
               COALESCE(SUM(cost_usd), 0)     AS cost_usd
        FROM images
        WHERE created_by_key_id IS NOT NULL
        GROUP BY created_by_key_id
        """
    ).fetchall():
        out[r["key_id"]] = {
            "images_generated": r["images_generated"],
            "cost_usd": float(r["cost_usd"] or 0),
            "drafts_created": 0,
        }
    for r in conn.execute(
        """
        SELECT created_by_key_id AS key_id, COUNT(*) AS drafts_created
        FROM drafts
        WHERE created_by_key_id IS NOT NULL
        GROUP BY created_by_key_id
        """
    ).fetchall():
        entry = out.setdefault(
            r["key_id"],
            {"images_generated": 0, "cost_usd": 0.0, "drafts_created": 0},
        )
        entry["drafts_created"] = r["drafts_created"]
    return out


def set_status(conn: psycopg.Connection, image_id: int, status: str) -> dict | None:
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status: {status}")
    row = conn.execute(
        "UPDATE images SET status = %s, updated_at = NOW() WHERE id = %s RETURNING *",
        (status, image_id),
    ).fetchone()
    return dict(row) if row else None


def set_kind(conn: psycopg.Connection, image_id: int, kind: str) -> dict | None:
    """Move an image between the two stacks.

    This is what makes the hard partition liveable: a generated shot that would
    make a good thumbnail is one click from being a cover, rather than needing
    to be regenerated under the cover prompt.

    Refused while the image is attached to a live draft, because the slot it
    occupies is validated against its kind — relabelling underneath an existing
    attachment would leave a photo sitting in slot 1 with nothing to catch it.
    """
    if kind not in ("photo", "cover"):
        raise ValueError(f"invalid kind: {kind}")
    held = conn.execute(
        """
        SELECT di.draft_id FROM draft_images di
        JOIN drafts d ON d.id = di.draft_id
        WHERE di.image_id = %s AND d.status = ANY(%s)
        LIMIT 1
        """,
        (image_id, list(LIVE_DRAFT_STATUSES)),
    ).fetchone()
    if held:
        raise ValueError(
            f"image is attached to draft {held['draft_id']}; detach it before "
            f"changing its kind"
        )
    row = conn.execute(
        "UPDATE images SET kind = %s, updated_at = NOW() WHERE id = %s RETURNING *",
        (kind, image_id),
    ).fetchone()
    return dict(row) if row else None


def kind_for_slot(slot: int) -> str:
    return "cover" if slot == COVER_SLOT else "photo"


def reserved_by(conn: psycopg.Connection, image_id: int) -> int | None:
    """The live draft holding this image, if any."""
    row = conn.execute(
        """
        SELECT di.draft_id FROM draft_images di
        JOIN drafts d ON d.id = di.draft_id
        WHERE di.image_id = %s AND d.status = ANY(%s)
        ORDER BY di.draft_id LIMIT 1
        """,
        (image_id, list(LIVE_DRAFT_STATUSES)),
    ).fetchone()
    return row["draft_id"] if row else None


def reserved_by_post(
    conn: psycopg.Connection, image_id: int, *, exclude_post_id: str | None = None
) -> str | None:
    """The live posting whose desired image set holds this image, if any.

    `exclude_post_id` is the caller's own post. Without it, moving an image from
    slot 3 to slot 4 of the same posting reads as a double-booking against
    itself. Excluding in SQL rather than comparing the result keeps the answer
    deterministic when an image is somehow held by more than one post.
    """
    row = conn.execute(
        """
        SELECT post_id FROM post_desired_images
        WHERE image_id = %s AND (%s::text IS NULL OR post_id <> %s)
        ORDER BY post_id LIMIT 1
        """,
        (image_id, exclude_post_id, exclude_post_id),
    ).fetchone()
    return row["post_id"] if row else None


def delete_image(conn: psycopg.Connection, image_id: int) -> bool:
    row = conn.execute(
        "SELECT storage_path, used_at FROM images WHERE id = %s", (image_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM images WHERE id = %s", (image_id,))
    # Keep the bytes of anything already published, so an audit of a live ad
    # can still show what went out.
    if row["used_at"] is None:
        storage.delete(row["storage_path"])
    return True


def stats(conn: psycopg.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, kind, COUNT(*) AS n FROM images GROUP BY 1,2"
    ).fetchall()
    spend = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS total FROM images"
    ).fetchone()["total"]
    by_account = conn.execute(
        """
        SELECT COALESCE(owner_account,'(unclaimed)') AS account, COUNT(*) AS n
        FROM images WHERE status = 'approved' GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    return {
        "by_status": [dict(r) for r in rows],
        "spend_usd": float(spend or 0),
        "available": {r["account"]: r["n"] for r in by_account},
        "buckets": bucket_counts(conn),
        "stack_health": stack_health(conn),
    }


def stack_health(conn: psycopg.Connection) -> dict:
    """How far the photo stack is from filling the queue it has to feed.

    With refill on manual, being short is the normal state rather than an
    error — so this exists to be shown continuously, not to raise an alarm once.
    `short` is the number of slots that will simply go unfilled if every queued
    draft were autofilled right now.
    """
    g = conn.execute(
        "SELECT photos_max FROM generation_settings LIMIT 1"
    ).fetchone()
    per_draft = int((g or {}).get("photos_max") or 0)

    queued = conn.execute(
        "SELECT COUNT(*) AS n FROM drafts WHERE status = 'queued'"
    ).fetchone()["n"]

    # Slots already filled on queued drafts do not need feeding again.
    filled = conn.execute(
        """
        SELECT COUNT(*) AS n FROM draft_images di
        JOIN drafts d ON d.id = di.draft_id
        WHERE d.status = 'queued' AND di.slot > %s
        """,
        (COVER_SLOT,),
    ).fetchone()["n"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=REUSE_COOLDOWN_DAYS)
    available = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM images
        WHERE status = 'approved' AND kind = 'photo'
          AND (used_at IS NULL OR used_at < %(cutoff)s)
          AND NOT {_RESERVED_SQL}
        """,
        {"cutoff": cutoff, "live": list(LIVE_DRAFT_STATUSES)},
    ).fetchone()["n"]

    demand = max(0, queued * per_draft - filled)
    covers = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM images
        WHERE status = 'approved' AND kind = 'cover' AND used_at IS NULL
          AND NOT {_RESERVED_SQL}
        """,
        {"live": list(LIVE_DRAFT_STATUSES)},
    ).fetchone()["n"]

    return {
        "queued_drafts": queued,
        "photos_per_draft": per_draft,
        "photo_demand": demand,
        "photos_available": available,
        "photos_short": max(0, demand - available),
        "covers_available": covers,
    }


# ---------------------------------------------------------------------------
# Attachment — this is where an image becomes an account's forever
# ---------------------------------------------------------------------------

def _check_attachable(
    conn: psycopg.Connection,
    *,
    image_id: int,
    account: str,
    slot: int,
    allow_double_book: bool,
    self_draft_id: int | None = None,
    self_post_id: str | None = None,
) -> dict:
    """Every rule that governs binding an image to a slot. Returns the image row.

    Shared by `attach` (queued drafts) and `attach_to_post` (live postings)
    because the rules are the same rules — the only thing that differs is which
    table the row lands in. The live-post path used to reimplement a thinner
    version of this and quietly lost the kind partition and the reservation
    check with it, so a photo could take slot 1 of a live listing and the same
    image could be staged on two ads at once.

    `self_draft_id` / `self_post_id` name the target doing the asking, so
    re-attaching an image to a slot it already occupies is not read as a
    double-booking against itself.
    """
    if not (1 <= slot <= MAX_SLOTS):
        raise ValueError(f"slot must be 1-{MAX_SLOTS}")
    img = conn.execute("SELECT * FROM images WHERE id = %s", (image_id,)).fetchone()
    if img is None:
        raise ValueError("image not found")
    if img["status"] != "approved":
        raise ValueError("image is not approved yet")
    if img["owner_account"] and img["owner_account"] != account:
        raise ValueError(
            f"image belongs to {img['owner_account']} and cannot be reused by "
            f"{account}"
        )
    # The partition. Slot 1 is the Craigslist thumbnail and the only place a
    # cover belongs; a cover anywhere else publishes a phone number in the
    # middle of the gallery, and a photo in slot 1 throws away the one visual
    # that was designed to be seen.
    want = kind_for_slot(slot)
    if img["kind"] != want:
        raise ValueError(
            f"slot {slot} takes a {want}, but image {image_id} is a {img['kind']} — "
            f"change its kind on the Images page first"
        )
    # Reservation. Bypassable on purpose: reusing one strong photo across two
    # drafts is a legitimate thing to want, it just must never happen by
    # accident (which is what the unguarded version did, every top-up run).
    if not allow_double_book:
        holder = reserved_by(conn, image_id)
        if holder is not None and holder != self_draft_id:
            raise ValueError(
                f"image is already attached to draft {holder}; detach it there "
                f"first, or confirm the reuse"
            )
        post_holder = reserved_by_post(conn, image_id, exclude_post_id=self_post_id)
        if post_holder is not None:
            raise ValueError(
                f"image is staged on live posting {post_holder}; using it here "
                f"would put the same picture on two listings"
            )
    return dict(img)


def attach(
    conn: psycopg.Connection,
    *,
    draft_id: int,
    image_id: int,
    slot: int,
    allow_double_book: bool = False,
) -> dict:
    draft = conn.execute(
        "SELECT id, account, status FROM drafts WHERE id = %s", (draft_id,)
    ).fetchone()
    if draft is None:
        raise ValueError("draft not found")
    img = _check_attachable(
        conn,
        image_id=image_id,
        account=draft["account"],
        slot=slot,
        allow_double_book=allow_double_book,
        self_draft_id=draft_id,
    )

    conn.execute(
        """
        INSERT INTO draft_images (draft_id, image_id, slot) VALUES (%s,%s,%s)
        ON CONFLICT (draft_id, slot) DO UPDATE SET image_id = EXCLUDED.image_id
        """,
        (draft_id, image_id, slot),
    )
    # The claim. Permanent from here.
    if img["owner_account"] is None:
        conn.execute(
            "UPDATE images SET owner_account = %s, updated_at = NOW() WHERE id = %s",
            (draft["account"], image_id),
        )
    return {"draft_id": draft_id, "image_id": image_id, "slot": slot,
            "owner_account": draft["account"]}


def detach(conn: psycopg.Connection, *, draft_id: int, image_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM draft_images WHERE draft_id = %s AND image_id = %s",
        (draft_id, image_id),
    )
    if not cur.rowcount:
        return False
    # Release the claim only if it never published and nothing else holds it.
    # `post_desired_images` (0010) is a second holder: an image attached to a
    # live posting's desired set must not be released just because a draft let
    # go of it, or it could then be claimed by another account.
    conn.execute(
        """
        UPDATE images SET owner_account = NULL, updated_at = NOW()
        WHERE id = %s AND used_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM draft_images WHERE image_id = %s)
          AND NOT EXISTS (SELECT 1 FROM post_desired_images WHERE image_id = %s)
        """,
        (image_id, image_id, image_id),
    )
    return True


def images_for_draft(conn: psycopg.Connection, draft_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.*, di.slot FROM draft_images di
        JOIN images i ON i.id = di.image_id
        WHERE di.draft_id = %s ORDER BY di.slot
        """,
        (draft_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# The same three operations against a live posting's desired image set.
#
# `post_desired_images` mirrors `draft_images` exactly (DESIGN_EDITS decision
# 34), so these are the draft functions with one table swapped. They live here
# rather than in `services.edits` so that ownership, the cooldown and the
# cover/photo partition stay owned by one module — the live-post path drifted
# away from these rules once already.
# ---------------------------------------------------------------------------

def images_for_post(conn: psycopg.Connection, post_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.*, pdi.slot FROM post_desired_images pdi
        JOIN images i ON i.id = pdi.image_id
        WHERE pdi.post_id = %s ORDER BY pdi.slot
        """,
        (post_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def attach_to_post(
    conn: psycopg.Connection,
    *,
    post_id: str,
    account: str,
    image_id: int,
    slot: int,
    allow_double_book: bool = False,
) -> dict:
    """Bind an image to a slot of a live posting's desired set.

    Same rules as `attach`, including decision 13's permanent account claim: an
    approved image with no owner becomes this account's the moment it is staged
    on one of its listings.
    """
    img = _check_attachable(
        conn,
        image_id=image_id,
        account=account,
        slot=slot,
        allow_double_book=allow_double_book,
        self_post_id=post_id,
    )

    conn.execute(
        """
        INSERT INTO post_desired_images (post_id, image_id, slot) VALUES (%s,%s,%s)
        ON CONFLICT (post_id, slot) DO UPDATE SET image_id = EXCLUDED.image_id
        """,
        (post_id, image_id, slot),
    )
    if img["owner_account"] is None:
        conn.execute(
            "UPDATE images SET owner_account = %s, updated_at = NOW() WHERE id = %s",
            (account, image_id),
        )
    return {"post_id": post_id, "image_id": image_id, "slot": slot,
            "owner_account": account}


def fill_post_photo_slots(
    conn: psycopg.Connection,
    *,
    post_id: str,
    account: str,
    want: int,
    rng: random.Random | None = None,
) -> list[dict]:
    """Top up a live posting's empty photo slots. Never touches the cover."""
    rng = rng or random.Random()
    taken = {i["slot"] for i in images_for_post(conn, post_id)}
    free = [s for s in range(COVER_SLOT + 1, MAX_SLOTS + 1) if s not in taken][:want]
    if not free:
        return []

    candidates = pick_for_draft(
        conn, account=account, count=len(free) + 8, kind="photo", rng=rng
    )
    attached: list[dict] = []
    for slot, img in zip(free, candidates):
        try:
            attach_to_post(
                conn, post_id=post_id, account=account, image_id=img["id"], slot=slot
            )
            attached.append(img)
        except ValueError as e:
            logger.warning(f"could not attach image {img['id']} to post {post_id}: {e}")
    if len(attached) < want:
        logger.info(
            f"post {post_id}: filled {len(attached)}/{want} photo slot(s) — "
            f"the photo stack for {account} is short"
        )
    return attached


def attach_post_cover(
    conn: psycopg.Connection,
    *,
    post_id: str,
    account: str,
    rng: random.Random | None = None,
) -> dict | None:
    """Put a cover in slot 1 of a live posting's desired set if it is empty."""
    rng = rng or random.Random()
    if any(i["slot"] == COVER_SLOT for i in images_for_post(conn, post_id)):
        return None
    for img in pick_for_draft(conn, account=account, count=5, kind="cover", rng=rng):
        try:
            attach_to_post(
                conn, post_id=post_id, account=account,
                image_id=img["id"], slot=COVER_SLOT,
            )
            return img
        except ValueError as e:
            logger.warning(f"could not attach cover {img['id']} to post {post_id}: {e}")
    return None


def mark_used(conn: psycopg.Connection, image_ids: list[int]) -> None:
    if image_ids:
        conn.execute(
            "UPDATE images SET used_at = NOW(), updated_at = NOW() WHERE id = ANY(%s)",
            (image_ids,),
        )


def mark_confirmed_used(
    conn: psycopg.Connection, *, draft_id: int, photos_confirmed: int | None
) -> int:
    """Retire the images a failed post already pushed to Craigslist.

    `mark_used` only ever ran from `mark_posted`, so a draft that died at or
    after `photo_upload` left its images with `used_at IS NULL` — the database
    calling them fresh after Craigslist had already rendered thumbnails for
    them. Reuse of a burned photo is precisely what gets a listing ghosted.

    The desktop reports `photos_confirmed`: the number of thumbnails the site
    actually rendered. Uploads happen in slot order, so those are the first N
    attached. A missing count means we cannot tell, and the conservative
    reading — retire everything on the draft — is the right one: retiring a
    clean image early costs $0.0035, re-showing a burned one costs an account.

    Returns how many were retired.
    """
    attached = images_for_draft(conn, draft_id)
    if not attached:
        return 0
    take = len(attached) if photos_confirmed is None else max(0, photos_confirmed)
    burned = [i["id"] for i in attached[:take] if i["used_at"] is None]
    mark_used(conn, burned)
    return len(burned)


def roll_photo_count(
    rng: random.Random,
    *,
    photos_min: int = 1,
    photos_max: int = 5,
    imageless_rate: float = 0.10,
) -> int:
    """How many images this draft should carry.

    Separate from MAX_SLOTS: Craigslist permits 24, but every upload blocks
    until the site confirms its thumbnail, so a 24-photo post is minutes of
    browser time. The range is a setting so that trade-off stays yours.

    A share of posts (10% by default) deliberately carry nothing at all, so an
    account does not look mechanically identical every time.
    """
    if rng.random() < imageless_rate:
        return 0
    lo = max(0, min(photos_min, MAX_SLOTS))
    hi = max(lo, min(photos_max, MAX_SLOTS))
    return rng.randint(lo, hi) if hi else 0


def autoattach(
    conn: psycopg.Connection, *, draft_id: int, account: str, rng: random.Random | None = None
) -> list[dict]:
    """Fill a freshly generated draft's photo slots from the approved stack.

    **Photos only — the cover is yours.** Generation used to claim a cover per
    draft, which meant a background loop spent the hand-curated stack across 45
    queued drafts weeks before any of them published, and every "manual" cover
    choice was really an edit of a machine's choice. Slot 1 is now left empty
    and filled either by you in Review or, failing that, by `claim_next` at the
    moment the post actually goes out.

    A draft that rolls imageless takes nothing at all — no photos and no cover.
    That variation is the only thing left distinguishing one post's image
    profile from another's, so it survives the move to full 23-photo posts.

    Best-effort otherwise: a short stack yields a thinner post, never a failed
    draft. See `stack_health` for the shortfall this leaves behind.
    """
    rng = rng or random.Random()
    g = conn.execute(
        "SELECT photos_min, photos_max, imageless_rate FROM generation_settings LIMIT 1"
    ).fetchone()
    want = roll_photo_count(
        rng,
        photos_min=g["photos_min"],
        photos_max=g["photos_max"],
        imageless_rate=g["imageless_rate"],
    )
    if want == 0:
        return []
    return fill_photo_slots(conn, draft_id=draft_id, account=account, want=want, rng=rng)


def fill_photo_slots(
    conn: psycopg.Connection,
    *,
    draft_id: int,
    account: str,
    want: int,
    rng: random.Random | None = None,
) -> list[dict]:
    """Fill up to `want` empty photo slots (2..MAX_SLOTS) on a draft.

    Tops up rather than replaces: slots already holding an image are left
    alone, so pressing Autofill on a draft you have hand-curated adds to your
    choices instead of discarding them. The cover slot is never touched.
    """
    rng = rng or random.Random()
    taken = {i["slot"] for i in images_for_draft(conn, draft_id)}
    free = [s for s in range(COVER_SLOT + 1, MAX_SLOTS + 1) if s not in taken][:want]
    if not free:
        return []

    # Over-fetch: ownership and the partition are enforced by `attach`, and a
    # candidate can still be lost to a race, so asking for a few spare keeps a
    # full request from coming up one short.
    candidates = pick_for_draft(
        conn, account=account, count=len(free) + 8, kind="photo", rng=rng
    )
    attached: list[dict] = []
    for slot, img in zip(free, candidates):
        try:
            attach(conn, draft_id=draft_id, image_id=img["id"], slot=slot)
            attached.append(img)
        except ValueError as e:
            logger.warning(f"could not attach image {img['id']} to draft {draft_id}: {e}")
    if len(attached) < want:
        logger.info(
            f"draft {draft_id}: filled {len(attached)}/{want} photo slot(s) — "
            f"the photo stack for {account} is short"
        )
    return attached


def attach_cover(
    conn: psycopg.Connection,
    *,
    draft_id: int,
    account: str,
    rng: random.Random | None = None,
) -> dict | None:
    """Put a cover in slot 1 if it is empty. Returns the image, or None.

    The backstop behind manual cover selection: `claim_next` calls this in the
    moment it hands a draft out, so a draft you never got to still publishes
    with a real thumbnail rather than promoting a roof photo into slot 1 —
    which is what an empty slot 1 silently does, since the desktop uploads in
    slot order and Craigslist thumbnails whatever arrives first.
    """
    rng = rng or random.Random()
    if any(i["slot"] == COVER_SLOT for i in images_for_draft(conn, draft_id)):
        return None
    for img in pick_for_draft(conn, account=account, count=5, kind="cover", rng=rng):
        try:
            attach(conn, draft_id=draft_id, image_id=img["id"], slot=COVER_SLOT)
            return img
        except ValueError as e:
            logger.warning(f"could not attach cover {img['id']} to draft {draft_id}: {e}")
    return None


def pick_for_draft(
    conn: psycopg.Connection,
    *,
    account: str,
    count: int,
    kind: str = "photo",
    rng: random.Random | None = None,
) -> list[dict]:
    """Choose approved images this account may use, respecting the cooldown.

    Prefers never-used images, then the longest-idle. Returns fewer than asked
    for — including none — rather than reusing something too recently published.

    Filters on `kind` (defaulting to photos) and skips anything already
    attached to a live draft. Without the first filter this handed covers out
    as ordinary photos; without the second it handed the same photo to several
    queued drafts at once.
    """
    rng = rng or random.Random()
    cutoff = datetime.now(timezone.utc) - timedelta(days=REUSE_COOLDOWN_DAYS)
    rows = conn.execute(
        f"""
        SELECT * FROM images
        WHERE status = 'approved'
          AND kind = %(kind)s
          AND (owner_account IS NULL OR owner_account = %(account)s)
          AND (used_at IS NULL OR used_at < %(cutoff)s)
          AND NOT {_RESERVED_SQL}
        ORDER BY used_at NULLS FIRST, random()
        LIMIT %(count)s
        """,
        {
            "kind": kind,
            "account": account,
            "cutoff": cutoff,
            "count": count,
            "live": list(LIVE_DRAFT_STATUSES),
        },
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Archiving the pictures an ended posting used
#
# A recovered posting's image manifest holds Craigslist's own URLs. They resolve
# today and nothing says they will once Craigslist prunes the posting, so the
# only durable answer is to hold the bytes ourselves.
#
# Archived images go in this table like everything else, but at
# `status='archived'`: `pick_for_draft` requires 'approved', so they can never
# be handed to a draft, and `_BUCKET_SQL` files them under their own name rather
# than pretending they are available. They are a record of what ran, not stock.
# ---------------------------------------------------------------------------

ARCHIVED_STATUS = "archived"
RECOVERED_SOURCE = "recovered"

# Craigslist serves these from its own CDN and they are ordinary JPEGs; the cap
# is a sanity bound, not a real limit.
_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
_ARCHIVE_TIMEOUT = 30.0

# Craigslist encodes the size in the filename — `_50x50c`, `_300x300`,
# `_600x450`, `_1200x900` — and `_1200x900` is the largest it keeps. The
# manage/display page a scan reads renders its gallery strip at 50x50, so a
# manifest scraped from it points at 50-pixel thumbnails unless something asks
# for better. Normalising here as well as at the scraper means a manifest
# already in the database heals itself without a second trip to Craigslist.
_CL_VARIANT = re.compile(r"_\d+x\d+c?\.jpg$", re.I)
_LARGEST_VARIANT = "_1200x900.jpg"


def largest_variant(url: str) -> str:
    """Rewrite a Craigslist image URL to the full-size original.

    Anything unrecognised is returned unchanged: a working URL beats a clever
    one, and a 404 here means the only surviving copy of that picture is lost.
    """
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    return _CL_VARIANT.sub(_LARGEST_VARIANT, url)


def archive_post_images(conn: psycopg.Connection, post_id: str) -> dict:
    """Fetch a posting's images and keep our own copies.

    Rewrites `posts.images` in place, adding `image_id` and `sha256` to each
    entry it manages to store. An entry that already has an `image_id` is left
    alone, so this is safe to run repeatedly and cheap on the second pass —
    unless its URL was pointing at a downscaled variant, in which case the copy
    we hold is a thumbnail and gets replaced by the full-size original.
    """
    import httpx

    row = conn.execute(
        "SELECT account, images FROM posts WHERE post_id = %s", (post_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown post_id {post_id!r}")

    manifest = list(row["images"] or [])
    if not manifest:
        return {"post_id": post_id, "stored": 0, "already": 0, "failed": 0}

    stored = already = failed = upgraded = 0
    with httpx.Client(timeout=_ARCHIVE_TIMEOUT, follow_redirects=True) as client:
        for entry in manifest:
            url = entry.get("url")
            if not url:
                continue
            full = largest_variant(url)
            if full != url:
                # What we hold, if anything, came from a smaller variant.
                entry["url"] = url = full
                if entry.get("image_id"):
                    entry["image_id"] = entry["sha256"] = None
                    upgraded += 1
            if entry.get("image_id"):
                already += 1
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.content
                if not data or len(data) > _ARCHIVE_MAX_BYTES:
                    raise ValueError(f"{len(data)} bytes is not a usable image")
                mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                digest, rel, size = storage.put_bytes(data, mime)
            except Exception as e:
                logger.warning(f"could not archive {url} for post {post_id}: {e}")
                failed += 1
                continue

            # Content-addressed, so the same picture across two postings is one
            # row and one file.
            img = conn.execute(
                """
                INSERT INTO images (sha256, storage_path, mime, bytes_size, source,
                                    status, kind, owner_account, used_at)
                VALUES (%s,%s,%s,%s,%s,%s,'photo',%s, NOW())
                ON CONFLICT (sha256) DO UPDATE SET updated_at = NOW()
                RETURNING id
                """,
                (digest, rel, mime, size, RECOVERED_SOURCE, ARCHIVED_STATUS,
                 row["account"]),
            ).fetchone()
            entry["image_id"] = img["id"]
            entry["sha256"] = digest
            stored += 1

    conn.execute(
        "UPDATE posts SET images = %s::jsonb, updated_at = NOW() WHERE post_id = %s",
        (json.dumps(manifest), post_id),
    )
    if stored:
        logger.info(
            f"archived {stored} image(s) for post {post_id}"
            + (f" ({upgraded} replacing a downscaled copy)" if upgraded else "")
        )
    return {"post_id": post_id, "stored": stored, "already": already,
            "failed": failed, "upgraded": upgraded}


def posts_needing_archive(conn: psycopg.Connection, limit: int = 50) -> list[str]:
    """Postings whose pictures we do not yet hold at full size.

    Two cases, and the second is why this is not just "image_id IS NULL": an
    entry whose URL names a downscaled variant was archived from a thumbnail,
    so the copy we hold is 50 pixels wide and needs replacing.
    """
    # Raw string: the regex backslashes are for Postgres, and Python 3.12 warns
    # about `\d` in a plain literal — a warning that becomes an error later.
    rows = conn.execute(
        r"""
        SELECT post_id FROM posts
        WHERE jsonb_typeof(images) = 'array'
          AND jsonb_array_length(images) > 0
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(images) e
              WHERE e->>'url' IS NOT NULL
                AND (e->>'image_id' IS NULL
                     OR (e->>'url' ~* '_\d+x\d+c?\.jpg$'
                         AND e->>'url' !~* '_1200x900\.jpg$'))
          )
        ORDER BY posted_ts DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [r["post_id"] for r in rows]
