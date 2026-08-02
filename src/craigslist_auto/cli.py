from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from loguru import logger

# Load .env (REPORTER_URL/REPORTER_TOKEN, etc.) before any module reads os.environ.
load_dotenv()

from .accounts import record_post
from .config import ACCOUNTS, ACCOUNTS_BY_NAME, EXCEL_PATH, LOGS_DIR, clamp_guardrails
from .ghost_check import check_all_recent
from .poster import PosterFailure, launch_account, post_ad
from . import artifacts as artifacts_mod
from . import content as content_mod
from . import queue_client
from . import stats as stats_mod
from . import reporter as reporter_mod
from .events import PostAttempt
from .stats import extract_post_id

app = typer.Typer(add_completion=False, no_args_is_help=True)

# How often the reporter daemon checks for edit work. Short because hydration is
# on demand (DESIGN_EDITS decision 24) and you are watching for it in the UI.
EDIT_POLL_SECONDS = 15

# How often the daemon looks for an operator's "Post now". Same beat as edits,
# for the same reason: someone is watching the dashboard for it to happen.
POST_REQUEST_POLL_SECONDS = 15

# Hard ceiling on one spawned posting run. Comfortably longer than a real post
# (a couple of minutes) and deliberately shorter than the server's 45-minute
# stale-claim reaper, so a hung run is killed while the server still believes
# the draft is claimed — the reaper then parks it with its "may or may not have
# published" message, which is the right answer for a state nobody knows.
POST_REQUEST_TIMEOUT_SECONDS = 1500


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
    """Show which accounts can post right now and why not.

    Eligibility is answered by the server (decision 15), so this reflects what
    `cl post` will actually be allowed to do — not a local guess.
    """
    _setup_logging()
    machine = _machine_name()
    typer.echo(f"Machine: {machine}")

    bound = [a.name for a in ACCOUNTS if a.allowed_machine == machine]
    if not bound:
        typer.echo("  (no accounts bound to this machine)")
    else:
        try:
            report = queue_client.eligibility(bound)
        except queue_client.QueueUnavailable as e:
            typer.echo(f"  [!!] queue unavailable: {e}")
            typer.echo("       posting is fail-closed, so nothing will post until this clears")
        else:
            for reason in report.get("global_blocks") or []:
                typer.echo(f"  [--] all accounts: {reason}")
            for name, info in (report.get("accounts") or {}).items():
                mark = "[OK]" if info["eligible"] else "[--]"
                reasons = "; ".join(info["reasons"]) if info["reasons"] else "ready"
                typer.echo(f"  {mark} {name:10s}  depth={info['queue_depth']:<3d} {reasons}")

    outbox = reporter_mod.outbox_summary()
    typer.echo("")
    typer.echo(
        f"Outbox: {outbox['pending']} pending, {outbox['sent']} sent"
        f"{'  (reporter not configured)' if not outbox['configured'] else ''}"
    )

    spooled = artifacts_mod.pending_count()
    if spooled:
        typer.echo(f"Artifacts: {spooled} awaiting upload")

    # Whoever holds the browser explains why anything else is idle right now.
    from .lease import current_holder
    holder = current_holder()
    if holder:
        typer.echo(
            f"Browser: in use by {holder['flow']}"
            f"{' (' + holder['account'] + ')' if holder.get('account') else ''}"
        )

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
    account: str = typer.Option(None, help="Restrict the claim to one account"),
    requested_draft_id: int = typer.Option(
        None, "--draft-id",
        help="Post this specific draft (an operator's 'Post now'). It must "
             "still carry a live request on the server.",
    ),
    dry_run: bool = typer.Option(False, help="Walk the form but don't publish (claims nothing)"),
    headless: bool = typer.Option(False, help="Run browser headless (NOT recommended)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="DEBUG-level console logging"),
):
    """Claim the next queued draft from the dashboard and post it.

    The server decides whether this machine may post and which account goes
    next (decision 15). If the queue is empty or nothing is eligible, this
    no-ops — it never invents an ad.
    """
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    _setup_logging(verbose=verbose)
    machine = _machine_name()

    # `--dry-run` peeks at the head of the queue and claims nothing, so it
    # cannot honour a specific id — it would rehearse a different draft than the
    # one asked for and report success. Refuse rather than mislead.
    if requested_draft_id is not None and dry_run:
        typer.echo("--draft-id cannot be combined with --dry-run: a dry run reads "
                   "the head of the queue and would walk a different draft.")
        raise typer.Exit(2)

    # Misconfiguration exits report to the server before quitting. A wrong
    # CL_MACHINE used to stop posting forever with no server-side trace: every
    # fire exited here silently, the dashboard showed the last successful post
    # ageing, and nothing ever said why.
    bound = [a.name for a in ACCOUNTS if a.allowed_machine == machine]
    if account:
        if account not in ACCOUNTS_BY_NAME:
            typer.echo(f"Unknown account: {account}")
            reporter_mod.report_flow_error(
                "post", f"unknown account {account!r}", step="machine_binding",
                context={"machine": machine, "known": list(ACCOUNTS_BY_NAME)},
            )
            raise typer.Exit(1)
        if account not in bound:
            typer.echo(
                f"REFUSING: account {account} is bound to machine "
                f"'{ACCOUNTS_BY_NAME[account].allowed_machine}', but this machine is "
                f"'{machine}'. Set CL_MACHINE or run on the correct machine."
            )
            reporter_mod.report_flow_error(
                "post",
                f"account {account} is bound to "
                f"{ACCOUNTS_BY_NAME[account].allowed_machine!r}, not this machine",
                step="machine_binding", account=account,
                context={"machine": machine},
            )
            raise typer.Exit(2)
        bound = [account]
    if not bound:
        typer.echo(f"No accounts are bound to this machine ({machine}).")
        reporter_mod.report_flow_error(
            "post",
            f"no accounts bound to machine {machine!r} — posting cannot run here",
            step="machine_binding",
            context={
                "machine": machine,
                "configured": {a.name: a.allowed_machine for a in ACCOUNTS},
            },
        )
        raise typer.Exit(1)

    # The server owns post history, so it must not authorise a post while we
    # still hold unsent evidence of a completed post (see DESIGN.md, derived
    # reqs). Only 'posted' attempts matter — blocking on unsent skips would
    # strand the queue behind noise, permanently if the reporter is unset.
    still_queued = reporter_mod.flush_until_empty()
    pending = reporter_mod.pending_history_count()
    if still_queued:
        logger.warning(
            f"outbox has {still_queued} unsent event(s) after flushing "
            f"({pending} of them affect post history)"
        )

    try:
        draft = (
            _peek_draft(bound) if dry_run
            else queue_client.claim(
                bound, outbox_pending=pending, draft_id=requested_draft_id
            )
        )
    except queue_client.QueueUnavailable as e:
        logger.error(f"queue unavailable: {e}")
        reporter_mod.report_flow_error("post", e, step="claim", context={"machine": machine})
        typer.echo(f"Queue unavailable — not posting. {e}")
        raise typer.Exit(4)

    if draft is None:
        if requested_draft_id is not None:
            # Exit 0, deliberately. The daemon spawns this every 15s while a
            # request is live and reports non-zero exits as flow_errors; a
            # guardrail that has not cleared yet is a normal answer, not a
            # failure, and treating it as one would file the same Diagnostics
            # entry eighty times before the request expired.
            typer.echo(
                f"Draft {requested_draft_id} was not claimed — see the reason "
                f"above. Nothing was posted."
            )
            raise typer.Exit(0)
        typer.echo("Nothing to post right now. Run `cl status` to see why.")
        reporter_mod.emit(PostAttempt(
            ts=_dt.now(_tz.utc),
            machine=machine,
            account="(none)",
            outcome="skipped_no_drafts",
        ))
        raise typer.Exit(0)

    acct = ACCOUNTS_BY_NAME[draft["account"]]
    draft_id = int(draft["id"])

    # Images are optional. A download that fails costs pictures, not the post.
    try:
        photos = queue_client.fetch_draft_images(draft)
    except Exception as e:
        logger.warning(f"image download failed, posting without pictures: {e}")
        reporter_mod.report_flow_error("post", e, step="fetch_images",
                                       account=acct.name, context={"draft_id": draft_id})
        photos = []

    # Building the Ad used to be unguarded. A malformed draft raised here, the
    # traceback went to a Task Scheduler window nobody sees, and the draft sat
    # 'claimed' on the server forever — invisible to queue depth, so the account
    # silently reported "queue empty" and stopped posting. 'build_ad' is a
    # pre-upload step, so reporting it returns the draft to the queue.
    try:
        ad = content_mod.ad_from_draft(draft, photos)
    except Exception as e:
        logger.exception(f"could not build an ad from draft {draft_id}")
        _emit_post_failure(
            machine, acct.name, "failed_other", repr(e),
            started=_time.monotonic(), ad_title=draft.get("title"),
            draft_id=draft_id, failed_step="build_ad",
        )
        typer.echo(f"Draft {draft_id} is malformed — returned to the queue. Check logs.")
        raise typer.Exit(3)

    logger.info(
        f"draft {draft_id} for {acct.name}: {ad.title!r}  ({len(photos)} image(s))"
    )

    started = _time.monotonic()
    try:
        run = post_ad(acct, ad, headless=headless, dry_run=dry_run)
    except PosterFailure as e:
        _emit_post_failure(
            machine, acct.name, _outcome_for_step(e.step), str(e.original),
            started, ad.title, draft_id=draft_id, failed_step=e.step, run=e.run,
        )
        typer.echo(f"Post failed at step '{e.step}' — check logs.")
        _flush_evidence()
        raise typer.Exit(3)
    except Exception as e:
        # Everything from the first page load onward is inside post_ad's own
        # try, so an exception reaching here was raised by launch_account or
        # ctx.new_page(): a held browser lease, a locked Chrome profile, a
        # missing patchright install. Nothing touched Craigslist, so the step is
        # 'launch' — and reporting it as such returns the draft to the queue
        # instead of parking it for a human (DESIGN.md decision 16).
        logger.exception("post_ad raised before the browser reached Craigslist")
        _emit_post_failure(
            machine, acct.name, "failed_other", repr(e),
            started, ad.title, draft_id=draft_id, failed_step="launch",
        )
        typer.echo("Post failed at launch — check logs.")
        _flush_evidence()
        raise typer.Exit(3)

    duration = _time.monotonic() - started

    if dry_run:
        reporter_mod.emit(PostAttempt(
            ts=_dt.now(_tz.utc),
            machine=machine,
            account=acct.name,
            outcome="dry_run",
            duration_seconds=duration,
            ad_title=ad.title,
            draft_id=draft_id,
            warnings=run.warnings,
            photos_confirmed=run.photos_confirmed,
            artifact_ids=run.artifact_ids,
        ))
        typer.echo("Dry run complete (draft left in the queue).")
        _flush_evidence()
        return

    if run.url:
        record_post(acct, ad.title, run.url)
        reporter_mod.emit(PostAttempt(
            ts=_dt.now(_tz.utc),
            machine=machine,
            account=acct.name,
            outcome="posted",
            duration_seconds=duration,
            post_id=extract_post_id(run.url),
            post_url=run.url,
            ad_title=ad.title,
            draft_id=draft_id,
            photos_attached=[p.name for p in ad.photos],
            cover_photo=ad.photos[0].name if ad.photos else None,
            # The post published, so the outcome stays 'posted' and history is
            # unaffected. These are what stop a green badge hiding a broken ad.
            warnings=run.warnings,
            photos_confirmed=run.photos_confirmed,
            artifact_ids=run.artifact_ids,
        ))
        typer.echo(f"Posted: {run.url}")
        for w in run.warnings:
            typer.echo(f"  [!] {w}")
    else:
        # post_ad returned without a URL and without raising — the form was
        # submitted but the confirmation page gave us nothing to record.
        _emit_post_failure(
            machine, acct.name, "failed_form", "post_ad returned no url",
            started, ad.title, draft_id=draft_id, failed_step="confirmation",
            run=run,
        )
        typer.echo("Post failed — check logs.")
        _flush_evidence()
        raise typer.Exit(3)

    _flush_evidence()


