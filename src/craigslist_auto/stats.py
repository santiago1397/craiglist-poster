from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from loguru import logger
from patchright.sync_api import Page

from .config import ACCOUNTS, CL_ACCOUNT_URL, DATA_DIR, LOGS_DIR, Account, machine_name
from .human import read_pause, sleep_jitter
from .poster import launch_account

STATS_DB = DATA_DIR / "stats.sqlite"
STATS_HEALTH = DATA_DIR / "stats_health.json"
FAILURES_DIR = LOGS_DIR / "failures"

# CL_ACCOUNT_URL now lives in config (poster.py resolves post URLs there too and
# cannot import this module — stats imports poster). Re-exported for editor.py.
ET = ZoneInfo("America/New_York")

# CL exposes four per-post counters (confirmed against real DOM 2026-07-01):
#   impressions = eye icon      — times the listing was rendered in a results page
#   views       = point-up icon — clicks into the full posting
#   shares      = share icon
#   favorites   = star icon
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id       TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    posted_ts     TEXT,          -- ISO8601 UTC
    source        TEXT NOT NULL  -- 'stats_sync' | 'state_json_seed'
);

CREATE TABLE IF NOT EXISTS snapshots (
    post_id            TEXT NOT NULL,
    snapshot_date      TEXT NOT NULL,  -- YYYY-MM-DD in America/New_York
    snapshot_ts_utc    TEXT NOT NULL,
    status             TEXT,           -- 'Active' | 'Inactive' | 'gone_from_active' | ...
    impressions        INTEGER,        -- NULL when CL says "stats available in ~N hours"
    views              INTEGER,
    shares             INTEGER,
    favorites          INTEGER,
    area               TEXT,
    category           TEXT,
    expires_in_days    INTEGER,
    autorepost         TEXT,
    freshness_note     TEXT,
    PRIMARY KEY (post_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_posts_account ON posts(account);
"""


def _connect() -> sqlite3.Connection:
    STATS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _today_et() -> str:
    return datetime.now(ET).date().isoformat()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------

def _load_health() -> dict:
    if not STATS_HEALTH.exists():
        return {}
    try:
        return json.loads(STATS_HEALTH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_health(health: dict) -> None:
    STATS_HEALTH.parent.mkdir(parents=True, exist_ok=True)
    STATS_HEALTH.write_text(json.dumps(health, indent=2, default=str), encoding="utf-8")


def _record_health(account_name: str, ok: bool, error_type: str | None = None, message: str | None = None) -> None:
    health = _load_health()
    health[account_name] = {
        "ok": ok,
        "last_run_ts_utc": _now_utc_iso(),
        "error_type": error_type,
        "message": message,
    }
    _save_health(health)


def health_report() -> dict:
    return _load_health()


# ---------------------------------------------------------------------------
# DOM scraping
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"[\d,]+")


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    m = _INT_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_expires_days(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _parse_posted_date(text: str | None) -> str | None:
    """CL shows e.g. '30 Jun 2026 10:01'. Return ISO UTC or None."""
    if not text:
        return None
    for fmt in ("%d %b %Y %H:%M", "%d %B %Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            return dt.replace(tzinfo=ET).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _dump_page(page: Page, account_name: str, tag: str) -> None:
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = FAILURES_DIR / f"{ts}_{account_name}_stats_{tag}"
    try:
        page.screenshot(path=str(stem) + ".png", full_page=True)
    except Exception as e:
        logger.warning(f"screenshot failed: {e}")
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        logger.warning(f"html dump failed: {e}")
    logger.error(f"[{account_name}] stats page dumped → {stem}.png/.html")


class LoginExpiredError(RuntimeError):
    pass


class ScrapeParseError(RuntimeError):
    pass


def _ensure_active_tab(page: Page, account_name: str) -> None:
    """Back-compat shim: the stats scrape only ever wants the active tab."""
    _ensure_tab(page, account_name, "active")


def _ensure_tab(page: Page, account_name: str, tab: str = "active") -> None:
    """Navigate to the postings page and submit one of its filters.

    The account page keeps ended postings under their own filter, and they are
    the only remaining record of what those ads said — Craigslist stops serving
    the public URL and the edit form, so nothing else can reach them.

    `tab` is the filter button's value: 'active', 'inactive', 'deleted'.
    """
    page.goto(CL_ACCOUNT_URL, wait_until="domcontentloaded")
    sleep_jitter(2.0)
    logger.debug(f"[{account_name}] landed on {page.url}")

    # Login detection: real login page shows a password input and no postings
    # tab bar; logged-in page shows the postings table + filter buttons.
    login_form_visible = page.locator("input[type='password']").count() > 0
    filters_visible = page.locator("button[name='filter_active']").count() > 0
    if login_form_visible and not filters_visible:
        raise LoginExpiredError(f"account {account_name} is not logged in (url={page.url})")

    # The filters are form-submit buttons, not links.
    try:
        btn = page.locator(f"button[name='filter_active'][value='{tab}']")
        if btn.count() == 0:
            # Craigslist has named this control differently over time; fall back
            # to any submit button carrying the tab's value.
            btn = page.locator(f"button[type='submit'][value='{tab}']")
        if btn.count() > 0:
            btn.first.click()
            page.wait_for_load_state("domcontentloaded")
            sleep_jitter(1.5)
        else:
            logger.warning(f"[{account_name}] no '{tab}' filter button on the page")
    except Exception as e:
        logger.debug(f"[{account_name}] could not submit '{tab}' filter: {e}")

    # Wait for the postings table body to actually render.
    try:
        page.wait_for_selector("table.accthp_postings tbody tr.posting-row, .postinglist-header", timeout=10000)
    except Exception:
        logger.warning(f"[{account_name}] postings table did not appear within 10s")


def _scrape_current_page(page: Page, account_name: str) -> list[dict]:
    """Extract all post rows on the current page as list of dicts.

    CL structure (per accounts.craigslist.org DOM 2026-07-01):
      <tr class="posting-row">     -- one per post
        <td class="status" data-postingid="123..."> Active/Inactive </td>
        <td class="buttons"> display/delete/repost/edit </td>
        <td class="title"><a href="/view/d/.../HASH">TITLE</a></td>
        <td class="areacat"><b><abbr>mia</abbr> - <abbr>pbc</abbr></b> skilled trade services</td>
        <td class="dates posteddate"><time datetime="ISO"/></td>
        <td class="autorepost"><a>enable</a></td>
        <td class="dates expdate">29 days</td>
        <td class="postingID">7944...</td>
      </tr>
      <tr class="account-post-metrics-row">   -- one per post, the metrics
        <td colspan=10>
          either <p class="account-post-no-metrics">stats available in ~N hours</p>
          or a .account-post-metrics-content with 4x div[title=impressions|views|shares|favorites]
        </td>
      </tr>
    """
    rows_data = page.evaluate(
        """
        () => {
            const out = [];
            const rows = document.querySelectorAll('tr.posting-row');
            for (const tr of rows) {
                const statusCell = tr.querySelector('td.status');
                const postId = (statusCell && statusCell.dataset.postingid)
                    || (tr.querySelector('td.postingID')?.textContent.trim() || '');
                if (!postId) continue;

                // Status: the visible <small.gc> (the hidden one has style="display:none")
                let status = '';
                if (statusCell) {
                    const smalls = statusCell.querySelectorAll('small.gc');
                    for (const s of smalls) {
                        const style = s.getAttribute('style') || '';
                        if (!/display\\s*:\\s*none/i.test(style)) {
                            status = s.textContent.trim();
                            break;
                        }
                    }
                    if (!status) status = statusCell.textContent.trim();
                }

                const titleA = tr.querySelector('td.title a');
                const title = titleA ? titleA.textContent.trim() : null;
                const url = titleA ? titleA.href : null;

                // Area = joined <abbr> texts inside <b>; category = remaining text (minus <sup>)
                const areacat = tr.querySelector('td.areacat');
                let area = null, category = null;
                if (areacat) {
                    const abbrs = Array.from(areacat.querySelectorAll('abbr')).map(a => a.textContent.trim());
                    area = abbrs.join(' - ') || null;
                    const clone = areacat.cloneNode(true);
                    clone.querySelectorAll('b, sup').forEach(el => el.remove());
                    category = clone.textContent.replace(/\\s+/g, ' ').trim() || null;
                }

                const timeEl = tr.querySelector('td.posteddate time');
                const postedIso = timeEl ? timeEl.getAttribute('datetime') : null;

                const autorepost = tr.querySelector('td.autorepost')?.textContent.trim() || null;
                const expiresText = tr.querySelector('td.expdate')?.textContent.trim() || null;

                // Sibling metrics row
                let impressions = null, views = null, shares = null, favorites = null;
                let freshness_note = null;
                const metricsRow = tr.nextElementSibling;
                if (metricsRow && metricsRow.classList.contains('account-post-metrics-row')) {
                    const noMetrics = metricsRow.querySelector('.account-post-no-metrics');
                    if (noMetrics) {
                        freshness_note = noMetrics.textContent.trim();
                    } else {
                        const grab = (t) => {
                            const el = metricsRow.querySelector(`div[title="${t}"] .account-post-metric-value`);
                            return el ? el.textContent.trim() : null;
                        };
                        impressions = grab('impressions');
                        views = grab('views');
                        shares = grab('shares');
                        favorites = grab('favorites');
                        const fresh = metricsRow.querySelector('.account-metric-valid-thru');
                        if (fresh) freshness_note = fresh.textContent.trim();
                    }
                }

                out.push({
                    post_id: String(postId).replace(/\\D/g, ''),
                    status, title, url, area, category,
                    posted_ts_iso: postedIso,
                    autorepost, expires_raw: expiresText,
                    impressions, views, shares, favorites,
                    freshness_note,
                });
            }
            return out;
        }
        """
    )

    parsed: list[dict] = []
    for r in rows_data:
        post_id = r.get("post_id") or ""
        if not post_id:
            continue
        # posted_ts_iso from CL is like "2026-07-01T10:01:32-0700"
        posted_ts = None
        raw = r.get("posted_ts_iso")
        if raw:
            try:
                posted_ts = datetime.fromisoformat(raw).astimezone(timezone.utc).isoformat()
            except Exception:
                posted_ts = raw
        parsed.append({
            "post_id": post_id,
            "status": r.get("status") or None,
            "title": r.get("title"),
            "url": r.get("url"),
            "area": r.get("area"),
            "category": r.get("category"),
            "posted_ts": posted_ts,
            "autorepost": r.get("autorepost"),
            "expires_in_days": _parse_expires_days(r.get("expires_raw")),
            "impressions": _to_int(r.get("impressions")),
            "views": _to_int(r.get("views")),
            "shares": _to_int(r.get("shares")),
            "favorites": _to_int(r.get("favorites")),
            "freshness_note": r.get("freshness_note"),
        })
    return parsed


def _has_next_page(page: Page) -> bool:
    """Return True if there's a 'next page' link visible."""
    try:
        # CL uses 'next' text links for pagination
        nxt = page.get_by_role("link", name=re.compile(r"^next", re.I))
        return nxt.count() > 0 and nxt.first.is_visible()
    except Exception:
        return False


def _click_next_page(page: Page) -> None:
    nxt = page.get_by_role("link", name=re.compile(r"^next", re.I)).first
    nxt.click()
    sleep_jitter(2.0)


def _scrape_account(account: Account, headless: bool) -> list[dict]:
    """Return all active-tab rows for one account. Raises LoginExpiredError."""
    all_rows: list[dict] = []
    with launch_account(account, headless=headless, flow="stats_sync") as ctx:
        page = ctx.new_page()
        _ensure_active_tab(page, account.name)
        read_pause(400)
        page_num = 1
        while True:
            rows = _scrape_current_page(page, account.name)
            logger.info(f"[{account.name}] page {page_num}: {len(rows)} rows")
            if not rows and page_num == 1:
                # Empty first page could be legit (no active posts) or a DOM change.
                # Dump for inspection but don't raise — treat as zero rows.
                _dump_page(page, account.name, "empty_page1")
            all_rows.extend(rows)
            if not _has_next_page(page):
                break
            _click_next_page(page)
            page_num += 1
            if page_num > 20:
                logger.warning(f"[{account.name}] pagination safety cap hit (20 pages)")
                break
    return all_rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _upsert_snapshots(conn: sqlite3.Connection, account: Account, rows: list[dict], snapshot_date: str) -> None:
    ts_utc = _now_utc_iso()
    for r in rows:
        # Ensure dimension row exists
        conn.execute(
            """
            INSERT INTO posts(post_id, account, title, url, posted_ts, source)
            VALUES(?, ?, ?, ?, ?, 'stats_sync')
            ON CONFLICT(post_id) DO UPDATE SET
                title = COALESCE(excluded.title, posts.title),
                url = COALESCE(excluded.url, posts.url),
                posted_ts = COALESCE(posts.posted_ts, excluded.posted_ts)
            """,
            (r["post_id"], account.name, r["title"], r["url"], r["posted_ts"]),
        )
        conn.execute(
            """
            INSERT INTO snapshots(
                post_id, snapshot_date, snapshot_ts_utc, status,
                impressions, views, shares, favorites,
                area, category, expires_in_days, autorepost, freshness_note
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id, snapshot_date) DO UPDATE SET
                snapshot_ts_utc = excluded.snapshot_ts_utc,
                status = excluded.status,
                impressions = excluded.impressions,
                views = excluded.views,
                shares = excluded.shares,
                favorites = excluded.favorites,
                area = excluded.area,
                category = excluded.category,
                expires_in_days = excluded.expires_in_days,
                autorepost = excluded.autorepost,
                freshness_note = excluded.freshness_note
            """,
            (
                r["post_id"], snapshot_date, ts_utc, r["status"],
                r["impressions"], r["views"], r["shares"], r["favorites"],
                r["area"], r["category"], r["expires_in_days"], r["autorepost"], r["freshness_note"],
            ),
        )


def _freeze_missing_posts(
    conn: sqlite3.Connection, account: Account, seen_ids: set[str], snapshot_date: str
) -> list[dict]:
    """Posts that were active yesterday but not today → mark gone_from_active.

    Returns the frozen rows in the same shape `_scrape_current_page` produces,
    so the caller can ship them to the dashboard alongside the live ones. They
    used to be written to local sqlite and counted, and that was all: the only
    record that a post had ended lived on this machine, and the VPS went on
    showing its last 'Active' snapshot indefinitely. An ad that expired weeks
    ago is not distinguishable from one still earning unless this leaves here.
    """
    # Yesterday's active posts for this account
    yesterday = (date.fromisoformat(snapshot_date) - timedelta(days=1)).isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT s.post_id
        FROM snapshots s
        JOIN posts p ON p.post_id = s.post_id
        WHERE p.account = ?
          AND s.snapshot_date = ?
          AND (s.status IS NULL OR s.status NOT LIKE 'gone_from_active%')
        """,
        (account.name, yesterday),
    ).fetchall()

    ts_utc = _now_utc_iso()
    frozen: list[dict] = []
    for row in rows:
        pid = row["post_id"]
        if pid in seen_ids:
            continue
        # Carry forward the last known counters and mark gone.
        last = conn.execute(
            """
            SELECT s.impressions, s.views, s.shares, s.favorites, s.area,
                   s.category, s.autorepost, s.freshness_note,
                   p.title, p.url, p.posted_ts
            FROM snapshots s
            JOIN posts p ON p.post_id = s.post_id
            WHERE s.post_id = ?
            ORDER BY s.snapshot_date DESC LIMIT 1
            """,
            (pid,),
        ).fetchone()
        if not last:
            continue
        cur = conn.execute(
            """
            INSERT INTO snapshots(
                post_id, snapshot_date, snapshot_ts_utc, status,
                impressions, views, shares, favorites,
                area, category, expires_in_days, autorepost, freshness_note
            ) VALUES(?, ?, ?, 'gone_from_active', ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(post_id, snapshot_date) DO NOTHING
            """,
            (
                pid, snapshot_date, ts_utc,
                last["impressions"], last["views"], last["shares"], last["favorites"],
                last["area"], last["category"], last["autorepost"], last["freshness_note"],
            ),
        )
        # Only report what this run actually froze. A second sync on the same
        # day hits the ON CONFLICT and must stay silent — emitting regardless
        # would file a fresh event_id for an ending already reported, and the
        # outbox would carry one duplicate per re-run.
        if not cur.rowcount:
            continue
        frozen.append({
            "post_id": pid,
            "status": "gone_from_active",
            "title": last["title"],
            "url": last["url"],
            "posted_ts": last["posted_ts"],
            "area": last["area"],
            "category": last["category"],
            "autorepost": last["autorepost"],
            "expires_in_days": None,
            "impressions": last["impressions"],
            "views": last["views"],
            "shares": last["shares"],
            "favorites": last["favorites"],
            "freshness_note": last["freshness_note"],
        })
    return frozen


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_all(headless: bool = False, only_account: str | None = None) -> dict:
    """
    Scrape active postings for every account and write a snapshot for today (ET).
    Returns a summary dict. Per-account atomicity: each account is its own tx.
    """
    snapshot_date = _today_et()
    summary = {"snapshot_date": snapshot_date, "accounts": {}}

    for account in ACCOUNTS:
        if only_account and account.name != only_account:
            continue
        logger.info(f"[{account.name}] stats-sync starting")
        try:
            rows = _scrape_account(account, headless=headless)
        except LoginExpiredError as e:
            logger.error(f"[{account.name}] login expired: {e}")
            _record_health(account.name, ok=False, error_type="login_expired", message=str(e))
            summary["accounts"][account.name] = {"ok": False, "error": "login_expired"}
            continue
        except Exception as e:
            logger.exception(f"[{account.name}] scrape failed: {e}")
            _record_health(account.name, ok=False, error_type="scrape_error", message=repr(e))
            summary["accounts"][account.name] = {"ok": False, "error": "scrape_error"}
            continue

        # Commit this account inside a single transaction
        conn = _connect()
        try:
            with conn:
                _upsert_snapshots(conn, account, rows, snapshot_date)
                seen = {r["post_id"] for r in rows}
                frozen = _freeze_missing_posts(conn, account, seen, snapshot_date)
            _record_health(account.name, ok=True)
            summary["accounts"][account.name] = {
                "ok": True, "rows": len(rows), "frozen": len(frozen),
            }
            logger.info(
                f"[{account.name}] committed {len(rows)} rows, froze {len(frozen)}"
            )
            # Frozen rows ship too — an ended post is a result, not an absence.
            _emit_snapshot_events(account, rows + frozen, snapshot_date)
        except Exception as e:
            logger.exception(f"[{account.name}] DB write failed: {e}")
            _record_health(account.name, ok=False, error_type="db_error", message=repr(e))
            summary["accounts"][account.name] = {"ok": False, "error": "db_error"}
        finally:
            conn.close()

    return summary


def _emit_snapshot_events(account: Account, rows: list[dict], snapshot_date: str) -> None:
    """Push one SnapshotTaken event per row to the reporter outbox. Best-effort."""
    try:
        from datetime import datetime as _dt, timezone as _tz

        from . import reporter
        from .events import SnapshotTaken
    except Exception:
        return

    now_utc = _dt.now(_tz.utc)
    for r in rows:
        try:
            posted_ts_val = None
            if r.get("posted_ts"):
                try:
                    posted_ts_val = _dt.fromisoformat(r["posted_ts"])
                except Exception:
                    posted_ts_val = None
            reporter.emit(SnapshotTaken(
                ts=now_utc,
                snapshot_date=snapshot_date,
                post_id=r["post_id"],
                account=account.name,
                title=r.get("title"),
                url=r.get("url"),
                posted_ts=posted_ts_val,
                status=r.get("status"),
                impressions=r.get("impressions"),
                views=r.get("views"),
                shares=r.get("shares"),
                favorites=r.get("favorites"),
                area=r.get("area"),
                category=r.get("category"),
                expires_in_days=r.get("expires_in_days"),
                autorepost=r.get("autorepost"),
                freshness_note=r.get("freshness_note"),
            ))
        except Exception as e:  # never let telemetry crash the sync
            logger.warning(f"snapshot emit failed for post {r.get('post_id')}: {e}")


# ---------------------------------------------------------------------------
# Seed from state.json
# ---------------------------------------------------------------------------

_POST_ID_PATTERNS = [
    re.compile(r"/(\d{8,})\.html"),           # /d/slug/1234567890.html
    re.compile(r"postingID=(\d{8,})", re.I),  # search?postingID=1234567890 (paid-category receipt fallback)
    re.compile(r"[?&]id=(\d{8,})", re.I),     # generic id=... form
    # Craigslist's current share form: /view/d/<slug>/<token>, where the token
    # is base62 rather than digits. Every pattern above wanted 8+ digits, so a
    # real published post yielded no id at all — it went into history under a
    # placeholder and could not be ghost-checked or tracked for stats. The
    # 16-char floor keeps this from matching the numeric form above or a slug.
    re.compile(r"/(?:view/)?d/[^/]+/([A-Za-z0-9_-]{16,})"),
]


def _extract_post_id(url: str) -> str | None:
    for pat in _POST_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


# Public alias — used by the reporter to attach post_id to events.
def extract_post_id(url: str | None) -> str | None:
    if not url:
        return None
    return _extract_post_id(url)


def seed_from_state_json() -> dict:
    """Import historical posts from logs/state.json into the posts table."""
    from .config import STATE_FILE
    if not STATE_FILE.exists():
        return {"imported": 0, "skipped": 0, "reason": "state.json not found"}

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    posts = state.get("posts", [])
    imported = 0
    skipped = 0
    skipped_examples: list[str] = []
    conn = _connect()
    try:
        with conn:
            for p in posts:
                url = p.get("url") or ""
                post_id = _extract_post_id(url)
                if not post_id:
                    skipped += 1
                    if len(skipped_examples) < 3:
                        skipped_examples.append(url or "(no url)")
                    continue
                # Convert 'at' ISO to UTC ISO (already UTC per accounts.record_post)
                posted_ts = p.get("at")
                cur = conn.execute(
                    """
                    INSERT INTO posts(post_id, account, title, url, posted_ts, source)
                    VALUES(?, ?, ?, ?, ?, 'state_json_seed')
                    ON CONFLICT(post_id) DO NOTHING
                    """,
                    (post_id, p.get("account"), p.get("title"), url, posted_ts),
                )
                if cur.rowcount:
                    imported += 1
                else:
                    skipped += 1
    finally:
        conn.close()
    return {"imported": imported, "skipped": skipped, "skipped_examples": skipped_examples}


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def rollup(period: str, account: str | None = None, since: str | None = None) -> list[dict]:
    """
    period ∈ {'day','week','month'}. Returns per-post rows with delta counters
    over the given period. 'day' compares to previous day; 'week' to 7d earlier;
    'month' to 30d earlier.
    """
    if period not in ("day", "week", "month"):
        raise ValueError(f"period must be day/week/month, got {period}")
    days = {"day": 1, "week": 7, "month": 30}[period]

    where = ["p.post_id = s.post_id"]
    params: list = []
    if account:
        where.append("p.account = ?")
        params.append(account)
    if since:
        where.append("s.snapshot_date >= ?")
        params.append(since)
    where_sql = " AND ".join(where)

    with read_conn() as conn:
        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT s.post_id, MAX(s.snapshot_date) AS d
                FROM snapshots s, posts p
                WHERE {where_sql}
                GROUP BY s.post_id
            )
            SELECT
                p.account, p.post_id, p.title, p.url,
                s_now.snapshot_date AS as_of,
                s_now.status, s_now.impressions, s_now.views, s_now.shares, s_now.favorites,
                s_now.freshness_note,
                COALESCE(s_now.impressions, 0) - COALESCE(s_prev.impressions, 0) AS d_impressions,
                COALESCE(s_now.views, 0) - COALESCE(s_prev.views, 0) AS d_views,
                COALESCE(s_now.favorites, 0) - COALESCE(s_prev.favorites, 0) AS d_favorites
            FROM latest l
            JOIN snapshots s_now ON s_now.post_id = l.post_id AND s_now.snapshot_date = l.d
            LEFT JOIN snapshots s_prev
                   ON s_prev.post_id = l.post_id
                  AND s_prev.snapshot_date = date(l.d, ?)
            JOIN posts p ON p.post_id = l.post_id
            ORDER BY p.account, d_impressions DESC
            """,
            (*params, f"-{days} day"),
        ).fetchall()
        return [dict(r) for r in rows]


def history_for_post(post_id: str) -> list[dict]:
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date, status, impressions, views, shares, favorites, freshness_note
            FROM snapshots WHERE post_id = ?
            ORDER BY snapshot_date
            """,
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def export_xlsx(path: Path, since: str | None = None) -> int:
    """Dump the full snapshots × posts join to an xlsx workbook. Returns row count."""
    from openpyxl import Workbook

    with read_conn() as conn:
        q = """
            SELECT p.account, p.post_id, p.title, p.url, p.posted_ts,
                   s.snapshot_date, s.snapshot_ts_utc, s.status,
                   s.impressions, s.views, s.shares, s.favorites,
                   s.area, s.category, s.expires_in_days, s.autorepost, s.freshness_note
            FROM snapshots s JOIN posts p ON p.post_id = s.post_id
        """
        params: tuple = ()
        if since:
            q += " WHERE s.snapshot_date >= ?"
            params = (since,)
        q += " ORDER BY p.account, p.post_id, s.snapshot_date"
        rows = conn.execute(q, params).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "snapshots"
    if rows:
        headers = list(rows[0].keys())
        ws.append(headers)
        for r in rows:
            ws.append([r[h] for h in headers])
    else:
        ws.append(["(no data)"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(rows)


# ---------------------------------------------------------------------------
# Recovering ended postings
#
# When a posting ends, Craigslist stops serving its public URL and stops
# offering an edit form, so hydration — the only route to a body — is gone with
# it. What survives is the account page's own list of inactive postings, which
# is the last place the ad is described at all.
#
# This walks that list and reports it. Content and images need the per-posting
# manage page, whose DOM nothing here has seen; a run captures it so the
# selectors can be written against something real rather than guessed at, which
# is how every other page in this project has been settled.
# ---------------------------------------------------------------------------

# Which filters to walk. 'deleted' is included because Craigslist files a
# manually removed posting there rather than under inactive, and those are ads
# that ran and earned just like the rest.
ENDED_TABS = ("inactive", "deleted")

# The only control on a posting row this may ever click.
#
# Observed on the account page, each action is its own form:
#
#   <form action="https://post.craigslist.org/manage/<token>" method="GET"
#         class="manage display">
#     <input type="hidden" name="action" value="display">
#     <input type="submit" name="go" value="display" class="managebtn">
#   </form>
#
# `display` reads. The neighbouring forms in the same cell renew a dead ad,
# delete a live one, or open the editor, and they are one DOM reordering or one
# sloppy selector away from being the thing that gets clicked. Reading an ended
# posting must never be able to put it back on the market — a reposted ad costs
# money, burns the daily cap, and is indistinguishable from the operator having
# asked for it.
SAFE_ROW_ACTION = "display"

# Named so a mistake is loud rather than merely wrong.
FORBIDDEN_ROW_ACTIONS = ("repost", "renew", "delete", "edit")

# The manage/display page, OBSERVED (artifact 7467c7b2, 2026-08-02). Title
# "south florida | manage posting", and it is the whole ad: the copy, the
# gallery, the post id. This is the only surviving description of an ended
# posting anywhere.
ENDED_SEL = {
    "title": "#titletextonly",
    "body": "#postingbody",
    # The page also carries OpenStreetMap tiles from map*.craigslist.org, which
    # are not the ad's pictures. Scoping to the image host keeps them out.
    "images": "img[src*='images.craigslist.org']",
    "infos": "p.postinginfo",
}

# Craigslist encodes the size in the filename: `_50x50c`, `_300x300`, `_600x450`,
# `_1200x900`. The manage/display page renders the gallery strip at 50x50, and
# the trailing `c` on that one variant — it is a centre crop — is why the
# earlier `_\d+x\d+\.jpg$` pattern skipped precisely the size that most needed
# upgrading. 80 of the first 110 pictures we kept were 50-pixel thumbnails.
_CL_VARIANT = re.compile(r"_\d+x\d+c?\.jpg$", re.I)
# The largest Craigslist keeps. Asking for it costs one request either way.
LARGEST_VARIANT = "_1200x900.jpg"


def largest_variant(url: str) -> str:
    """Rewrite a Craigslist image URL to the full-size original.

    Leaves anything it does not recognise alone — a working URL beats a clever
    one, and a 404 here means the only surviving copy of that picture is lost.
    """
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    return _CL_VARIANT.sub(LARGEST_VARIANT, url)


def scan_ended(
    *,
    headless: bool = False,
    only_account: str | None = None,
    capture: bool = True,
    max_recover: int = 5,
) -> dict:
    """Read every ended posting off the account page and report it.

    Returns a summary per account. Emits a `snapshot_taken` for each row, which
    is what carries it to the dashboard: ingest creates the `posts` row from the
    title and URL and files the status as a snapshot, so an ended ad stops being
    a bare id.
    """
    from . import artifacts as artifacts_mod
    from .config import ACCOUNTS
    from .poster import launch_account

    summary: dict = {"accounts": {}}
    for account in ACCOUNTS:
        if only_account and account.name != only_account:
            continue
        found: list[dict] = []
        info: dict = {"ok": False, "rows": 0, "tabs": {}}
        try:
            with launch_account(account, headless=headless, flow="scan_ended") as ctx:
                page = ctx.new_page()
                for tab in ENDED_TABS:
                    _ensure_tab(page, account.name, tab)
                    rows = _scrape_current_page(page, account.name)
                    pages = 1
                    while _has_next_page(page) and pages < 20:
                        _click_next_page(page)
                        rows += _scrape_current_page(page, account.name)
                        pages += 1
                    for r in rows:
                        r["ended_tab"] = tab
                    info["tabs"][tab] = len(rows)
                    found.extend(rows)
                    logger.info(
                        f"[{account.name}] {tab}: {len(rows)} posting(s) over {pages} page(s)"
                    )
                    if capture and rows:
                        artifacts_mod.capture_page(
                            page, flow="scan_ended", label=f"list_{tab}",
                            account=account.name,
                        )
                # The listing gives a title and a date; the copy and the
                # pictures are one click further in, on the manage/display page.
                if max_recover:
                    for row in found[:max_recover]:
                        _capture_ended_post(
                            page, account.name, row, capture=capture
                        )
            info["ok"] = True
        except LoginExpiredError as e:
            info["error"] = f"login_expired: {e}"
            logger.error(f"[{account.name}] {e}")
        except Exception as e:  # pragma: no cover - defensive
            info["error"] = f"{type(e).__name__}: {e}"
            logger.exception(f"[{account.name}] scan-ended failed")

        info["rows"] = len(found)
        summary["accounts"][account.name] = info
        if found:
            _emit_ended_events(account, found)

    return summary


def _capture_ended_post(
    page: Page, account_name: str, row: dict, *, capture: bool = True
) -> None:
    """Open one ended posting's own page and capture it.

    Reconnaissance, not extraction. Which control an ended posting still offers
    — display, repost, or nothing — decides how its body and images can be read,
    and that is not worth guessing at.
    """
    from . import artifacts as artifacts_mod

    post_id = row.get("post_id")
    try:
        target = page.locator(
            f"tr.posting-row:has(td.status[data-postingid='{post_id}'])"
        )
        if target.count() == 0:
            return
        buttons = target.first.locator("td.buttons input[type='submit']")
        labels = [
            buttons.nth(i).get_attribute("value") for i in range(buttons.count())
        ]
        logger.info(f"[{account_name}] ended post {post_id} offers: {labels}")

        # Scoped to the display form specifically, not to a value anywhere in
        # the cell, so a matching value on a different form cannot be picked up.
        show = target.first.locator(
            f"td.buttons form.manage.{SAFE_ROW_ACTION} "
            f"input[type='submit'][value='{SAFE_ROW_ACTION}']"
        )
        if show.count() == 0:
            logger.info(
                f"[{account_name}] ended post {post_id} offers no "
                f"{SAFE_ROW_ACTION!r} control — leaving it alone"
            )
            return

        # Read back what is about to be clicked and refuse anything that is not
        # exactly the read-only action. Belt and braces on purpose: the cost of
        # being wrong here is a live ad nobody asked for.
        value = (show.first.get_attribute("value") or "").strip().lower()
        if value != SAFE_ROW_ACTION:
            logger.error(
                f"[{account_name}] refusing to click {value!r} on {post_id}; "
                f"only {SAFE_ROW_ACTION!r} is permitted"
            )
            return
        form_action = (
            show.first.evaluate(
                "el => (el.form && el.form.querySelector(\"input[name='action']\")"
                "  || {}).value || ''"
            )
            or ""
        ).strip().lower()
        if form_action and form_action != SAFE_ROW_ACTION:
            logger.error(
                f"[{account_name}] the {SAFE_ROW_ACTION!r} button on {post_id} sits "
                f"in a form whose action is {form_action!r} — not clicking it"
            )
            return
        show.first.click()
        page.wait_for_load_state("domcontentloaded")
        sleep_jitter(1.5)

        content = _read_ended_post(page)
        if capture:
            artifacts_mod.capture_page(
                page, flow="scan_ended", label="ended_post_display",
                post_id=post_id, account=account_name,
            )
        body_len = len(content.get("body") or "")
        logger.info(
            f"[{account_name}] recovered {post_id}: {body_len} body chars, "
            f"{len(content['images'])} image(s)"
        )
        if body_len:
            _emit_ended_content(account_name, post_id, content)
        row["recovered"] = bool(body_len)

        page.go_back(wait_until="domcontentloaded")
        sleep_jitter(1.0)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[{account_name}] could not read ended post {post_id}: {e}")


def _emit_ended_content(account_name: str, post_id: str, content: dict) -> None:
    """Report what an ended posting said, through the ordinary content path.

    A `post_content` event is exactly right: ingest already writes the copy and
    the image manifest onto the posting from one, and refuses an older read over
    a newer one. `editable` is false because Craigslist offers no edit form for
    an ended ad — the dashboard uses that to show the copy without offering to
    change it.
    """
    from . import reporter as reporter_mod
    from .events import PostContent, PostImage

    try:
        reporter_mod.emit(PostContent(
            ts=datetime.now(timezone.utc),
            machine=machine_name(),
            account=account_name,
            post_id=post_id,
            ok=True,
            title=content.get("title"),
            body=content.get("body"),
            images=[PostImage(**i) for i in content.get("images", [])],
            live_status="ended",
            editable=False,
        ))
    except Exception as e:
        logger.warning(f"could not report recovered content for {post_id}: {e}")


def _read_ended_post(page: Page) -> dict:
    """Scrape an ended posting's own page. Read-only.

    Craigslist keeps serving `manage/<token>?action=display` after a posting
    ends, and that page is the complete ad. Nothing else is: the public URL is
    gone, the edit form is gone, and the account row carries only a title.
    """
    def _text(key: str) -> str:
        loc = page.locator(ENDED_SEL[key])
        if loc.count() == 0:
            return ""
        try:
            return (loc.first.inner_text() or "").strip()
        except Exception:
            return ""

    images: list[dict] = []
    gallery = page.locator(ENDED_SEL["images"])
    for i in range(gallery.count()):
        src = gallery.nth(i).get_attribute("src")
        if src:
            # Craigslist names the variant in the filename, and the same id at
            # _1200x900 is the largest it keeps, so ask for that whatever the
            # page happened to render.
            images.append({
                "slot": i + 1,
                "url": largest_variant(src),
                "sha256": None,
            })

    post_id = None
    infos = page.locator(ENDED_SEL["infos"])
    for i in range(infos.count()):
        try:
            txt = (infos.nth(i).inner_text() or "").strip()
        except Exception:
            continue
        m = re.search(r"post id:\s*(\d+)", txt, re.I)
        if m:
            post_id = m.group(1)
            break

    return {
        "title": _text("title") or None,
        "body": _text("body") or None,
        "images": images,
        "craigslist_post_id": post_id,
    }


def _emit_ended_events(account: Account, rows: list[dict]) -> None:
    """Report ended postings so the dashboard stops showing bare ids.

    Reuses `snapshot_taken` rather than inventing an event: ingest already
    creates the `posts` row from a snapshot's title and URL, and files the
    status, which is exactly what is wanted. The snapshot date is the day the
    posting was last seen, not today, so an ad that ended in June does not
    appear to have been alive this morning.
    """
    from . import reporter as reporter_mod
    from .events import SnapshotTaken

    for r in rows:
        if not r.get("post_id"):
            continue
        when = r.get("posted_date") or _today_et()
        try:
            reporter_mod.emit(SnapshotTaken(
                ts=datetime.now(timezone.utc),
                account=account.name,
                post_id=r["post_id"],
                snapshot_date=when,
                title=r.get("title"),
                url=r.get("url"),
                status=r.get("status") or r.get("ended_tab"),
                area=r.get("area"),
                category=r.get("category"),
                impressions=r.get("impressions"),
                views=r.get("views"),
                shares=r.get("shares"),
                favorites=r.get("favorites"),
            ))
        except Exception as e:
            logger.warning(f"could not report ended post {r.get('post_id')}: {e}")
