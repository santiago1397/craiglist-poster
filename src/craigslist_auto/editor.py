"""Drive Craigslist's edit form: hydrate a live posting, then reconcile it.

DESIGN_EDITS.md decisions 23, 26, 32, 33, 36.

    !!  THE SELECTORS IN `SEL` BELOW ARE UNVERIFIED.  !!

    Nothing in this project has ever opened Craigslist's edit form. Every
    selector here is inferred from the postings-page DOM that `stats.py`
    documents plus the posting form in `poster.py`. Phase 0 of DESIGN_EDITS.md
    is a manual spike against one throwaway post whose entire job is to replace
    these with observed values.

    Until that spike is done, `edits_enabled` defaults FALSE on the server, so
    this module cannot run against a live post by accident.

The code is written so a wrong selector is loud and safe, never silent:
every step is timed and recorded, a mismatch raises `EditFailure` carrying the
step name, and the backend routes on that step to decide whether the edit
retries or parks (decision 32).

The one genuinely dangerous window is between removing the live images and
finishing the re-upload — a failure there leaves a live posting with no images.
`_replace_images` therefore attempts an in-session recovery before giving up,
and reports `degraded_live` if even that fails.
"""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from patchright.sync_api import Page

from . import artifacts
from .config import Account
from .human import human_type, read_pause, sleep_jitter
from .poster import launch_account
from .stats import CL_ACCOUNT_URL, LoginExpiredError

# ---------------------------------------------------------------------------
# UNVERIFIED SELECTORS — see the module docstring. Replace after the spike.
# ---------------------------------------------------------------------------

SEL = {
    # Account postings page — these two ARE observed (stats.py:205-224).
    "posting_row": "tr.posting-row",
    "row_status_cell": "td.status",
    # Everything below is inferred and must be confirmed by the spike.
    "row_edit_button": "td.buttons a:has-text('edit'), td.buttons button:has-text('edit')",
    "edit_title": "input[name='PostingTitle']",
    "edit_body": "textarea[name='PostingBody']",
    "edit_postal": "input[name='postal']",
    "edit_city": "input[name='city']",
    "edit_phone": "input[name='contact_phone']",
    "edit_license": "input[name='license_number']",
    # The edit form's continue/save control.
    "save_button": "button[type='submit'], input[type='submit']",
    # Image editor.
    "image_thumb": ".swatch, .thumb, li.thumb",
    "image_remove": "a:has-text('remove'), button:has-text('remove')",
    "file_input": "input[type='file']",
    "images_done": "button:has-text('done with images'), a:has-text('done with images')",
    # Confirmation that the edit landed.
    "publish_button": "button[name='go'], button:has-text('publish')",
}

# Steps that mutate nothing on Craigslist. Mirrors PRE_MUTATION_STEPS in
# backend/app/services/edits.py — keep the two in sync.
PRE_MUTATION_STEPS = frozenset({
    "launch", "lease", "login_check", "open_account_page", "find_post_row",
    "open_edit_form", "hydrate", "verify_hash", "diff",
})

HASHED_FIELDS = (
    "title", "body", "county", "city", "service_offered",
    "postal_code", "license_number", "phone_number",
)


class EditFailure(RuntimeError):
    """A step failed. `step` decides how the backend routes the desired state."""

    def __init__(self, step: str, original: BaseException | str, *, mutated: bool = False):
        super().__init__(f"edit failed at step {step!r}: {original}")
        self.step = step
        self.original = original
        self.mutated = mutated


