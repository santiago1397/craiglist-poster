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
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import psycopg
from loguru import logger

from .. import storage
from ..config import get_settings
from .imagegen import ImageGenError, build_provider

# Matches the desktop's historic photo rule: an image may be reused within its
# owning account, but not for 30 days.
REUSE_COOLDOWN_DAYS = 30
MAX_SLOTS = 5

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

def generate_images(
    conn: psycopg.Connection,
    *,
    count: int = 4,
    city: str | None = None,
    kind: str = "photo",
    rng: random.Random | None = None,
) -> dict:
    """Generate into the pending shelf. Never raises for provider problems —
    returns what succeeded plus the error, because a failed batch must not take
    the caller down with it."""
    rng = rng or random.Random()
    g = conn.execute("SELECT * FROM generation_settings LIMIT 1").fetchone()
    settings = get_settings()

    template = (g["image_prompt"] or "").strip() or DEFAULT_IMAGE_PROMPT
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
        prompt = template.format(
            city=city or "South Florida",
            kind=rng.choice(ROOF_KINDS),
        )
        try:
            blobs = provider.generate(prompt, aspect=g["image_aspect"], n=1)
        except ImageGenError as e:
            error = str(e)
            logger.warning(f"image generation failed: {e}")
            break
        for data in blobs:
            row = _store(
                conn, data,
                source="generated", kind=kind, prompt=prompt,
                provider=provider.name, model=g["image_model"], cost=unit_cost,
            )
            if row:
                created.append(row)

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
) -> dict | None:
    """Persist bytes + row. Returns None if we already hold these exact bytes."""
    digest, rel, size = storage.put_bytes(data, mime)
    row = conn.execute(
        """
        INSERT INTO images (sha256, storage_path, mime, bytes_size, source,
                            status, kind, prompt, provider, model, cost_usd)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING *
        """,
        (digest, rel, mime, size, source, status, kind, prompt, provider, model, cost),
    ).fetchone()
    if row is None:
        logger.debug(f"image {digest[:12]} already stored; skipping duplicate")
        return None
    return dict(row)


def store_upload(conn: psycopg.Connection, data: bytes, *, mime: str, kind: str = "photo") -> dict | None:
    """Uploaded images skip the shelf — you chose the file, so it is approved."""
    return _store(conn, data, source="uploaded", kind=kind, mime=mime, status="approved")


# ---------------------------------------------------------------------------
# Shelf / pool
# ---------------------------------------------------------------------------

def list_images(
    conn: psycopg.Connection,
    *,
    status: str | None = None,
    account: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where, params = ["TRUE"], {"limit": limit, "offset": offset}
    if status:
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
        f"SELECT * FROM images WHERE {clause} ORDER BY created_at DESC "
        f"LIMIT %(limit)s OFFSET %(offset)s",
        params,
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM images WHERE {clause}", params
    ).fetchone()["n"]
    return {"images": [dict(r) for r in rows], "total": total}


def set_status(conn: psycopg.Connection, image_id: int, status: str) -> dict | None:
    if status not in ("pending", "approved", "rejected"):
        raise ValueError(f"invalid status: {status}")
    row = conn.execute(
        "UPDATE images SET status = %s, updated_at = NOW() WHERE id = %s RETURNING *",
        (status, image_id),
    ).fetchone()
    return dict(row) if row else None


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
    }


# ---------------------------------------------------------------------------
# Attachment — this is where an image becomes an account's forever
# ---------------------------------------------------------------------------

def attach(conn: psycopg.Connection, *, draft_id: int, image_id: int, slot: int) -> dict:
    if not (1 <= slot <= MAX_SLOTS):
        raise ValueError(f"slot must be 1-{MAX_SLOTS}")
    draft = conn.execute(
        "SELECT id, account, status FROM drafts WHERE id = %s", (draft_id,)
    ).fetchone()
    if draft is None:
        raise ValueError("draft not found")
    img = conn.execute("SELECT * FROM images WHERE id = %s", (image_id,)).fetchone()
    if img is None:
        raise ValueError("image not found")
    if img["status"] != "approved":
        raise ValueError("image is not approved yet")
    if img["owner_account"] and img["owner_account"] != draft["account"]:
        raise ValueError(
            f"image belongs to {img['owner_account']} and cannot be reused by "
            f"{draft['account']}"
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
    conn.execute(
        """
        UPDATE images SET owner_account = NULL, updated_at = NOW()
        WHERE id = %s AND used_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM draft_images WHERE image_id = %s)
        """,
        (image_id, image_id),
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


def mark_used(conn: psycopg.Connection, image_ids: list[int]) -> None:
    if image_ids:
        conn.execute(
            "UPDATE images SET used_at = NOW(), updated_at = NOW() WHERE id = ANY(%s)",
            (image_ids,),
        )


def pick_for_draft(
    conn: psycopg.Connection, *, account: str, count: int, rng: random.Random | None = None
) -> list[dict]:
    """Choose approved images this account may use, respecting the cooldown.

    Prefers never-used images, then the longest-idle. Returns fewer than asked
    for — including none — rather than reusing something too recently published.
    """
    rng = rng or random.Random()
    cutoff = datetime.now(timezone.utc) - timedelta(days=REUSE_COOLDOWN_DAYS)
    rows = conn.execute(
        """
        SELECT * FROM images
        WHERE status = 'approved'
          AND (owner_account IS NULL OR owner_account = %s)
          AND (used_at IS NULL OR used_at < %s)
        ORDER BY used_at NULLS FIRST, random()
        LIMIT %s
        """,
        (account, cutoff, count),
    ).fetchall()
    return [dict(r) for r in rows]
