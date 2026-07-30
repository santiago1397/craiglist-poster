"""Desktop -> VPS queue client.

The desktop no longer decides what to post. It asks. This module is the only
place that talks to the queue endpoints; everything else on this machine goes
through the durable event outbox instead.

Environment:
  QUEUE_URL      — base URL of the queue API, e.g. https://api.example.com/queue
  MACHINE_TOKEN  — per-machine bearer token, format "<id>.<secret>". Issued in
                   the dashboard under Settings -> Machine tokens, shown once.

Every failure raises `QueueUnavailable`. Callers treat that as "do not post" —
fail-closed is the whole point (decision 1). Posting from a stale local guess
is exactly the behaviour the queue exists to remove.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
from loguru import logger

from .config import DATA_DIR

REQUEST_TIMEOUT = 20.0
IMAGE_TIMEOUT = 120.0

# Downloaded image bytes live here, named by digest. Content-addressed, so a
# cached file is always exactly the image the server means and never goes stale.
IMAGE_CACHE = DATA_DIR / "image_cache"


class QueueUnavailable(RuntimeError):
    """The queue could not be reached, or refused us. Never post after this."""


def _base_url() -> str:
    url = os.environ.get("QUEUE_URL", "").strip().rstrip("/")
    if not url:
        raise QueueUnavailable("QUEUE_URL is not set")
    return url


def _headers() -> dict[str, str]:
    token = os.environ.get("MACHINE_TOKEN", "").strip()
    if not token:
        raise QueueUnavailable("MACHINE_TOKEN is not set")
    return {"Authorization": f"Bearer {token}"}


def _request(method: str, path: str, **kwargs) -> dict:
    url = f"{_base_url()}{path}"
    try:
        resp = httpx.request(
            method, url, headers=_headers(), timeout=REQUEST_TIMEOUT, **kwargs
        )
    except httpx.HTTPError as e:
        raise QueueUnavailable(f"{method} {path} failed: {e!r}") from e
    if resp.status_code == 401:
        raise QueueUnavailable(
            "machine token rejected — reissue it in the dashboard "
            "(Settings -> Machine tokens) and update MACHINE_TOKEN"
        )
    if resp.status_code // 100 != 2:
        raise QueueUnavailable(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def fetch_settings() -> dict:
    """Server-owned guardrails. Caller must clamp via config.clamp_guardrails."""
    return _request("GET", "/settings").get("guardrails", {})


def fetch_queue(limit: int = 10) -> list[dict]:
    """Prefetch window. Read-only; claims nothing."""
    return _request("GET", "", params={"limit": limit}).get("drafts", [])


_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def fetch_image(image_id: int, sha256: str, mime: str = "image/jpeg") -> Path:
    """Download one image into the local cache and return its path.

    Cached by digest, so a file already present is byte-identical to what the
    server holds and is reused without a round trip. The download is verified
    against the digest before being accepted — a truncated file handed to
    Craigslist would publish a broken image.
    """
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    dest = IMAGE_CACHE / f"{sha256}{_EXT_BY_MIME.get(mime, '.jpg')}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    url = f"{_base_url().rsplit('/', 1)[0]}/images/{image_id}/raw"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=IMAGE_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as e:
        raise QueueUnavailable(f"image {image_id} download failed: {e!r}") from e
    if resp.status_code // 100 != 2:
        raise QueueUnavailable(f"image {image_id} -> HTTP {resp.status_code}")

    got = hashlib.sha256(resp.content).hexdigest()
    if got != sha256:
        raise QueueUnavailable(
            f"image {image_id} digest mismatch: expected {sha256[:12]}, got {got[:12]}"
        )

    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    logger.debug(f"cached image {image_id} ({len(resp.content) // 1024} KB)")
    return dest


def fetch_draft_images(draft: dict) -> list[Path]:
    """Download every image attached to a claimed draft, in slot order.

    Images are optional: one that cannot be fetched is skipped with a warning
    rather than aborting the post. Slot 1 is the Craigslist thumbnail, so order
    is preserved exactly as the server assigned it.
    """
    out: list[Path] = []
    for img in sorted(draft.get("images") or [], key=lambda i: i["slot"]):
        try:
            out.append(fetch_image(img["id"], img["sha256"], img.get("mime", "image/jpeg")))
        except QueueUnavailable as e:
            logger.warning(f"skipping image in slot {img['slot']}: {e}")
    return out


def eligibility(accounts: list[str]) -> dict:
    return _request("GET", "/eligibility", params={"accounts": ",".join(accounts)})


def claim(accounts: list[str], *, outbox_pending: int = 0) -> dict | None:
    """Atomically take the next draft to post, or None if there is nothing.

    Returns the draft dict. `None` covers every "not now" case — outside the
    window, cooldown, empty queue, outbox backlog — and the reason is logged.
    """
    payload = {"accounts": accounts, "outbox_pending": outbox_pending}
    data = _request("POST", "/claim", json=payload)

    draft = data.get("draft")
    if draft:
        logger.info(
            f"claimed draft {draft['id']} for {draft['account']}: {draft['title']!r}"
        )
        return draft

    if data.get("refused"):
        logger.warning(f"claim refused ({data['refused']}): {data.get('detail')}")
        return None

    report = data.get("eligibility") or {}
    for reason in report.get("global_blocks") or []:
        logger.info(f"not posting: {reason}")
    for name, info in (report.get("accounts") or {}).items():
        if not info.get("eligible"):
            logger.info(f"  {name}: {'; '.join(info.get('reasons') or [])}")
    return None
