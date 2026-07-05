from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from loguru import logger

# Load .env (INSTANTPROXIES_USER/PASS, etc.) before any module reads os.environ.
load_dotenv()

from .accounts import eligibility_report, pick_next_account, record_post
from .config import ACCOUNTS_BY_NAME, EXCEL_PATH, LOGS_DIR
from .content import generate_ad
from .ghost_check import check_all_recent
from .poster import launch_account, post_ad
from . import stats as stats_mod
from . import reporter as reporter_mod
from .events import PostAttempt
from .stats import LoginExpiredError, extract_post_id

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _setup_logging(verbose: bool = False):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")
    logger.add(LOGS_DIR / "run.log", level="DEBUG", rotation="5 MB")


def _machine_name() -> str:
    """
    Identify this physical machine. Override with CL_MACHINE env var.
    The script will only run accounts whose `allowed_machine` matches this.
    """
    return os.environ.get("CL_MACHINE") or platform.node().lower()


@app.command("init-data")
def init_data():
    """Create a sample ads.xlsx with the expected schema."""
    from openpyxl import Workbook

    if EXCEL_PATH.exists():
        typer.echo(f"Already exists: {EXCEL_PATH}")
        raise typer.Exit(0)
    EXCEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "ads"
    ws.append([
        "county", "city", "service_offered", "posting_title",
        "zip_code", "description", "license_number", "phone_number", "photos_count",
    ])
    ws.append([
        "Broward",
        "Hollywood",
        "Roofing",
        "{Affordable|Top-rated|Licensed} {Metal Roofing|Roof Replacement} in {city}",
        "33020",
        (
            "Hi {neighbors|homeowners}, we are a {family-owned|local} {roofing|metal roofing} "
            "company serving {city} and nearby areas.\n\n"
            "We {offer|provide}: free inspections, {financing|payment plans}, and "
            "{lifetime|long-term} warranties.\n\n"
            "Call or text {phone} today for a free estimate. Zip {zip_code}. "
            "License #{license}."
        ),
        "CCC1334317",
        "(954) 634-7370",
        4,
    ])
    ws.append([
        "Broward",
        "Fort Lauderdale",
        "Roofing",
        "{Storm|Hurricane} Damage Roof {Repair|Inspection} - {Free Estimate|No Obligation}",
        "33301",
        (
            "{After the recent storms|Following hurricane season}, we are {offering|providing} "
            "free roof inspections in {city}. "
            "{Insurance claims welcome|We work with all major insurers}.\n\n"
            "Licensed & insured (#{license}). Call {phone}. Serving {zip_code} and surrounding zips."
        ),
        "CCC1334317",
        "(954) 634-7420",
        5,
    ])
    wb.save(EXCEL_PATH)
    typer.echo(f"Created sample: {EXCEL_PATH}")
    typer.echo("Add real rows, then put unique photos in data/photos/craigs1, craigs2, craigs3.")


@app.command("init-account")
def init_account(account_name: str):
    """
    Open a browser for this account so you can log in manually ONE TIME.
    The session persists in the profile directory and is reused on every post.
    """
    _setup_logging()
    if account_name not in ACCOUNTS_BY_NAME:
        typer.echo(f"Unknown account: {account_name}")
        raise typer.Exit(1)
    account = ACCOUNTS_BY_NAME[account_name]
    typer.echo(f"Opening Chrome with profile: {account.profile_dir}")
    typer.echo("→ Log into Craigslist with this account.")
    typer.echo("→ Close the browser window when done.")
    with launch_account(account, headless=False) as ctx:
        page = ctx.new_page()
        page.goto("https://accounts.craigslist.org/login")
        # Wait for user to close manually
        page.wait_for_event("close", timeout=0)


