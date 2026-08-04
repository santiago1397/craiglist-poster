"""Turn a third-party photo into a row in the image stack.

Sits between `companycam.py` (bytes off the wire) and `images.py` (the stack's
own rules). Nothing here knows what CompanyCam is; it takes bytes and an
external id and is reusable for any future source.

Why this does not call `images._store`
--------------------------------------
`_store` returns None on an sha256 conflict and that is deliberate — it is what
makes `was_created_by_key` a real authorisation check and what produces the 409
on a duplicate upload. An importer needs the opposite: on conflict it wants the
id of the row that already holds those bytes, so it can record "this remote
photo maps to that image" and never download it again. Two callers, two
contracts; widening `_store` to serve both would weaken the security property
for the sake of a batch job.
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

import psycopg
from loguru import logger
from PIL import Image, ImageOps

from .. import storage

# Craigslist's largest display variant is 1200x900 (see images._LARGEST_VARIANT),
# so 1600 on the long edge leaves headroom for a crop without carrying a phone's
# full 4032px sensor output around forever.
DEFAULT_MAX_EDGE = 1600
DEFAULT_QUALITY = 85

# Everything is re-encoded to this. Not a preference — see `normalise`.
OUTPUT_MIME = "image/jpeg"

STATES = ("imported", "duplicate", "failed", "skipped")

# A photo that has failed this many times is not going to start working.
MAX_ATTEMPTS = 3


class ImportError_(RuntimeError):
    """One photo could not be turned into an image. Never fatal to a run."""


def normalise(
    data: bytes,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_QUALITY,
) -> bytes:
    """Re-encode to a plain, correctly-oriented, metadata-free JPEG.

    Three separate production bugs are fixed by the four lines below, and each
    one is invisible in testing with a synthetic fixture:

    **Orientation.** Phone cameras write the sensor's raw buffer and set EXIF
    `Orientation` to 6 or 8; every viewer un-rotates on the way out. Strip the
    EXIF without applying it first and a large share of roof photos publish
    *sideways* on Craigslist — permanently, and undetectably from the database.
    `exif_transpose` must run before the strip, not after.

    **Format.** iPhone originals are HEIC. `storage.relative_path` maps only
    jpeg/png/webp and falls through to `.bin`, which the desktop would later
    hand to Craigslist's file input at post time — so the failure would surface
    as a posting error on a live account, hours later, nowhere near the import.
    Re-encoding makes the whole class impossible rather than adding a MIME entry.

    **GPS.** These are photographs of customers' homes and carry the coordinates
    to prove it. Saving without `exif=` drops the block entirely.

    Downscaling rides along free, since the re-encode is already happening.
    """
    try:
        im = Image.open(BytesIO(data))
        # Apply the orientation tag while it still exists.
        im = ImageOps.exif_transpose(im)
        # JPEG cannot hold alpha, and a PNG screenshot with transparency would
        # otherwise raise on save.
        if im.mode != "RGB":
            im = im.convert("RGB")
        if max_edge > 0:
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = BytesIO()
        # No `exif=` argument: this is the strip.
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except ImportError_:
        raise
    except Exception as e:
        raise ImportError_(f"could not decode or re-encode image: {e}") from e


def store_external(
    conn: psycopg.Connection,
    data: bytes,
    *,
    source: str,
    kind: str = "photo",
    status: str = "pending",
) -> tuple[int, bool]:
    """Persist normalised bytes. Returns `(image_id, was_new)`.

    Unlike `images._store`, a conflict is an answer rather than a refusal: the
    caller wants the id of whatever already holds these bytes so the ledger can
    point at it.
    """
    digest, rel, size = storage.put_bytes(data, OUTPUT_MIME)
    row = conn.execute(
        """
        INSERT INTO images (sha256, storage_path, mime, bytes_size, source,
                            status, kind)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING id
        """,
        (digest, rel, OUTPUT_MIME, size, source, status, kind),
    ).fetchone()
    if row is not None:
        return int(row["id"]), True

    existing = conn.execute(
        "SELECT id FROM images WHERE sha256 = %s", (digest,)
    ).fetchone()
    if existing is None:  # pragma: no cover — only reachable on a concurrent delete
        raise ImportError_("image vanished between insert and lookup")
    return int(existing["id"]), False


# ---------------------------------------------------------------------------
# The ledger. Keyed on the remote id, because our sha256 is a hash of *our*
# bytes and two remote photos routinely normalise to the same ones.
# ---------------------------------------------------------------------------


def ledger_entry(
    conn: psycopg.Connection, *, source: str, external_id: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM image_sources WHERE source = %s AND external_id = %s",
        (source, str(external_id)),
    ).fetchone()
    return dict(row) if row else None


def should_import(entry: dict | None, *, retry_failed: bool = False) -> bool:
    """Has this remote photo already been dealt with?

    A deliberate rejection or deletion must survive: the ledger row stays even
    when `images.id` goes to NULL, so the next run skips it rather than helpfully
    re-importing what somebody threw away.
    """
    if entry is None:
        return True
    if entry["state"] != "failed":
        return False
    if retry_failed:
        return True
    return int(entry["attempts"] or 0) < MAX_ATTEMPTS


def record(
    conn: psycopg.Connection,
    *,
    source: str,
    external_id: str,
    state: str,
    image_id: int | None = None,
    sha256: str | None = None,
    remote_hash: str | None = None,
    remote_url: str | None = None,
    captured_at: datetime | None = None,
    project_id: str | None = None,
    description: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert the ledger row. `attempts` only advances on failure."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; expected one of {STATES}")
    conn.execute(
        """
        INSERT INTO image_sources (
            source, external_id, image_id, sha256, remote_hash, remote_url,
            captured_at, project_id, description, state, error,
            attempts, updated_at
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (source, external_id) DO UPDATE SET
            image_id    = COALESCE(EXCLUDED.image_id, image_sources.image_id),
            sha256      = COALESCE(EXCLUDED.sha256, image_sources.sha256),
            remote_hash = COALESCE(EXCLUDED.remote_hash, image_sources.remote_hash),
            remote_url  = COALESCE(EXCLUDED.remote_url, image_sources.remote_url),
            captured_at = COALESCE(EXCLUDED.captured_at, image_sources.captured_at),
            project_id  = COALESCE(EXCLUDED.project_id, image_sources.project_id),
            description = COALESCE(EXCLUDED.description, image_sources.description),
            state       = EXCLUDED.state,
            error       = EXCLUDED.error,
            attempts    = image_sources.attempts
                          + CASE WHEN EXCLUDED.state = 'failed' THEN 1 ELSE 0 END,
            updated_at  = NOW()
        """,
        # Every text field goes through `as_text`, not just description. A
        # remote API is free to change the shape of any of these, and an import
        # that dies half way through is a worse outcome than a field we could
        # not parse.
        (
            source, str(external_id), image_id, as_text(sha256),
            as_text(remote_hash), as_text(remote_url),
            captured_at, as_text(project_id), as_text(description), state,
            (as_text(error) or "")[:500] or None,
            1 if state == "failed" else 0,
        ),
    )


def as_text(value) -> str | None:
    """Coerce a remote field to something a TEXT column will accept.

    CompanyCam's `description` is documented as a string and is usually null or
    a string — but roughly one photo in a thousand returns a rich-text object:

        {"id": "23041662",
         "html_content": "<p>No entry to find leak</p>",
         "plain_text_content": "No entry to find leak"}

    psycopg refuses to adapt a dict, so a single one of those killed an import
    550 photos in. Prefer the plain-text field, fall back to any string value,
    and never let an unexpected shape stop a run — the description is a nicety
    for the review session, not something worth failing over.
    """
    if value is None or isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("plain_text_content", "text", "content", "html_content"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return None
    if isinstance(value, (list, tuple)):
        parts = [as_text(v) for v in value]
        joined = " ".join(p for p in parts if p)
        return joined or None
    return str(value)


def unix_to_dt(value) -> datetime | None:
    """CompanyCam timestamps are unix seconds. Bad values are not worth a crash."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def ledger_summary(conn: psycopg.Connection, source: str) -> dict:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM image_sources WHERE source = %s "
        "GROUP BY state",
        (source,),
    ).fetchall()
    out = {s: 0 for s in STATES}
    for r in rows:
        out[r["state"]] = r["n"]
    return out
