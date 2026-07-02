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
            raise typer.Exit(0)

    ad = generate_ad(acct)
    logger.info(f"Generated ad: {ad.title}  ({len(ad.photos)} photos)")
    url = post_ad(acct, ad, headless=headless, dry_run=dry_run)
    if url and not dry_run:
        record_post(acct, ad.title, url)
        typer.echo(f"Posted: {url}")
    elif dry_run:
        typer.echo("Dry run complete.")
    else:
        typer.echo("Post failed — check logs.")
        raise typer.Exit(3)


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