@app.command("status")
def status():
    """Show which accounts are eligible to post right now and why not."""
    _setup_logging()
    typer.echo(f"Machine: {_machine_name()}")
    for r in eligibility_report():
        mark = "[OK]" if r["eligible"] else "[--]"
        reasons = "; ".join(r["reasons"]) if r["reasons"] else "ready"
        typer.echo(f"  {mark} {r['account']:10s}  {reasons}")

    health = stats_mod.health_report()
    if health:
        typer.echo("")
        typer.echo("Stats sync health:")
        for name, h in health.items():
            mark = "[OK]" if h.get("ok") else "[!!]"
            when = h.get("last_run_ts_utc", "?")
            if h.get("ok"):
                typer.echo(f"  {mark} {name:10s}  last run {when}")
            else:
                err = h.get("error_type") or "unknown"
                msg = h.get("message") or ""
                typer.echo(f"  {mark} {name:10s}  {err}: {msg[:60]}  (last {when})")
                if err == "login_expired":
                    typer.echo(f"       → run:  uv run cl init-account {name}")


@app.command("post")
def post(
    account: str = typer.Option(None, help="Force a specific account (skips rotation)"),
    dry_run: bool = typer.Option(False, help="Walk the form but don't publish"),
    headless: bool = typer.Option(False, help="Run browser headless (NOT recommended)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG-level console logging"),
):
    """Post one ad now, picking the next eligible account for this machine."""
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    _setup_logging(verbose=verbose)
    machine = _machine_name()
    if account:
        acct = ACCOUNTS_BY_NAME.get(account)
        if not acct:
            typer.echo(f"Unknown account: {account}")
            raise typer.Exit(1)
        if acct.allowed_machine != machine and not dry_run:
            typer.echo(
                f"REFUSING: account {account} is bound to machine '{acct.allowed_machine}', "
                f"but this machine is '{machine}'. Set CL_MACHINE env var or run on correct machine."
            )
            raise typer.Exit(2)
    else:
        acct = pick_next_account(machine)
        if acct is None:
            typer.echo("No eligible account right now. Run `cl status` to see why.")
            # Emit "skipped_no_eligible" for the dashboard's last-attempt indicator
            reporter_mod.emit(PostAttempt(
                ts=_dt.now(_tz.utc),
                machine=machine,
                account="(none)",
                outcome="skipped_no_eligible",
            ))
            raise typer.Exit(0)

    started = _time.monotonic()
    ad_title: str | None = None
    photos_attached: list[str] = []
    cover_photo: str | None = None
    try:
        ad = generate_ad(acct)
        ad_title = ad.title
        photos_attached = [p.name for p in ad.photos]
        # First photo is the cover when present (see content._select_photos)
        cover_photo = ad.photos[0].name if ad.photos else None
        logger.info(f"Generated ad: {ad.title}  ({len(ad.photos)} photos)")
        url = post_ad(acct, ad, headless=headless, dry_run=dry_run)
    except LoginExpiredError as e:
        _emit_post_failure(machine, acct.name, "failed_login", str(e), started, ad_title, photos_attached, cover_photo)
        typer.echo(f"Login expired for {acct.name}. Run `cl init-account {acct.name}`.")
        raise typer.Exit(3)
    except Exception as e:
        logger.exception("post_ad raised")
        error_type = "failed_form" if "form" in repr(e).lower() else "failed_other"
        _emit_post_failure(machine, acct.name, error_type, repr(e), started, ad_title, photos_attached, cover_photo)
        typer.echo("Post failed — check logs.")
        raise typer.Exit(3)

    duration = _time.monotonic() - started

    if dry_run:
        reporter_mod.emit(PostAttempt(
            ts=_dt.now(_tz.utc),
            machine=machine,
            account=acct.name,
            outcome="dry_run",
            duration_seconds=duration,
            ad_title=ad_title,
            photos_attached=photos_attached,
            cover_photo=cover_photo,
        ))
        typer.echo("Dry run complete.")
        return

    if url:
        record_post(acct, ad.title, url)
        reporter_mod.emit(PostAttempt(
            ts=_dt.now(_tz.utc),
            machine=machine,
            account=acct.name,
            outcome="posted",
            duration_seconds=duration,
            post_id=extract_post_id(url),
            post_url=url,
            ad_title=ad_title,
            photos_attached=photos_attached,
            cover_photo=cover_photo,
        ))
        typer.echo(f"Posted: {url}")
    else:
        _emit_post_failure(machine, acct.name, "failed_form", "post_ad returned no url",
                          started, ad_title, photos_attached, cover_photo)
        typer.echo("Post failed — check logs.")
        raise typer.Exit(3)


