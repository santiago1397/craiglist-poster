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
import time
from pathlib import Path

import httpx
from loguru import logger

from .config import DATA_DIR

REQUEST_TIMEOUT = 20.0
IMAGE_TIMEOUT = 120.0

# Retry policy for reads. Three attempts over ~4s of sleep, which covers a
# container restart on the VPS without stalling the reporter daemon's ~15s
# poll loop for long. A connection reset fails immediately rather than burning
# the timeout, so the realistic worst case here is seconds, not a minute.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (1.0, 3.0)

# Statuses worth a second look.
#
# 404 is in the list, which normally it should not be. Traefik serves a plain
# "404 page not found" while the API container is being recreated, and that is
# indistinguishable at this layer from a genuinely wrong URL. A permanently
# misconfigured QUEUE_URL therefore costs ~4 extra seconds before reporting the
# same error it always would; a deploy window stops costing a posting slot.
# That trade is worth it.
RETRY_STATUS = frozenset({404, 408, 425, 429, 500, 502, 503, 504})

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


def _request(method: str, path: str, *, retry: bool | None = None, **kwargs) -> dict:
    """One call to the queue API, retried when that is safe.

    Retries default to on for GET and off for everything else, and the split is
    not stylistic. `POST /claim` and `POST /edits/claim` take work: if the
    server handled a claim and the response was lost on the way back, retrying
    claims a *second* draft while the first sits `claimed` until the stale-claim
    reaper releases it. A missed read costs a few seconds; a duplicated claim
    costs a draft. So reads retry and writes do not.

    Why this exists: there was no retry at all, and one dropped TCP connection
    ended the whole run — fail-closed, no second attempt until the next
    scheduled slot four hours later. Two of those were observed in a single
    day, one of them landing exactly on the 09:00 posting slot, which passed
    with nothing attempted. Every container restart on the VPS did the same
    thing, because Traefik answers 404 or 502 while the API is being recreated.
    """
    url = f"{_base_url()}{path}"
    # Resolved once: both raise QueueUnavailable when unset, and a missing
    # token is not something a retry can fix.
    headers = _headers()

    if retry is None:
        retry = method.upper() == "GET"
    attempts = RETRY_ATTEMPTS if retry else 1

    last: QueueUnavailable | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = httpx.request(
                method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
            )
        except httpx.HTTPError as e:
            last = QueueUnavailable(f"{method} {path} failed: {e!r}")
        else:
            # Auth is settled, not transient. Retrying a rejected token just
            # delays the message that says to reissue it.
            if resp.status_code == 401:
                raise QueueUnavailable(
                    "machine token rejected - reissue it in the dashboard "
                    "(Settings -> Machine tokens) and update MACHINE_TOKEN"
                )
            if resp.status_code // 100 == 2:
                try:
                    return resp.json()
                except ValueError as e:
                    # A 200 carrying something that is not JSON means we are
                    # talking to a proxy's error page, not the API. Fail closed
                    # rather than surfacing a JSON decode error from deep in a
                    # posting run.
                    raise QueueUnavailable(
                        f"{method} {path} returned {resp.status_code} but not JSON: "
                        f"{resp.text[:200]}"
                    ) from e
            else:
                last = QueueUnavailable(
                    f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
                if resp.status_code not in RETRY_STATUS:
                    raise last

        if attempt < attempts:
            delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            logger.warning(
                f"{method} {path or '/'} failed ({last}); "
                f"retrying in {delay:g}s (attempt {attempt + 1} of {attempts})"
            )
            time.sleep(delay)

    assert last is not None  # unreachable: the loop runs at least once
    raise last


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
    headers = _headers()

    # Retried for the same reason reads are, and it matters more here. A failed
    # download during an edit reconcile is fatal to the caller by design
    # (`fetch_post_images` refuses to publish a partial set, because the
    # reconcile replaces the live images wholesale), so one blip on one image
    # can leave a published ad flagged `degraded_live` and waiting on a human.
    # A truncated body is retried too: the digest check below is what proves a
    # download is whole, so a mismatch is a transport failure, not a bad image.
    content: bytes | None = None
    last: QueueUnavailable | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = httpx.get(
                url, headers=headers, timeout=IMAGE_TIMEOUT, follow_redirects=True
            )
        except httpx.HTTPError as e:
            last = QueueUnavailable(f"image {image_id} download failed: {e!r}")
        else:
            if resp.status_code // 100 != 2:
                last = QueueUnavailable(f"image {image_id} -> HTTP {resp.status_code}")
                if resp.status_code not in RETRY_STATUS:
                    raise last
            else:
                got = hashlib.sha256(resp.content).hexdigest()
                if got == sha256:
                    content = resp.content
                    break
                last = QueueUnavailable(
                    f"image {image_id} digest mismatch: "
                    f"expected {sha256[:12]}, got {got[:12]}"
                )

        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            logger.warning(
                f"image {image_id} failed ({last}); retrying in {delay:g}s "
                f"(attempt {attempt + 1} of {RETRY_ATTEMPTS})"
            )
            time.sleep(delay)

    if content is None:
        assert last is not None  # unreachable: the loop runs at least once
        raise last

    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(dest)
    logger.debug(f"cached image {image_id} ({len(content) // 1024} KB)")
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


def fetch_post_images(desired: dict) -> list[Path]:
    """Download the desired image set for a claimed edit, in slot order.

    Unlike `fetch_draft_images`, a missing image is fatal to the caller rather
    than skippable: the reconcile replaces the live image set wholesale
    (DESIGN_EDITS decision 33), so publishing a partial set would silently
    delete images from a live posting.
    """
    out: list[Path] = []
    for img in sorted(desired.get("images") or [], key=lambda i: i["slot"]):
        out.append(fetch_image(img["id"], img["sha256"], img.get("mime", "image/jpeg")))
    return out


def eligibility(accounts: list[str]) -> dict:
    return _request("GET", "/eligibility", params={"accounts": ",".join(accounts)})


# ---------------------------------------------------------------------------
# Post editing (DESIGN_EDITS.md)
# ---------------------------------------------------------------------------

def edits_pending(accounts: list[str], limit: int = 10) -> dict:
    """Hydration requests and reconcile candidates for this machine.

    Polled every ~15s by the reporter daemon (decision 29).
    """
    return _request(
        "GET", "/edits/pending",
        params={"accounts": ",".join(accounts), "limit": limit},
    )


def claim_edit(post_id: str) -> dict | None:
    """Atomically take one post's desired state, or None if someone beat us."""
    data = _request("POST", "/edits/claim", json={"post_id": post_id})
    desired = data.get("desired")
    if desired:
        logger.info(
            f"claimed edit for post {desired['post_id']} "
            f"(rev {desired['desired_rev']}, account {desired['account']})"
        )
    return desired


def post_requests(accounts: list[str], limit: int = 10) -> dict:
    """Drafts an operator pressed "Post now" on, for this machine's accounts.

    Polled every ~15s by the reporter daemon, which spawns a posting run for
    whatever comes back.
    """
    return _request(
        "GET", "/post-requests",
        params={"accounts": ",".join(accounts), "limit": limit},
    )


def claim(
    accounts: list[str], *, outbox_pending: int = 0, draft_id: int | None = None
) -> dict | None:
    """Atomically take the next draft to post, or None if there is nothing.

    Returns the draft dict. `None` covers every "not now" case — outside the
    window, cooldown, empty queue, outbox backlog — and the reason is logged.

    `draft_id` pins the claim to one operator-requested draft. The server still
    applies every guardrail, and still refuses unless that draft carries a live
    request — so this narrows what may be claimed, it never widens it.
    """
    payload = {"accounts": accounts, "outbox_pending": outbox_pending}
    if draft_id is not None:
        payload["draft_id"] = draft_id
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