def content_hash(fields: dict) -> str:
    """Stable digest of a posting's editable content (decision 26).

    Whitespace is normalised because Craigslist round-trips textareas with
    inconsistent trailing newlines; without that, a post would look "changed"
    every single time we hydrated it and every edit would park as stale.
    """
    parts = []
    for key in HASHED_FIELDS:
        value = (fields.get(key) or "").strip()
        value = "\n".join(line.rstrip() for line in value.splitlines())
        parts.append(f"{key}={value}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Step recorder — the breadcrumb trail shipped on PostEditAttempt
# ---------------------------------------------------------------------------

@dataclass
class StepLog:
    steps: list[dict] = field(default_factory=list)
    current: str | None = None
    _started: float = 0.0

    @contextmanager
    def step(self, name: str):
        self.current = name
        self._started = time.monotonic()
        logger.debug(f"edit step: {name}")
        try:
            yield
        except Exception:
            self._record(name, ok=False)
            raise
        else:
            self._record(name, ok=True)

    def _record(self, name: str, *, ok: bool, note: str | None = None) -> None:
        self.steps.append({
            "name": name,
            "ok": ok,
            "duration_seconds": round(time.monotonic() - self._started, 3),
            "note": note,
        })

    def note(self, name: str, note: str) -> None:
        self.steps.append({"name": name, "ok": True, "duration_seconds": None, "note": note})


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def _open_account_page(page: Page, account_name: str) -> None:
    page.goto(CL_ACCOUNT_URL, wait_until="domcontentloaded")
    sleep_jitter(2.0)
    if page.locator("input[type='password']").count() > 0:
        raise LoginExpiredError(f"account {account_name} is not logged in (url={page.url})")


def _find_row(page: Page, post_id: str):
    """Locate the postings-row for this post_id. Observed DOM (stats.py:211)."""
    row = page.locator(
        f"{SEL['posting_row']}:has({SEL['row_status_cell']}[data-postingid='{post_id}'])"
    )
    if row.count() == 0:
        # Fall back to the visible postingID column before declaring it gone.
        row = page.locator(f"{SEL['posting_row']}:has-text('{post_id}')")
    return row.first if row.count() > 0 else None


def _read_form(page: Page) -> dict:
    """Scrape the edit form's current values."""
    def _val(key: str) -> str:
        loc = page.locator(SEL[key])
        if loc.count() == 0:
            return ""
        try:
            return (loc.first.input_value() or "").strip()
        except Exception:
            return (loc.first.text_content() or "").strip()

    return {
        "title": _val("edit_title"),
        "body": _val("edit_body"),
        "postal_code": _val("edit_postal"),
        "city": _val("edit_city"),
        "phone_number": _val("edit_phone"),
        "license_number": _val("edit_license"),
        "county": "",
        "service_offered": "",
    }


def _live_images(page: Page) -> list[dict]:
    thumbs = page.locator(SEL["image_thumb"])
    out = []
    for i in range(thumbs.count()):
        src = None
        try:
            img = thumbs.nth(i).locator("img")
            if img.count() > 0:
                src = img.first.get_attribute("src")
        except Exception:
            pass
        out.append({"slot": i + 1, "url": src, "sha256": None})
    return out


# ---------------------------------------------------------------------------
# Hydration (decision 23)
# ---------------------------------------------------------------------------

def hydrate_post(
    account: Account, post_id: str, *, headless: bool = False
) -> dict:
    """Read a live posting's edit form. Never writes anything.

    Returns a dict shaped for the `post_content` event.
    """
    log = StepLog()
    artifact_ids: list[str] = []
    result: dict = {
        "post_id": post_id, "account": account.name, "ok": False,
        "steps": log.steps, "artifact_ids": artifact_ids,
    }

    with launch_account(
        account, headless=headless, flow="edit", lease_blocking=False
    ) as ctx:
        page = ctx.new_page()
        try:
            with log.step("open_account_page"):
                _open_account_page(page, account.name)

            with log.step("find_post_row"):
                row = _find_row(page, post_id)
                if row is None:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="post_row_missing",
                        post_id=post_id, account=account.name,
                    ))
                    result.update({
                        "error_type": "post_not_found",
                        "error_message": "no postings row matched this post_id",
                        "live_status": "gone", "editable": False,
                    })
                    return result

            with log.step("open_edit_form"):
                edit = row.locator(SEL["row_edit_button"])
                if edit.count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="no_edit_button",
                        post_id=post_id, account=account.name,
                    ))
                    result.update({
                        "error_type": "not_editable",
                        "error_message": "no edit control on this posting's row",
                        "editable": False,
                    })
                    return result
                edit.first.click()
                page.wait_for_load_state("domcontentloaded")
                read_pause(900)

            with log.step("hydrate"):
                if page.locator(SEL["edit_title"]).count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="edit_form_unrecognised",
                        post_id=post_id, account=account.name,
                    ))
                    result.update({
                        "error_type": "selector_miss",
                        "error_message": (
                            "edit form did not expose the expected title input — "
                            "selectors in editor.SEL need updating"
                        ),
                    })
                    return result
                fields = _read_form(page)
                images = _live_images(page)

            result.update(fields)
            result.update({
                "ok": True,
                "images": images,
                "content_hash": content_hash(fields),
                "live_status": "active",
                "editable": True,
            })
            logger.info(
                f"[{account.name}] hydrated post {post_id}: "
                f"{len(fields['body'])} body chars, {len(images)} image(s)"
            )
            return result

        except LoginExpiredError as e:
            result.update({"error_type": "login_expired", "error_message": str(e)})
            return result
        except Exception as e:
            logger.exception(f"hydrate failed for post {post_id}")
            artifact_ids.extend(artifacts.capture_page(
                page, flow="edit_hydrate", label="exception",
                post_id=post_id, account=account.name,
            ))
            result.update({"error_type": type(e).__name__, "error_message": str(e)[:500]})
            return result