def _emit_post_failure(
    machine: str,
    account_name: str,
    error_type: str,
    error_message: str,
    started_monotonic: float,
    ad_title: str | None,
    photos_attached: list[str],
    cover_photo: str | None,
) -> None:
    import time as _time
    from datetime import datetime as _dt, timezone as _tz
    reporter_mod.emit(PostAttempt(
        ts=_dt.now(_tz.utc),
        machine=machine,
        account=account_name,
        outcome=error_type,  # type: ignore[arg-type]
        duration_seconds=_time.monotonic() - started_monotonic,
        ad_title=ad_title,
        photos_attached=photos_attached,
        cover_photo=cover_photo,
        error_type=error_type,
        error_message=error_message[:500],
    ))


@app.command("check-ghosts")
def check_ghosts(
    proxy: str = typer.Option(
        None,
        help="HTTP proxy override. If omitted, uses INSTANTPROXIES_USER/PASS from .env "
        "with the configured GHOST_CHECK_PROXY_HOST. Fail-closed: aborts if neither is set.",
    )
):
    """
    Check whether recent posts are visible in public search.
    Runs through a non-home IP (default: configured InstantProxies NY exit) so
    your own session doesn't see ghosted posts as 'visible'.
    """
    _setup_logging()
    check_all_recent(proxy=proxy)


@app.command("posts")
def posts(
    limit: int = typer.Option(20, help="Most recent N posts to show"),
    account: str = typer.Option(None, help="Filter to one account"),
    only: str = typer.Option(None, help="Filter: visible | ghosted | unchecked"),
):
    """List recent posts with their visibility status."""
    import json
    from datetime import datetime
    from .config import STATE_FILE

    if not STATE_FILE.exists():
        typer.echo("No posts yet.")
        raise typer.Exit(0)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    items = state.get("posts", [])
    if account:
        items = [p for p in items if p["account"] == account]
    if only == "visible":
        items = [p for p in items if p.get("ghosted") is False]
    elif only == "ghosted":
        items = [p for p in items if p.get("ghosted") is True]
    elif only == "unchecked":
        items = [p for p in items if p.get("ghosted") is None]
    items = sorted(items, key=lambda p: p["at"], reverse=True)[:limit]

    if not items:
        typer.echo("No matching posts.")
        raise typer.Exit(0)

    visible = sum(1 for p in items if p.get("ghosted") is False)
    ghosted = sum(1 for p in items if p.get("ghosted") is True)
    unchecked = sum(1 for p in items if p.get("ghosted") is None)
    typer.echo(f"Showing {len(items)} posts  ({visible} visible, {ghosted} ghosted, {unchecked} unchecked)")
    typer.echo("")
    for p in items:
        flag = {True: "[GHOST]", False: "[OK]   ", None: "[?]    "}[p.get("ghosted")]
        when = datetime.fromisoformat(p["at"]).strftime("%Y-%m-%d %H:%M")
        title = (p.get("title") or "")[:50]
        url = p.get("url") or "(no url captured)"
        typer.echo(f"  {flag} {when}  {p['account']:8s}  {title}")
        typer.echo(f"           {url}")
    typer.echo("")
    typer.echo("Tip: run `cl check-ghosts --proxy ...` to refresh ghost status from a different network.")


