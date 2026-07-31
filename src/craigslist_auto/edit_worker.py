"""The edit worker: poll the VPS for edit work, do it, report back.

DESIGN_EDITS.md decisions 24, 27, 28, 29, 32, 36.

Runs inside the reporter daemon (which is already always-on and already holds a
machine token) on its own ~15s poll. Two kinds of work come back from
`GET /queue/edits/pending`:

    hydrate    — read a live posting's edit form so the dashboard can show it
    reconcile  — make a live posting match the desired state

Everything this module does is reported through the durable outbox as a
`post_content` or `post_edit_attempt` event, so a VPS outage delays reporting
rather than losing it — same contract as posting.

Edits never contend with posting. The browser lease is taken non-blocking, so a
post in flight makes the worker skip and try again next cycle, and decision 28's
posting-slot guard keeps it from starting a long edit right before 9/1/5.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from . import artifacts, editor, queue_client, reporter
from .config import ACCOUNTS_BY_NAME
from .events import PostContent, PostEditAttempt, PostImage
from .lease import LeaseBusy

ET = ZoneInfo("America/New_York")

# Task Scheduler fires `cl post` at these local hours (scripts/install-schedule.ps1).
POSTING_SLOT_HOURS = (9, 13, 17)
# Decision 28: an edit is never worth delaying a post, and a reconcile with five
# image uploads can run for minutes. Stay out of the way around each slot.
SLOT_GUARD_MINUTES = 10


def near_posting_slot(now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    for hour in POSTING_SLOT_HOURS:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if abs((now - slot).total_seconds()) <= SLOT_GUARD_MINUTES * 60:
            return True
    return False


def _emit_content(machine: str, result: dict) -> None:
    reporter.emit(PostContent(
        ts=datetime.now(timezone.utc),
        machine=machine,
        account=result["account"],
        post_id=result["post_id"],
        ok=result.get("ok", False),
        error_type=result.get("error_type"),
        error_message=result.get("error_message"),
        title=result.get("title"),
        body=result.get("body"),
        county=result.get("county"),
        city=result.get("city"),
        service_offered=result.get("service_offered"),
        postal_code=result.get("postal_code"),
        license_number=result.get("license_number"),
        phone_number=result.get("phone_number"),
        images=[PostImage(**i) for i in result.get("images", [])],
        content_hash=result.get("content_hash"),
        editable=result.get("editable", True),
        live_status=result.get("live_status"),
        artifact_ids=result.get("artifact_ids", []),
    ))


def _emit_attempt(machine: str, result: dict) -> None:
    reporter.emit(PostEditAttempt(
        ts=datetime.now(timezone.utc),
        machine=machine,
        account=result["account"],
        post_id=result["post_id"],
        outcome=result["outcome"],
        duration_seconds=result.get("duration_seconds"),
        desired_rev=result.get("desired_rev"),
        applied_rev=result.get("applied_rev"),
        steps=result.get("steps", []),
        failed_step=result.get("failed_step"),
        error_type=result.get("error_type"),
        error_message=result.get("error_message"),
        images_live_count=result.get("images_live_count"),
        images_desired_count=result.get("images_desired_count"),
        artifact_ids=result.get("artifact_ids", []),
    ))


def run_once(
    machine: str,
    accounts: list[str],
    *,
    dry_run: bool = False,
    headless: bool = False,
    max_hydrations: int = 3,
    max_reconciles: int = 1,
) -> dict:
    """One pass of edit work. Returns a small summary for the caller to print.

    `max_reconciles` defaults to 1: each reconcile is a full browser session on
    an aged account, and spacing them across poll cycles looks far less like a
    script than running several back to back.
    """
    summary = {"hydrated": 0, "reconciled": 0, "skipped": 0, "failed": 0, "results": []}

    try:
        work = queue_client.edits_pending(accounts)
    except queue_client.QueueUnavailable as e:
        logger.warning(f"edit poll failed: {e}")
        return summary

    hydrate = work.get("hydrate") or []
    reconcile = work.get("reconcile") or []
    if not hydrate and not reconcile:
        return summary

    logger.info(
        f"edit work: {len(hydrate)} to hydrate, {len(reconcile)} to reconcile"
    )

    # -------------------------------------------------- hydration (read-only)
    for item in hydrate[:max_hydrations]:
        account = ACCOUNTS_BY_NAME.get(item["account"])
        if account is None:
            logger.warning(f"hydrate skipped: unknown account {item['account']!r}")
            continue
        try:
            result = editor.hydrate_post(account, item["post_id"], headless=headless)
        except LeaseBusy as e:
            logger.info(f"hydrate deferred, browser busy: {e}")
            summary["skipped"] += 1
            break
        except Exception as e:
            logger.exception(f"hydrate raised for post {item['post_id']}")
            reporter.report_flow_error(
                "edit_hydrate", e, account=item["account"],
                context={"post_id": item["post_id"]},
            )
            summary["failed"] += 1
            continue
        _emit_content(machine, result)
        summary["hydrated"] += 1
        summary["results"].append(
            {"post_id": item["post_id"], "kind": "hydrate", "ok": result.get("ok")}
        )

    # ------------------------------------------------------------- reconcile
    if reconcile and near_posting_slot():
        logger.info("skipping reconciles: too close to a posting slot (decision 28)")
        return summary

    for item in reconcile[:max_reconciles]:
        account = ACCOUNTS_BY_NAME.get(item["account"])
        if account is None:
            continue
        post_id = item["post_id"]

        if dry_run:
            # A rehearsal must not consume the claim, exactly as `cl post
            # --dry-run` reads the queue head without claiming it.
            desired = dict(item)
            desired.setdefault("base_hash", None)
            desired.setdefault("image_set_managed", False)
        else:
            try:
                desired = queue_client.claim_edit(post_id)
            except queue_client.QueueUnavailable as e:
                logger.warning(f"edit claim failed for {post_id}: {e}")
                break
            if desired is None:
                logger.debug(f"edit for {post_id} was taken by someone else")
                continue

        try:
            photos = (
                queue_client.fetch_post_images(desired)
                if desired.get("image_set_managed") else []
            )
        except queue_client.QueueUnavailable as e:
            # Decision 33 replaces the whole image set; a partial download would
            # delete images from a live posting. Refuse before touching anything.
            logger.error(f"image download failed for {post_id}, not editing: {e}")
            _emit_attempt(machine, {
                "post_id": post_id, "account": account.name,
                "outcome": "failed_images", "failed_step": "fetch_images",
                "error_type": "image_download_failed", "error_message": str(e)[:500],
                "desired_rev": desired.get("desired_rev"),
            })
            summary["failed"] += 1
            continue

        try:
            result = editor.reconcile_post(
                account, desired, photos, dry_run=dry_run, headless=headless
            )
        except LeaseBusy as e:
            logger.info(f"reconcile deferred, browser busy: {e}")
            _emit_attempt(machine, {
                "post_id": post_id, "account": account.name,
                "outcome": "skipped_lease_held",
                "error_type": "lease_busy", "error_message": str(e)[:300],
                "desired_rev": desired.get("desired_rev"),
            })
            summary["skipped"] += 1
            break
        except Exception as e:
            logger.exception(f"reconcile raised for post {post_id}")
            _emit_attempt(machine, {
                "post_id": post_id, "account": account.name,
                "outcome": "failed_other", "failed_step": "unknown",
                "error_type": type(e).__name__, "error_message": str(e)[:500],
                "desired_rev": desired.get("desired_rev"),
            })
            summary["failed"] += 1
            continue

        _emit_attempt(machine, result)
        summary["results"].append(
            {"post_id": post_id, "kind": "reconcile", "outcome": result["outcome"]}
        )
        if result["outcome"] in ("applied", "no_change", "dry_run"):
            summary["reconciled"] += 1
        else:
            summary["failed"] += 1
            if result["outcome"] == "degraded_live":
                logger.error(
                    f"POST {post_id} IS LIVE AND DEGRADED — "
                    f"{result.get('images_live_count')} image(s) instead of "
                    f"{result.get('images_desired_count')}. Check the dashboard."
                )

    return summary


def run_canary(
    machine: str, post_id: str, *, allowlist: list[str], headless: bool = False
) -> dict:
    """One real end-to-end edit against a post you have designated disposable.

    Decision 37. Read-only rehearsal proves the plan; this proves the write.
    Refuses anything not on the allowlist so it can never be pointed at a
    revenue-earning posting by a typo.
    """
    if post_id not in allowlist:
        raise ValueError(
            f"post {post_id} is not on the canary allowlist. Set CL_CANARY_POSTS "
            f"to a comma-separated list of disposable post ids."
        )
    try:
        desired = queue_client.claim_edit(post_id)
    except queue_client.QueueUnavailable as e:
        raise RuntimeError(f"could not claim the canary edit: {e}") from e
    if desired is None:
        raise RuntimeError(
            "nothing pending for that post — edit it in the dashboard first"
        )

    account = ACCOUNTS_BY_NAME.get(desired["account"])
    if account is None:
        raise RuntimeError(f"unknown account {desired['account']!r}")

    photos = (
        queue_client.fetch_post_images(desired)
        if desired.get("image_set_managed") else []
    )
    result = editor.reconcile_post(
        account, desired, photos, dry_run=False, headless=headless
    )
    _emit_attempt(machine, result)
    # Push the evidence up immediately — the whole point of a canary is to look
    # at what happened, and waiting on the daemon's next cycle wastes the run.
    artifacts.flush_once(limit=20)
    return result
