from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from loguru import logger

from .accounts import mark_ghosted
from .config import CL_SEARCH_URL, GHOST_LOG, STATE_FILE


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"posts": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _search_html(query: str, proxy: str | None = None) -> str:
    """
    Fetch CL search results as anonymous user.
    For a TRUE ghost check, run this from a different network than the posting
    machine — pass `--proxy` (e.g. a phone hotspot's proxy) so the request does
    not egress from the IP that created the post.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True, proxy=proxy) as c:
        r = c.get(CL_SEARCH_URL, params={"query": query})
        r.raise_for_status()
        return r.text


def check_post_visibility(post_url: str, title: str, *, proxy: str | None = None) -> bool:
    """
    Returns True if the ad appears in public search, False otherwise.
    Strategy: search by exact title keywords; check whether the post URL appears
    in the results HTML.
    """
    # Use first 4-6 distinctive words from title as query
    words = [w for w in re.findall(r"[A-Za-z0-9]+", title) if len(w) > 3]
    query = " ".join(words[:5]) or title[:40]
    html = _search_html(query, proxy=proxy)
    # CL post URLs end with /<id>.html — match the id
    m = re.search(r"/(\d+)\.html", post_url or "")
    if not m:
        # fall back to substring of url path
        return (post_url or "") in html
    post_id = m.group(1)
    return post_id in html


def check_all_recent(proxy: str | None = None, *, allow_local_ip: bool = False) -> None:
    # Still fail-closed by default: a check run from the posting machine's own
    # IP is not trustworthy, because CL keeps showing you your own posts even
    # after they're ghosted for everyone else. There is no built-in proxy any
    # more — the caller supplies one, or explicitly accepts the weaker check.
    if proxy is None and not allow_local_ip:
        raise RuntimeError(
            "Refusing to run ghost-check from the local IP — results would be "
            "unreliable (CL shows you your own ghosted posts). Pass --proxy "
            "http://host:port to check from another network, or --allow-local-ip "
            "to accept the weaker check."
        )
    if proxy:
        logger.info("ghost-check using caller-supplied --proxy")
    else:
        logger.warning(
            "ghost-check running from the LOCAL IP (--allow-local-ip). "
            "'VISIBLE' results here are not proof a post isn't ghosted."
        )

    from . import reporter
    from .events import GhostCheck
    from .stats import extract_post_id

    state = _load_state()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    GHOST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with GHOST_LOG.open("a", encoding="utf-8") as f:
        for p in state.get("posts", []):
            if not p.get("url"):
                continue
            visible = check_post_visibility(p["url"], p["title"], proxy=proxy)
            mark_ghosted(p["account"], p["at"], not visible)
            entry = {
                "checked_at": now,
                "account": p["account"],
                "post_at": p["at"],
                "url": p["url"],
                "visible": visible,
                # Records how much this row can be trusted — an unproxied check
                # can report 'visible' for a post that's ghosted to the public.
                "proxied": bool(proxy),
            }
            f.write(json.dumps(entry) + "\n")
            status = "VISIBLE" if visible else "GHOSTED"
            logger.info(f"[{p['account']}] {status}  {p['url']}")

            post_id = extract_post_id(p["url"])
            if post_id:
                try:
                    reporter.emit(GhostCheck(
                        ts=now_dt,
                        post_id=post_id,
                        account=p["account"],
                        ghosted=not visible,
                    ))
                except Exception as e:
                    logger.warning(f"ghost_check emit failed for {post_id}: {e}")