@app.command("stats-sync")
def stats_sync(
    account: str = typer.Option(None, help="Sync only this account (default: all)"),
    headless: bool = typer.Option(False, help="Run browser headless"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scrape today's stats snapshot for every account's active postings."""
    _setup_logging(verbose=verbose)
    summary = stats_mod.sync_all(headless=headless, only_account=account)
    typer.echo(f"Snapshot date: {summary['snapshot_date']}")
    for name, info in summary["accounts"].items():
        if info.get("ok"):
            typer.echo(f"  [OK] {name:10s}  rows={info['rows']}  frozen={info['frozen']}")
        else:
            typer.echo(f"  [--] {name:10s}  ERROR: {info.get('error')}")


@app.command("stats-seed")
def stats_seed():
    """One-time: import historical posts from logs/state.json into the stats DB."""
    _setup_logging()
    result = stats_mod.seed_from_state_json()
    typer.echo(f"Imported: {result['imported']}   Skipped: {result['skipped']}")
    if result.get("reason"):
        typer.echo(f"  ({result['reason']})")
    for ex in result.get("skipped_examples") or []:
        typer.echo(f"  skipped url: {ex}")


@app.command("stats")
def stats(
    day: bool = typer.Option(False, "--day", help="Show day-over-day deltas (default)"),
    week: bool = typer.Option(False, "--week", help="Show week-over-week deltas"),
    month: bool = typer.Option(False, "--month", help="Show month-over-month deltas"),
    account: str = typer.Option(None, help="Filter to one account"),
    post: str = typer.Option(None, "--post", help="Show full history for one post_id"),
    since: str = typer.Option(None, help="Only include snapshots on/after YYYY-MM-DD"),
    export: Path = typer.Option(None, help="Write full snapshots to this .xlsx path"),
):
    """View per-post stats with deltas, or export the raw data to xlsx."""
    _setup_logging()

    if export:
        n = stats_mod.export_xlsx(export, since=since)
        typer.echo(f"Wrote {n} snapshot rows → {export}")
        return

    if post:
        rows = stats_mod.history_for_post(post)
        if not rows:
            typer.echo(f"No snapshots for post_id={post}")
            return
        typer.echo(f"History for post {post}:")
        typer.echo(f"  {'date':12s}  {'status':18s}  {'impr':>7s}  {'views':>6s}  {'shr':>4s}  {'favs':>5s}")
        for r in rows:
            typer.echo(
                f"  {r['snapshot_date']:12s}  {str(r['status'])[:18]:18s}  "
                f"{str(r['impressions'] if r['impressions'] is not None else '-'):>7s}  "
                f"{str(r['views'] if r['views'] is not None else '-'):>6s}  "
                f"{str(r['shares'] if r['shares'] is not None else '-'):>4s}  "
                f"{str(r['favorites'] if r['favorites'] is not None else '-'):>5s}"
            )
        return

    period = "week" if week else "month" if month else "day"
    rows = stats_mod.rollup(period, account=account, since=since)
    if not rows:
        typer.echo("No data. Run `cl stats-sync` first, or wait for the daily task to fire.")
        return
    label = {"day": "vs yesterday", "week": "vs 7d ago", "month": "vs 30d ago"}[period]
    typer.echo(f"Rollup ({period} — deltas {label}):")
    typer.echo(f"  {'account':8s}  {'post_id':11s}  {'impr':>6s}  {'Δi':>5s}  {'views':>5s}  {'Δv':>4s}  title")
    for r in rows:
        title = (r.get("title") or "")[:50]
        typer.echo(
            f"  {r['account']:8s}  {r['post_id']:11s}  "
            f"{str(r['impressions'] if r['impressions'] is not None else '-'):>6s}  "
            f"{r['d_impressions']:>+5d}  "
            f"{str(r['views'] if r['views'] is not None else '-'):>5s}  "
            f"{r['d_views']:>+4d}  {title}"
        )
        typer.echo(f"           {r['url']}")


@app.command("reporter-daemon")
def reporter_daemon(
    heartbeat_minutes: int = typer.Option(5, help="AccountState heartbeat cadence"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """
    Long-running process: drains the outbox to the VPS and emits AccountState
    heartbeats. Installed by scripts/install-reporter-daemon.ps1 and started
    at boot.
    """
    import signal
    import threading
    from datetime import datetime as _dt, timezone as _tz

    from .accounts import account_snapshot
    from .config import (
        ACCOUNTS, MAX_POSTS_PER_ACCOUNT_PER_WEEK, MAX_POSTS_PER_DAY_TOTAL,
        MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT, POST_WEEKDAYS_ONLY,
        POST_WINDOW_END_HOUR, POST_WINDOW_START_HOUR,
    )
    from .events import AccountState, SchedulerConfig, StatsSyncHealth

    _setup_logging(verbose=verbose)
    machine = _machine_name()

    stop = threading.Event()

    def _handle_signal(signum, frame):  # pragma: no cover
        logger.info(f"reporter-daemon received signal {signum}")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass  # not all signals settable on Windows

    # Emit a SchedulerConfig at startup so the dashboard reflects real guardrails
    try:
        reporter_mod.emit(SchedulerConfig(
            ts=_dt.now(_tz.utc),
            machine=machine,
            posting_cadence=None,       # Task Scheduler cadence isn't introspectable from here
            stats_sync_cadence=None,
            min_hours_between_posts_same_account=MIN_HOURS_BETWEEN_POSTS_SAME_ACCOUNT,
            max_posts_per_day_total=MAX_POSTS_PER_DAY_TOTAL,
            max_posts_per_account_per_week=MAX_POSTS_PER_ACCOUNT_PER_WEEK,
            post_window_start_hour=POST_WINDOW_START_HOUR,
            post_window_end_hour=POST_WINDOW_END_HOUR,
            post_weekdays_only=POST_WEEKDAYS_ONLY,
        ))
    except Exception as e:
        logger.warning(f"scheduler_config emit failed: {e}")

    # Heartbeat runs on its own thread so the flusher can run independently.
    heartbeat_seconds = max(30, heartbeat_minutes * 60)

    def _heartbeat_loop():
        health = stats_mod.health_report()
        while not stop.is_set():
            for account in ACCOUNTS:
                if account.allowed_machine != machine:
                    continue
                try:
                    snap = account_snapshot(account.name)
                    h = health.get(account.name)
                    stats_health = None
                    if h is not None:
                        try:
                            stats_health = StatsSyncHealth(
                                ok=bool(h.get("ok")),
                                last_run_ts=_dt.fromisoformat(h["last_run_ts_utc"]) if h.get("last_run_ts_utc") else None,
                                error_type=h.get("error_type"),
                                error_message=(h.get("message") or None),
                            )
                        except Exception:
                            stats_health = None
                    reporter_mod.emit(AccountState(
                        ts=_dt.now(_tz.utc),
                        machine=machine,
                        account=account.name,
                        eligible_now=snap["eligible_now"],
                        next_eligible_at=snap["next_eligible_at"],
                        block_reasons=snap["block_reasons"],
                        posts_last_24h_total=snap["posts_last_24h_total"],
                        posts_last_7d_this_account=snap["posts_last_7d_this_account"],
                        last_post_at=snap["last_post_at"],
                        last_post_url=snap["last_post_url"],
                        stats_sync_health=stats_health,
                    ))
                except Exception as e:
                    logger.warning(f"heartbeat emit failed for {account.name}: {e}")
            # Reload health each cycle (stats.py rewrites the json)
            health = stats_mod.health_report()
            if stop.wait(heartbeat_seconds):
                return

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="cl-heartbeat", daemon=True)
    heartbeat_thread.start()

    # Flusher runs on the main thread; blocks until stop is set.
    reporter_mod.flush_forever(stop_event=stop)


@app.command("photo-inventory")
def photo_inventory(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Scan photo directories per account and emit PhotoInventory events."""
    from datetime import datetime as _dt, timezone as _tz

    from .config import ACCOUNTS, COVERS_DIR
    from .content import PHOTO_REUSE_LOG, PHOTO_COOLDOWN_DAYS
    from .events import PhotoInventory

    _setup_logging(verbose=verbose)
    now_utc = _dt.now(_tz.utc)

    usage = {}
    if PHOTO_REUSE_LOG.exists():
        import json as _json
        try:
            usage = _json.loads(PHOTO_REUSE_LOG.read_text(encoding="utf-8"))
        except Exception:
            usage = {}

    _IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

    for account in ACCOUNTS:
        # Regular photos
        photo_dir = account.photo_dir
        photos = [p for p in photo_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS] if photo_dir.exists() else []
        never_used = [p for p in photos if str(p) not in usage]
        eligible = [p for p in photos if _photo_is_eligible(p, usage, now_utc, PHOTO_COOLDOWN_DAYS)]

        # Covers claimed for this account (not counting used/ subdir)
        cover_dir = COVERS_DIR / account.name
        covers = [p for p in cover_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS] if cover_dir.exists() else []

        try:
            reporter_mod.emit(PhotoInventory(
                ts=now_utc,
                account=account.name,
                photos_total=len(photos),
                photos_never_used=len(never_used),
                photos_eligible=len(eligible),
                covers_total=len(covers),
                covers_never_used=len(covers),   # covers have no reuse — claimed==unused
                covers_eligible=len(covers),
            ))
        except Exception as e:
            logger.warning(f"photo_inventory emit failed for {account.name}: {e}")

        typer.echo(
            f"  {account.name:10s}  photos {len(never_used)}/{len(photos)} never-used, "
            f"{len(eligible)} eligible  |  covers {len(covers)}"
        )


def _photo_is_eligible(path, usage: dict, now_utc, cooldown_days: int) -> bool:
    from datetime import datetime as _dt
    last = usage.get(str(path))
    if last is None:
        return True
    try:
        last_dt = _dt.fromisoformat(last)
    except Exception:
        return True
    return (now_utc - last_dt).days >= cooldown_days


@app.command("backfill-postgres")
def backfill_postgres(
    dsn: str = typer.Option(..., help="Target Postgres DSN, e.g. postgresql://user:pass@host/db"),
    since: str = typer.Option(None, help="Only backfill posts posted on/after YYYY-MM-DD"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """
    One-shot: read local stats.sqlite + state.json and INSERT directly into
    Postgres. Idempotent by natural keys — safe to re-run.
    """
    _setup_logging(verbose=verbose)
    try:
        import psycopg  # local import; not a script runtime dep
    except ImportError:
        typer.echo("psycopg not installed. Run:  uv add psycopg[binary]")
        raise typer.Exit(1)
    from . import backfill
    backfill.run(dsn=dsn, since=since)


@app.command("outbox")
def outbox(
    purge_days: int = typer.Option(0, help="Purge sent events older than N days (0 = don't purge)"),
):
    """Show reporter outbox status. Optionally purge old sent events."""
    summary = reporter_mod.outbox_summary()
    typer.echo(f"Reporter configured: {summary['configured']}")
    typer.echo(f"  pending: {summary['pending']}")
    typer.echo(f"  sent:    {summary['sent']}")
    typer.echo(f"  retrying:{summary['retrying']}   max_attempts={summary['max_attempts']}")
    if purge_days > 0:
        n = reporter_mod.purge_sent(older_than_days=purge_days)
        typer.echo(f"  purged:  {n}")


@app.command("tail")
def tail(lines: int = typer.Option(50, help="How many lines of history to show first")):
    """Stream logs/run.log live. Ctrl+C to stop."""
    import time

    log_path = LOGS_DIR / "run.log"
    if not log_path.exists():
        typer.echo(f"No log file yet at {log_path}. Run a command first.")
        raise typer.Exit(0)
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        # Print last N lines, then follow
        history = f.readlines()[-lines:]
        for line in history:
            typer.echo(line.rstrip())
        f.seek(0, 2)  # to end
        try:
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                typer.echo(line.rstrip())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    app()
