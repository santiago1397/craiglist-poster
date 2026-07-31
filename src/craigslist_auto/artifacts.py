"""Failure artifacts: capture on the desktop, spool to disk, upload to the VPS.

DESIGN.md decision 17, finally implemented (DESIGN_EDITS.md decision 35).

`_dump_page()` in stats.py and poster.py already write a screenshot and an HTML
dump on failure — into `logs/failures/`, on a machine nobody is looking at. For
the posting flow that was survivable because the selectors were known-good. The
Craigslist *edit* form has never been exercised by this codebase, so a broken
selector is the expected failure, and an error string without the page behind it
is not debuggable.

Artifacts spool to disk rather than riding the event outbox: the outbox batches
JSON and a 2MB base64 blob in a batch of 100 would be hostile to it. Spooling
keeps delivery durable across a VPS outage while leaving the outbox alone. The
artifact id is generated at capture time, so the event that references it can be
emitted immediately without waiting for the upload.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from loguru import logger

from .config import LOGS_DIR

SPOOL_DIR = LOGS_DIR / "artifacts"

# Server rejects above 2MB (see routers/queue.MAX_ARTIFACT_BYTES). Stay under it
# here so a capture never becomes an upload that can only ever fail.
MAX_BYTES = 2 * 1024 * 1024
UPLOAD_TIMEOUT = 30.0

# Screenshot quality ladder. A full-page JPEG of a long Craigslist posting can
# blow past 2MB, so step down rather than lose the artifact entirely.
_QUALITY_LADDER = (70, 45, 25)


def _new_id() -> str:
    return str(uuid.uuid4())


def _spool(
    artifact_id: str,
    data: bytes,
    *,
    kind: str,
    content_type: str,
    post_id: str | None,
    account: str | None,
    flow: str | None,
    label: str | None,
) -> None:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    blob = SPOOL_DIR / f"{artifact_id}.blob"
    meta = SPOOL_DIR / f"{artifact_id}.json"
    blob.write_bytes(data)
    meta.write_text(
        json.dumps({
            "id": artifact_id,
            "kind": kind,
            "content_type": content_type,
            "post_id": post_id,
            "account": account,
            "flow": flow,
            "label": label,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": len(data),
        }),
        encoding="utf-8",
    )


def capture_page(
    page,
    *,
    flow: str,
    label: str,
    post_id: str | None = None,
    account: str | None = None,
    include_html: bool = True,
) -> list[str]:
    """Screenshot + HTML dump of the current page. Returns artifact ids.

    Never raises: capturing evidence must not be the reason a flow dies. A
    failure here is logged and the caller carries on with whatever it has.
    """
    ids: list[str] = []

    for quality in _QUALITY_LADDER:
        try:
            shot = page.screenshot(type="jpeg", quality=quality, full_page=True)
        except Exception as e:
            logger.warning(f"artifact screenshot failed ({flow}/{label}): {e}")
            shot = None
            break
        if len(shot) <= MAX_BYTES:
            break
        logger.debug(
            f"screenshot {len(shot)//1024}KB exceeds cap at quality={quality}, stepping down"
        )
        shot = None

    if shot is None:
        # Last resort: viewport only, which is bounded by the window size.
        try:
            shot = page.screenshot(type="jpeg", quality=40, full_page=False)
            if len(shot) > MAX_BYTES:
                shot = None
        except Exception:
            shot = None

    if shot is not None:
        aid = _new_id()
        _spool(aid, shot, kind="screenshot", content_type="image/jpeg",
               post_id=post_id, account=account, flow=flow, label=label)
        ids.append(aid)

    if include_html:
        try:
            html = page.content().encode("utf-8", errors="replace")
            if len(html) > MAX_BYTES:
                # Truncate rather than drop: the head of the document carries
                # the form markup we actually need.
                html = html[:MAX_BYTES - 200] + b"\n<!-- TRUNCATED BY ARTIFACT CAP -->"
            aid = _new_id()
            _spool(aid, html, kind="html", content_type="text/html",
                   post_id=post_id, account=account, flow=flow, label=label)
            ids.append(aid)
        except Exception as e:
            logger.warning(f"artifact html dump failed ({flow}/{label}): {e}")

    if ids:
        logger.info(f"captured {len(ids)} artifact(s) for {flow}/{label}: {', '.join(ids)}")
    return ids


# ---------------------------------------------------------------------------
# Uploader — drained by the reporter daemon
# ---------------------------------------------------------------------------

def pending_count() -> int:
    if not SPOOL_DIR.exists():
        return 0
    return len(list(SPOOL_DIR.glob("*.json")))


def _queue_base() -> str | None:
    url = os.environ.get("QUEUE_URL", "").strip().rstrip("/")
    return url or None


def _token() -> str | None:
    tok = os.environ.get("MACHINE_TOKEN", "").strip()
    return tok or None


def flush_once(limit: int = 5) -> int:
    """Upload up to `limit` spooled artifacts. Returns how many were sent."""
    base, token = _queue_base(), _token()
    if not base or not token or not SPOOL_DIR.exists():
        return 0

    sent = 0
    for meta_path in sorted(SPOOL_DIR.glob("*.json"))[:limit]:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(f"unreadable artifact meta, discarding: {meta_path.name}")
            meta_path.unlink(missing_ok=True)
            continue
        blob_path = meta_path.with_suffix(".blob")
        if not blob_path.exists():
            meta_path.unlink(missing_ok=True)
            continue

        params = {
            "kind": meta["kind"],
            "content_type": meta["content_type"],
            "post_id": meta.get("post_id"),
            "account": meta.get("account"),
            "flow": meta.get("flow"),
            "label": meta.get("label"),
        }
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = httpx.put(
                f"{base}/artifacts/{meta['id']}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                content=blob_path.read_bytes(),
                timeout=UPLOAD_TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.debug(f"artifact upload deferred ({meta['id']}): {e!r}")
            break  # network is down; stop trying this cycle

        if resp.status_code // 100 == 2:
            blob_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            sent += 1
        elif resp.status_code in (401, 403):
            logger.warning("artifact upload rejected — check MACHINE_TOKEN")
            break
        elif 400 <= resp.status_code < 500:
            # Permanently unacceptable to the server (too large, bad kind).
            # Keeping it would block the spool forever.
            logger.warning(
                f"artifact {meta['id']} rejected ({resp.status_code}), discarding: "
                f"{resp.text[:200]}"
            )
            blob_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
        else:
            logger.debug(f"artifact upload 5xx for {meta['id']}, will retry")
            break

    if sent:
        logger.info(f"uploaded {sent} artifact(s)")
    return sent


def purge_older_than(days: int = 7) -> int:
    """Drop spooled artifacts that could never be delivered."""
    if not SPOOL_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for meta_path in SPOOL_DIR.glob("*.json"):
        try:
            if meta_path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        meta_path.with_suffix(".blob").unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        removed += 1
    return removed
