from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from loguru import logger

from .accounts import mark_ghosted
from .config import (
    CL_SEARCH_URL,
    GHOST_CHECK_PROXY_HOST,
    GHOST_CHECK_PROXY_PORT,
    GHOST_LOG,
    STATE_FILE,
)


def _build_ghost_proxy_url() -> str | None:
    """Return the InstantProxies HTTP proxy URL, or None if creds missing."""
    user = os.environ.get("INSTANTPROXIES_USER")
    pw = os.environ.get("INSTANTPROXIES_PASS")
    if not (user and pw):
        return None
    return f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{GHOST_CHECK_PROXY_HOST}:{GHOST_CHECK_PROXY_PORT}"


def _verify_proxy_exit_ip(proxy_url: str, expected_ip: str) -> None:
    """Confirm the proxy is up AND egresses from the IP we expect. Raises on mismatch."""
    with httpx.Client(timeout=15, proxy=proxy_url) as c:
        seen = c.get("https://api.ipify.org").text.strip()
    if seen != expected_ip:
        raise RuntimeError(
            f"Proxy exit IP mismatch: got {seen!r}, expected {expected_ip!r}. "
            "Aborting to avoid leaking ghost-check traffic from the wrong IP."
        )


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"posts": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _search_html(query: str, proxy: str | None = None) -> str:
    """
    Fetch CL search results as anonymous user.
    For a TRUE ghost check, run this from a different network than the posting machine
    (set HTTP_PROXY/HTTPS_PROXY env vars to a residential proxy or your phone hotspot).
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


def check_all_recent(proxy: str | None = None) -> None:
    # Fail-closed: ghost-check MUST go through a non-home IP. If caller didn't
    # pass an explicit --proxy, build one from env. If neither is available,
    # abort rather than silently leaking checks from the posting machine's IP.
    if proxy is None:
        proxy = _build_ghost_proxy_url()
    if proxy is None:
        raise RuntimeError(
            "Refusing to run ghost-check from the local IP. "
            "Set INSTANTPROXIES_USER/INSTANTPROXIES_PASS in .env, or pass --proxy."
        )
    # If using the configured InstantProxies host, verify exit IP matches.
    if GHOST_CHECK_PROXY_HOST in proxy:
        _verify_proxy_exit_ip(proxy, GHOST_CHECK_PROXY_HOST)
        logger.info(f"ghost-check proxy verified: egressing as {GHOST_CHECK_PROXY_HOST}")
    else:
        logger.info("ghost-check using caller-supplied --proxy (skipping exit-IP verify)")

    state = _load_state()
    now = datetime.now(timezone.utc).isoformat()
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
            }
            f.write(json.dumps(entry) + "\n")
            status = "VISIBLE" if visible else "GHOSTED"
            logger.info(f"[{p['account']}] {status}  {p['url']}")