def _flush_evidence() -> None:
    """Push screenshots and events up now rather than on the daemon's next beat.

    `cl post` is a short-lived process fired by Task Scheduler. Without this,
    evidence for a 9am failure waits on the reporter daemon — and if the daemon
    is not running, which is exactly the situation worth debugging, it waits
    forever. Never raises: this runs on the way out of a failure path.
    """
    try:
        artifacts_mod.flush_once(limit=10)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"artifact flush failed: {e}")
    try:
        reporter_mod.flush_once()
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"event flush failed: {e}")


def _peek_draft(accounts: list[str]) -> dict | None:
    """Head of the queue without claiming it — used by --dry-run so a rehearsal
    never consumes a real draft."""
    for d in queue_client.fetch_queue(limit=10):
        if d["account"] in accounts:
            return d
    return None


def _outcome_for_step(step: str | None) -> str:
    if step == "login_check":
        return "failed_login"
    if step and step.startswith("form"):
        return "failed_form"
    return "failed_other"


def _emit_post_failure(
    machine: str,
    account_name: str,
    error_type: str,
    error_message: str,
    started: float,
    ad_title: str | None,
    *,
    draft_id: int | None = None,
    failed_step: str | None = None,
    run=None,
) -> None:
    """Report a failed post.

    `failed_step` is load-bearing, not decoration: the server routes the draft
    on it (DESIGN.md decision 16). A pre-upload step returns the draft to the
    queue; anything else parks it for a human because images were burned. Never
    pass None — an unknown step is treated as post-upload, so a launch failure
    that consumed nothing would still cost you a manual rescue.
    """
    import time as _time
    from datetime import datetime as _dt, timezone as _tz
    reporter_mod.emit(PostAttempt(
        ts=_dt.now(_tz.utc),
        machine=machine,
        account=account_name,
        outcome=error_type,  # type: ignore[arg-type]
        duration_seconds=_time.monotonic() - started,
        ad_title=ad_title,
        draft_id=draft_id,
        error_type=error_type,
        error_message=error_message[:500],
        failed_step=failed_step,
        warnings=list(getattr(run, "warnings", []) or []),
        photos_confirmed=getattr(run, "photos_confirmed", None),
        artifact_ids=list(getattr(run, "artifact_ids", []) or []),
    ))


