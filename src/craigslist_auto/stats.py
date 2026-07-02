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

from .config import ACCOUNTS, DATA_DIR, LOGS_DIR, Account
from .human import read_pause, sleep_jitter
from .poster import launch_account

STATS_DB = DATA_DIR / "stats.sqlite"
STATS_HEALTH = DATA_DIR / "stats_health.json"
FAILURES_DIR = LOGS_DIR / "failures"

# Same URL poster.is_logged_in uses — this is the postings page when signed in.
CL_ACCOUNT_URL = "https://accounts.craigslist.org/login/home"
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
    """Navigate to the postings page and submit the 'active' filter."""
    page.goto(CL_ACCOUNT_URL, wait_until="domcontentloaded")
    sleep_jitter(2.0)
    logger.debug(f"[{account_name}] landed on {page.url}")

    # Login detection: real login page shows a password input and no postings
    # tab bar; logged-in page shows the postings table + filter buttons.
    login_form_visible = page.locator("input[type='password']").count() > 0
    filters_visible = page.locator("button[name='filter_active']").count() > 0
    if login_form_visible and not filters_visible:
        raise LoginExpiredError(f"account {account_name} is not logged in (url={page.url})")

    # "active" is a form-submit button, not a link
    try:
        btn = page.locator("button[name='filter_active'][value='active']")
        if btn.count() > 0:
            btn.first.click()
            page.wait_for_load_state("domcontentloaded")
            sleep_jitter(1.5)
    except Exception as e:
        logger.debug(f"[{account_name}] could not submit 'active' filter: {e}")

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
    with launch_account(account, headless=headless) as ctx:
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


def _freeze_missing_posts(conn: sqlite3.Connection, account: Account, seen_ids: set[str], snapshot_date: str) -> int:
    """Posts that were active yesterday but not today → mark gone_from_active."""
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
    frozen = 0
    for row in rows:
        pid = row["post_id"]
        if pid in seen_ids:
            continue
        # Carry forward the last known counters and mark gone.
        last = conn.execute(
            """
            SELECT impressions, views, shares, favorites, area, category, autorepost, freshness_note
            FROM snapshots
            WHERE post_id = ?
            ORDER BY snapshot_date DESC LIMIT 1
            """,
            (pid,),
        ).fetchone()
        if not last:
            continue
        conn.execute(
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
        frozen += 1
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
                "ok": True, "rows": len(rows), "frozen": frozen,
            }
            logger.info(f"[{account.name}] committed {len(rows)} rows, froze {frozen}")
        except Exception as e:
            logger.exception(f"[{account.name}] DB write failed: {e}")
            _record_health(account.name, ok=False, error_type="db_error", message=repr(e))
            summary["accounts"][account.name] = {"ok": False, "error": "db_error"}
        finally:
            conn.close()

    return summary


# ---------------------------------------------------------------------------
# Seed from state.json
# ---------------------------------------------------------------------------

_POST_ID_PATTERNS = [
    re.compile(r"/(\d{8,})\.html"),           # /d/slug/1234567890.html
    re.compile(r"postingID=(\d{8,})", re.I),  # search?postingID=1234567890 (paid-category receipt fallback)
    re.compile(r"[?&]id=(\d{8,})", re.I),     # generic id=... form
]


def _extract_post_id(url: str) -> str | None:
    for pat in _POST_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


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