# ---------------------------------------------------------------------------
# Reconcile (decisions 26, 32, 33, 36)
# ---------------------------------------------------------------------------

def _fill(page: Page, key: str, value: str) -> None:
    loc = page.locator(SEL[key])
    if loc.count() == 0:
        raise EditFailure(f"fill_{key}", f"no element matched {SEL[key]!r}")
    # Clear first: an edit replaces the field, and human_type only appends.
    loc.first.click()
    loc.first.fill("")
    human_type(loc.first, value)
    sleep_jitter(0.4, 0.2)


def _replace_images(
    page: Page, log: StepLog, photos: list[Path], account_name: str, post_id: str
) -> tuple[int, list[str]]:
    """Remove every live image, then upload `photos` in order (decision 33).

    This is the only window in the whole system where failing leaves a live
    posting worse than we found it, so a failure mid-upload triggers one
    in-session recovery attempt before surrendering.
    """
    artifact_ids: list[str] = []

    with log.step("images_remove"):
        removed = 0
        # Removing shrinks the list, so always act on the first remaining thumb.
        for _ in range(20):
            thumbs = page.locator(SEL["image_thumb"])
            if thumbs.count() == 0:
                break
            remove = thumbs.first.locator(SEL["image_remove"])
            if remove.count() == 0:
                artifact_ids.extend(artifacts.capture_page(
                    page, flow="edit_reconcile", label="no_remove_control",
                    post_id=post_id, account=account_name,
                ))
                raise EditFailure(
                    "images_remove",
                    "images are present but no remove control was found — "
                    "Craigslist may not permit removal, which invalidates "
                    "DESIGN_EDITS decision 33",
                    mutated=removed > 0,
                )
            remove.first.click()
            removed += 1
            sleep_jitter(0.6, 0.2)
        log.note("images_remove", f"removed {removed} live image(s)")

    try:
        with log.step("images_upload"):
            if photos:
                page.wait_for_selector(SEL["file_input"], timeout=30_000)
                file_input = page.locator(SEL["file_input"])
                for i, photo in enumerate(photos, 1):
                    file_input.first.set_input_files(str(photo))
                    logger.info(f"  [{i}/{len(photos)}] uploaded {photo.name}")
                    sleep_jitter(1.0, 0.4)
                final = page.locator(SEL["image_thumb"]).count()
                if final != len(photos):
                    raise EditFailure(
                        "images_upload",
                        f"expected {len(photos)} thumbnails, Craigslist shows {final}",
                        mutated=True,
                    )
    except EditFailure:
        raise
    except Exception as e:
        raise EditFailure("images_upload", e, mutated=True) from e

    return page.locator(SEL["image_thumb"]).count(), artifact_ids