@app.command("check-ghosts")
def check_ghosts(
    proxy: str = typer.Option(
        None,
        help="HTTP proxy to check through, e.g. http://host:port. Use a phone "
        "hotspot or any non-home exit so the check is a true public view.",
    ),
    allow_local_ip: bool = typer.Option(
        False,
        "--allow-local-ip",
        help="Check from this machine's own IP. Weaker: CL shows you your own "
        "ghosted posts, so 'visible' results can be false.",
    ),
):
    """
    Check whether recent posts are visible in public search.
    Prefer --proxy so the request egresses from a different IP than the one that
    created the post; otherwise your own session sees ghosted posts as 'visible'.
    """
    _setup_logging()
    try:
        check_all_recent(proxy=proxy, allow_local_ip=allow_local_ip)
    except Exception as e:
        logger.exception("check-ghosts failed")
        reporter_mod.report_flow_error("ghost_check", e)
        raise typer.Exit(3)


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
    typer.echo("Tip: run `cl check-ghosts --proxy http://host:port` to refresh ghost status from a different network.")


@app.command("stats-sync")
def stats_sync(
    account: str = typer.Option(None, help="Sync only this account (default: all)"),
    headless: bool = typer.Option(False, help="Run browser headless"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scrape today's stats snapshot for every account's active postings."""
    _setup_logging(verbose=verbose)
    try:
        summary = stats_mod.sync_all(headless=headless, only_account=account)
    except Exception as e:
        logger.exception("stats-sync failed")
        reporter_mod.report_flow_error("stats_sync", e, account=account)
        raise typer.Exit(3)
    typer.echo(f"Snapshot date: {summary['snapshot_date']}")
    for name, info in summary["accounts"].items():
        if info.get("ok"):
            typer.echo(f"  [OK] {name:10s}  rows={info['rows']}  frozen={info['frozen']}")
        else:
            typer.echo(f"  [--] {name:10s}  ERROR: {info.get('error')}")

    # Ship the snapshots now rather than waiting on the reporter daemon, for the
    # same reason `cl post` does: this is a short-lived Scheduled Task, and if
    # the daemon is not running the events wait forever. That is not
    # hypothetical — the scrape emitted to the outbox and nothing drained it,
    # so the dashboard's stats went stale while the scrape itself looked fine.
    _flush_evidence()
    pending = reporter_mod.pending_count()
    if pending:
        typer.echo(
            f"  [!] {pending} event(s) still unsent — check REPORTER_URL/REPORTER_TOKEN"
        )


@app.command("scan-ended")
def scan_ended(
    account: str = typer.Option(None, help="Only this account (default: all)"),
    headless: bool = typer.Option(False, help="Run browser headless"),
    no_capture: bool = typer.Option(
        False, "--no-capture",
        help="Skip the page captures. They are how the selectors for reading an "
             "ended posting's body get written, so leave them on the first time.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Recover ended postings from the account page.

    When a posting ends, Craigslist stops serving its public URL and stops
    offering an edit form — so hydration, the only route to a body, is gone with
    it. The account page's inactive and deleted tabs are the last place those
    ads are described at all.

    This walks them and reports every posting it finds, so an ad that ended
    stops being a bare id in the dashboard. It also captures the pages, because
    the body and the images live one click further in and nothing here has seen
    that page yet.
    """
    _setup_logging(verbose=verbose)
    try:
        summary = stats_mod.scan_ended(
            headless=headless, only_account=account, capture=not no_capture
        )
    except Exception as e:
        logger.exception("scan-ended failed")
        reporter_mod.report_flow_error("scan_ended", e, account=account)
        raise typer.Exit(3)

    total = 0
    for name, info in summary["accounts"].items():
        if info.get("ok"):
            tabs = "  ".join(f"{k}={v}" for k, v in (info.get("tabs") or {}).items())
            typer.echo(f"  [OK] {name:10s}  {info['rows']} ended posting(s)   {tabs}")
            total += info["rows"]
        else:
            typer.echo(f"  [--] {name:10s}  ERROR: {info.get('error')}")

    typer.echo("")
    typer.echo(f"{total} ended posting(s) reported to the dashboard.")
    _flush_evidence()
    pending = reporter_mod.pending_count()
    if pending:
        typer.echo(f"  [!] {pending} event(s) still unsent — check REPORTER_URL/REPORTER_TOKEN")


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
        code_version,
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
            code_version=code_version(),
        ))
        logger.info(f"reporter-daemon starting on {code_version()}")
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

    def _sync_loop():
        """Pull server-owned guardrails and warm the queue prefetch.

        Nothing here decides anything — claiming happens at the posting slot
        (decision 4). This exists so the machine notices a clamped setting or an
        unreachable queue between slots rather than at 9am.
        """
        bound = [a.name for a in ACCOUNTS if a.allowed_machine == machine]
        last_notes: list[str] = []
        while not stop.is_set():
            try:
                remote = queue_client.fetch_settings()
                guardrails, notes = clamp_guardrails(remote)
                logger.debug(f"guardrails in effect: {guardrails}")
                # Only report a clamp when it changes, so a permanently bad
                # server value doesn't spam the dashboard every cycle.
                if notes and notes != last_notes:
                    reporter_mod.report_flow_error(
                        "queue_sync",
                        "server guardrails exceeded this machine's compiled ceilings",
                        step="clamp",
                        context={"clamps": notes},
                    )
                last_notes = notes
                if bound:
                    queue_client.fetch_queue(limit=10)
            except queue_client.QueueUnavailable as e:
                logger.warning(f"queue sync failed: {e}")
                reporter_mod.report_flow_error("queue_sync", e, step="fetch")
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(f"queue sync raised: {e}")
                reporter_mod.report_flow_error("queue_sync", e)
            if stop.wait(120):
                return

    sync_thread = threading.Thread(target=_sync_loop, name="cl-queue-sync", daemon=True)
    sync_thread.start()

    def _edit_loop():
        """Poll for edit work and do it (DESIGN_EDITS.md decision 29).

        Its own thread on a ~15s beat, separate from the 120s queue sync: with
        on-demand hydration you sit watching a spinner in the dashboard, so the
        latency is user-visible in a way the queue prefetch never was.

        The browser lease is taken non-blocking inside the worker, so a posting
        run in progress simply makes this skip and try again.
        """
        from . import edit_worker

        bound = [a.name for a in ACCOUNTS if a.allowed_machine == machine]
        if not bound:
            logger.info("edit worker idle: no accounts bound to this machine")
            return
        while not stop.is_set():
            try:
                edit_worker.run_once(machine, bound)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(f"edit worker raised: {e}")
                reporter_mod.report_flow_error("edit_worker", e)
            try:
                artifacts_mod.flush_once()
            except Exception as e:  # pragma: no cover
                logger.warning(f"artifact flush raised: {e}")
            if stop.wait(EDIT_POLL_SECONDS):
                return

    edit_thread = threading.Thread(target=_edit_loop, name="cl-edit-worker", daemon=True)
    edit_thread.start()

    def _post_request_loop():
        """Execute an operator's "Post now" from the dashboard.

        Its own thread, not folded into `_edit_loop`: a posting run blocks for
        minutes and would stall hydration polling for its whole duration.

        Spawns `cl post --draft-id` as a subprocess rather than posting inline.
        That path — machine binding, outbox flush, image fetch, the form, the
        failure taxonomy — is the most carefully tested code in the project and
        is reused here byte for byte. A daemon thread reimplementing it would be
        a second posting path to keep correct forever.
        """
        import subprocess

        from . import edit_worker, lease

        bound = [a.name for a in ACCOUNTS if a.allowed_machine == machine]
        if not bound:
            logger.info("post-now watcher idle: no accounts bound to this machine")
            return
        # `cli.py` is at src/craigslist_auto/cli.py; the project root is two up.
        # `load_dotenv()` searches upward from cwd, so the child needs this to
        # find the same .env this process read.
        project_root = Path(__file__).resolve().parents[2]

        while not stop.is_set():
            req = None
            try:
                work = queue_client.post_requests(bound)
                requests = work.get("requests") or []
                if requests:
                    # One per beat. Same reasoning as max_reconciles=1 in the
                    # edit worker — spacing browser sessions is deliberate.
                    req = requests[0]
                    holder = lease.current_holder()
                    if edit_worker.near_posting_slot():
                        # Defer, don't cancel; the request's TTL is the backstop.
                        logger.info(
                            "post-now deferred: within 10 minutes of a scheduled slot"
                        )
                    elif holder is not None:
                        logger.info(
                            f"post-now deferred: browser held by {holder['flow']!r}"
                        )
                    else:
                        logger.info(
                            f"post-now: spawning a posting run for draft "
                            f"{req['id']} ({req['account']})"
                        )
                        # Hold our flusher: the child samples the outbox to
                        # decide whether the server may authorise its claim, and
                        # a batch of ours mid-POST would read as a backlog.
                        with reporter_mod.pause_flushing():
                            proc = subprocess.run(
                                [sys.executable, "-m", "craigslist_auto.cli",
                                 "post", "--draft-id", str(req["id"])],
                                cwd=str(project_root),
                                capture_output=True,
                                text=True,
                                timeout=POST_REQUEST_TIMEOUT_SECONDS,
                                # Hide the child's console window, not Chrome —
                                # the poster runs headful and needs the desktop.
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            )
                        logger.info(
                            f"post-now: draft {req['id']} run exited {proc.returncode}"
                        )
                        if proc.returncode != 0:
                            reporter_mod.report_flow_error(
                                "post_now",
                                f"posting run for draft {req['id']} exited "
                                f"{proc.returncode}",
                                step="subprocess",
                                account=req["account"],
                                context={
                                    "draft_id": req["id"],
                                    "returncode": proc.returncode,
                                    "stdout": (proc.stdout or "")[-1500:],
                                    "stderr": (proc.stderr or "")[-1500:],
                                },
                            )
            except subprocess.TimeoutExpired:
                logger.error(f"post-now: run exceeded {POST_REQUEST_TIMEOUT_SECONDS}s")
                reporter_mod.report_flow_error(
                    "post_now",
                    f"posting run exceeded {POST_REQUEST_TIMEOUT_SECONDS}s and was "
                    f"killed; the draft may or may not have published",
                    step="subprocess",
                    context={"draft_id": (req or {}).get("id")},
                )
            except queue_client.QueueUnavailable as e:
                logger.warning(f"post-now poll failed: {e}")
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(f"post-now watcher raised: {e}")
                reporter_mod.report_flow_error("post_now", e)
            if stop.wait(POST_REQUEST_POLL_SECONDS):
                return

    post_now_thread = threading.Thread(
        target=_post_request_loop, name="cl-post-now", daemon=True
    )
    post_now_thread.start()

    # Flusher runs on the main thread; blocks until stop is set.
    reporter_mod.flush_forever(stop_event=stop)