def reconcile_post(
    account: Account,
    desired: dict,
    photos: list[Path],
    *,
    dry_run: bool = False,
    headless: bool = False,
) -> dict:
    """Make a live posting match `desired`.

    With `dry_run=True` this opens the form, hydrates, compares, and reports
    what *would* change without typing a character (decision 36) — deliberately
    unlike `cl post --dry-run`, because Craigslist commits image operations at
    selection time and a fill-then-abandon rehearsal could strip a live post.
    """
    post_id = desired["post_id"]
    log = StepLog()
    artifact_ids: list[str] = []
    started = time.monotonic()

    def _result(outcome: str, **extra) -> dict:
        out = {
            "post_id": post_id,
            "account": account.name,
            "outcome": outcome,
            "desired_rev": desired.get("desired_rev"),
            "duration_seconds": round(time.monotonic() - started, 2),
            "steps": log.steps,
            "artifact_ids": artifact_ids,
        }
        out.update(extra)
        return out

    with launch_account(
        account, headless=headless, flow="edit", lease_blocking=False
    ) as ctx:
        page = ctx.new_page()
        try:
            with log.step("open_account_page"):
                _open_account_page(page, account.name)

            with log.step("find_post_row"):
                row = _find_row(page, post_id)
                if row is None:
                    return _result(
                        "failed_gone", failed_step="find_post_row",
                        error_type="post_not_found",
                        error_message="posting is no longer listed on the account page",
                    )

            with log.step("open_edit_form"):
                edit = row.locator(SEL["row_edit_button"])
                if edit.count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="no_edit_button",
                        post_id=post_id, account=account.name,
                    ))
                    return _result(
                        "failed_gone", failed_step="open_edit_form",
                        error_type="not_editable",
                        error_message="posting has no edit control",
                    )
                edit.first.click()
                page.wait_for_load_state("domcontentloaded")
                read_pause(900)

            with log.step("hydrate"):
                if page.locator(SEL["edit_title"]).count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="edit_form_unrecognised",
                        post_id=post_id, account=account.name,
                    ))
                    return _result(
                        "failed_form", failed_step="hydrate",
                        error_type="selector_miss",
                        error_message="edit form did not expose the expected inputs",
                    )
                live = _read_form(page)
                live_images = _live_images(page)

            with log.step("verify_hash"):
                live_hash = content_hash(live)
                base = desired.get("base_hash")
                if base and live_hash != base:
                    # Decision 26: never clobber a change the operator never saw.
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="stale",
                        post_id=post_id, account=account.name,
                    ))
                    return _result(
                        "failed_stale", failed_step="verify_hash",
                        error_type="content_moved",
                        error_message=(
                            "the live posting changed since you edited it "
                            f"(expected {base[:12]}, found {live_hash[:12]})"
                        ),
                        images_live_count=len(live_images),
                        images_desired_count=len(photos),
                    )

            with log.step("diff"):
                text_changes = {
                    k: desired.get(k, "") for k in HASHED_FIELDS
                    if (desired.get(k) or "").strip() != (live.get(k) or "").strip()
                }
                manage_images = bool(desired.get("image_set_managed"))
                images_differ = manage_images and len(photos) != len(live_images)
                log.note("diff", f"{len(text_changes)} text field(s), images={images_differ}")

            if not text_changes and not images_differ:
                return _result(
                    "no_change", applied_rev=desired.get("desired_rev"),
                    images_live_count=len(live_images),
                    images_desired_count=len(photos),
                )

            if dry_run:
                artifact_ids.extend(artifacts.capture_page(
                    page, flow="edit_reconcile", label="dry_run",
                    post_id=post_id, account=account.name,
                ))
                logger.info(
                    f"[{account.name}] DRY RUN post {post_id}: would change "
                    f"{sorted(text_changes)}; images {len(live_images)} -> "
                    f"{len(photos) if manage_images else len(live_images)}"
                )
                return _result(
                    "dry_run",
                    images_live_count=len(live_images),
                    images_desired_count=len(photos) if manage_images else len(live_images),
                )

            # ---------------- mutation begins here ----------------
            for key in ("title", "body"):
                if key in text_changes:
                    with log.step(f"fill_{key}"):
                        _fill(page, f"edit_{key}", desired[key])
            for key, sel_key in (
                ("postal_code", "edit_postal"), ("city", "edit_city"),
                ("phone_number", "edit_phone"), ("license_number", "edit_license"),
            ):
                if key in text_changes and page.locator(SEL[sel_key]).count() > 0:
                    with log.step(f"fill_{key}"):
                        _fill(page, sel_key, desired[key])

            final_images = len(live_images)
            if manage_images:
                try:
                    final_images, extra = _replace_images(
                        page, log, photos, account.name, post_id
                    )
                    artifact_ids.extend(extra)
                except EditFailure as e:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label=f"images_failed_{e.step}",
                        post_id=post_id, account=account.name,
                    ))
                    if not e.mutated:
                        return _result(
                            "failed_images", failed_step=e.step,
                            error_type="image_error", error_message=str(e)[:500],
                            images_live_count=len(live_images),
                            images_desired_count=len(photos),
                        )
                    # Decision 32: images were already removed. Try once more to
                    # put the desired set back before leaving a bare posting.
                    logger.error(
                        f"[{account.name}] post {post_id} images mutated then failed — "
                        f"attempting in-session recovery"
                    )
                    recovered = _attempt_image_recovery(page, photos)
                    if not recovered:
                        return _result(
                            "degraded_live", failed_step=e.step,
                            error_type="image_error_unrecovered",
                            error_message=(
                                f"{e} — images were removed and could not be "
                                f"restored; this posting is live with "
                                f"{page.locator(SEL['image_thumb']).count()} image(s)"
                            )[:500],
                            images_live_count=page.locator(SEL["image_thumb"]).count(),
                            images_desired_count=len(photos),
                        )
                    log.note("images_recovered", "re-uploaded after a failed replace")
                    final_images = page.locator(SEL["image_thumb"]).count()

            with log.step("save"):
                done = page.locator(SEL["images_done"])
                if done.count() > 0:
                    done.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    sleep_jitter(1.2)
                save = page.locator(SEL["save_button"])
                publish = page.locator(SEL["publish_button"])
                target = publish if publish.count() > 0 else save
                if target.count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="no_save_control",
                        post_id=post_id, account=account.name,
                    ))
                    raise EditFailure("save", "no save/publish control found", mutated=True)
                target.first.click()
                page.wait_for_load_state("domcontentloaded")
                sleep_jitter(2.0)

            logger.info(f"[{account.name}] applied edit to post {post_id}")
            return _result(
                "applied", applied_rev=desired.get("desired_rev"),
                images_live_count=final_images,
                images_desired_count=len(photos) if manage_images else final_images,
            )

        except LoginExpiredError as e:
            return _result(
                "failed_login", failed_step="login_check",
                error_type="login_expired", error_message=str(e),
            )
        except EditFailure as e:
            artifact_ids.extend(artifacts.capture_page(
                page, flow="edit_reconcile", label=f"failed_{e.step}",
                post_id=post_id, account=account.name,
            ))
            outcome = "failed_form" if e.step in PRE_MUTATION_STEPS or not e.mutated else "degraded_live"
            return _result(
                outcome, failed_step=e.step,
                error_type="edit_failure", error_message=str(e)[:500],
            )
        except Exception as e:
            logger.exception(f"reconcile raised for post {post_id}")
            artifact_ids.extend(artifacts.capture_page(
                page, flow="edit_reconcile", label="exception",
                post_id=post_id, account=account.name,
            ))
            step = log.current or "unknown"
            return _result(
                "failed_other" if step in PRE_MUTATION_STEPS else "degraded_live",
                failed_step=step,
                error_type=type(e).__name__, error_message=str(e)[:500],
            )


def _attempt_image_recovery(page: Page, photos: list[Path]) -> bool:
    """Last-ditch re-upload after a failed image replace (decision 32)."""
    if not photos:
        return True
    try:
        page.wait_for_selector(SEL["file_input"], timeout=15_000)
        file_input = page.locator(SEL["file_input"])
        for photo in photos:
            file_input.first.set_input_files(str(photo))
            sleep_jitter(1.0, 0.4)
        ok = page.locator(SEL["image_thumb"]).count() == len(photos)
        logger.info(f"image recovery {'succeeded' if ok else 'did not restore full set'}")
        return ok
    except Exception as e:
        logger.error(f"image recovery failed outright: {e}")
        return False