@app.command("edit")
def edit(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Rehearse: open the form, diff against desired, type nothing. "
             "Safe on live posts.",
    ),
    headless: bool = typer.Option(False, help="Run browser headless (NOT recommended)"),
    reconciles: int = typer.Option(1, help="Max reconciles this pass"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run one pass of pending edit work (hydrate + reconcile).

    The reporter daemon does this every 15s on its own; this is the manual
    handle for verifying it in production.

    `--dry-run` is deliberately read-only, unlike `cl post --dry-run`:
    Craigslist commits image operations when a file is selected, so a
    fill-then-abandon rehearsal could strip a live posting of its images
    (DESIGN_EDITS.md decision 36).
    """
    from . import edit_worker

    _setup_logging(verbose=verbose)
    machine = _machine_name()
    bound = [a.name for a in ACCOUNTS if a.allowed_machine == machine]
    if not bound:
        typer.echo(f"No accounts are bound to this machine ({machine}).")
        raise typer.Exit(1)

    if dry_run:
        typer.echo("DRY RUN — nothing will be typed or uploaded.")

    summary = edit_worker.run_once(
        machine, bound, dry_run=dry_run, headless=headless, max_reconciles=reconciles,
    )
    typer.echo(
        f"hydrated={summary['hydrated']}  reconciled={summary['reconciled']}  "
        f"skipped={summary['skipped']}  failed={summary['failed']}"
    )
    for r in summary["results"]:
        detail = r.get("outcome") or ("ok" if r.get("ok") else "failed")
        typer.echo(f"  {r['kind']:9s} {r['post_id']:12s} {detail}")
    if not summary["results"]:
        typer.echo("Nothing to do. Load or edit a post in the dashboard first.")

    # Push evidence up now so a manual run is immediately inspectable.
    sent = artifacts_mod.flush_once(limit=20)
    if sent:
        typer.echo(f"Uploaded {sent} artifact(s) to the dashboard.")


@app.command("edit-canary")
def edit_canary(
    post_id: str = typer.Argument(..., help="Post id to really edit"),
    headless: bool = typer.Option(False, help="Run browser headless"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Perform ONE real end-to-end edit against a disposable post.

    Read-only rehearsal proves the plan; this proves the write actually lands
    (DESIGN_EDITS.md decision 37). Refuses any post not listed in the
    CL_CANARY_POSTS environment variable, so it cannot be aimed at a live
    earning posting by a typo.
    """
    from . import edit_worker

    _setup_logging(verbose=verbose)
    machine = _machine_name()
    allowlist = [
        p.strip() for p in os.environ.get("CL_CANARY_POSTS", "").split(",") if p.strip()
    ]
    if not allowlist:
        typer.echo(
            "CL_CANARY_POSTS is not set. Set it to a comma-separated list of "
            "post ids you are willing to have really edited."
        )
        raise typer.Exit(2)

    typer.echo(f"CANARY — really editing post {post_id}. This writes to Craigslist.")
    try:
        result = edit_worker.run_canary(
            machine, post_id, allowlist=allowlist, headless=headless
        )
    except (ValueError, RuntimeError) as e:
        typer.echo(f"Refused: {e}")
        raise typer.Exit(2)

    typer.echo(f"Outcome: {result['outcome']}")
    for s in result.get("steps", []):
        mark = "ok " if s["ok"] else "FAIL"
        secs = f"{s['duration_seconds']:.1f}s" if s.get("duration_seconds") else "-"
        typer.echo(f"  [{mark}] {s['name']:20s} {secs:>7s}  {s.get('note') or ''}")
    if result.get("error_message"):
        typer.echo(f"Error at {result.get('failed_step')}: {result['error_message']}")
    if result["outcome"] == "degraded_live":
        typer.echo("")
        typer.echo("!! THIS POST IS LIVE AND DEGRADED. Check the dashboard now.")
        raise typer.Exit(3)


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
