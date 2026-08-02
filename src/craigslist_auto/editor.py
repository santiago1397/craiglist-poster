"""Drive Craigslist's edit form: hydrate a live posting, then reconcile it.

DESIGN_EDITS.md decisions 23, 26, 32, 33, 36.

    Every selector in `SEL` is now OBSERVED against the real pages, captured by
    hydration runs against a live posting on 2026-07-31. Each block cites the
    artifact it came from.

    Editing is a hub, not a form. `/k/<token>` is a preview carrying a control
    bar; the three sub-pages hang off it as plain GETs:

        ?s=edit        the copy       (form#postingForm)
        ?s=geoverify   the location   (not driven yet)
        ?s=editimage   the gallery    (figure.imgbox, AJAX delete + upload)

    Nothing reaches the live posting until the hub's publish. Every change
    before that is a draft Craigslist will throw away on "cancel edit", which is
    what makes reading safe and what bounds the damage from a failed reconcile.

    The copy page has been submitted for real (2026-08-02). Replacing a gallery
    and pressing publish still have not run end to end, so treat those two as
    observed-but-unexercised.

    `edits_enabled` is TRUE on the server as of migration 0015, so this module
    CAN reach a live posting. What keeps that safe is that failure is loud: the
    diff step refuses the whole attempt before typing anything if a field it was
    asked to change has no control, and every run records a per-selector census
    on its step trail.

The code is written so a wrong selector is loud and safe, never silent:
every step is timed and recorded, a mismatch raises `EditFailure` carrying the
step name, and the backend routes on that step to decide whether the edit
retries or parks (decision 32).

The one genuinely dangerous window is between removing the images and finishing
the re-upload. `_replace_images` attempts an in-session recovery before giving
up, and reports `degraded_live` if even that fails — but only when this run
actually removed something. Claiming it unconditionally made a failure to find
the upload control indistinguishable from a stripped gallery, which is the
difference between "retry it" and "go and look at the ad right now".
"""
from __future__ import annotations

import hashlib
import os
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
    # --- OBSERVED on the real account page (artifact 5de988ff, 2026-07-31) ---
    "posting_row": "tr.posting-row",
    "row_status_cell": "td.status",
    # Each action is its own form, and the control is an <input type=submit>
    # whose label lives in `value` — not a link and not a <button>:
    #
    #   <form action="https://post.craigslist.org/manage/<token>" method="POST"
    #         class="manage edit">
    #     <input type="hidden" name="action" value="edit">
    #     <input type="hidden" name="crypt" value="...">
    #     <input type="submit" name="go" value="edit" class="managebtn">
    #   </form>
    #
    # The previous guess used `:has-text('edit')`, which can never match an
    # input — its label is an attribute, not text. Note the POST and the
    # per-posting `crypt`: the edit form cannot be reached by building a URL,
    # the control has to be clicked.
    "row_edit_button": (
        "td.buttons form.manage.edit input[type='submit'], "
        "td.buttons input.managebtn[value='edit']"
    ),
    # --- OBSERVED: the hub (artifact 6039e4cc, 2026-07-31) ---
    #
    # Clicking "edit" does NOT open a form. It opens /k/<token>, a preview of
    # the posting with a control bar:
    #
    #   <div class="draft_warning">
    #     press "publish" to apply edits
    #     <form action="/k/<token>" method="post" id="publish_top">
    #       <input type="hidden" name="cryptedStepCheck" value="...">
    #       <input type="hidden" name="continue" value="y">
    #       <button name="go" value="Continue">publish</button>
    #     </form>
    #   </div>
    #   <div class="preview-edit-buttons">
    #     <button onclick="...'/k/<token>?s=edit'">edit post</button>
    #     <button onclick="...'/k/<token>?s=geoverify'">edit location</button>
    #     <button onclick="...'/k/<token>?s=editimage'">edit images</button>
    #     <form ...><button name="discard" value="y">cancel edit</button></form>
    #   </div>
    #
    # Two things matter. Editing is a **hub with three sub-pages**, not one form
    # — so the old model, "open the form and fill everything in", was wrong
    # shape as well as wrong selectors. And the sub-pages are plain GET URLs, so
    # they are navigated to rather than clicked; only publish and cancel are
    # POSTs carrying `cryptedStepCheck`.
    #
    # Nothing reaches the live posting until `publish`. That is what makes
    # reading the form safe, and it is why hydration never touches these two.
    "hub_marker": ".draft_warning, .preview-edit-buttons",
    "hub_publish": "form#publish_top button[name='go']",
    "hub_cancel": "button[name='discard']",
    # --- OBSERVED: the copy sub-page, ?s=edit (artifact 123c1662, 2026-07-31).
    # --- Title "south florida | posting details", one <form id="postingForm">
    # --- POSTing to /k/<token>. These five were guesses that happened to be
    # --- right; the census confirmed each at exactly 1.
    "edit_title": "input[name='PostingTitle']",
    "edit_body": "textarea[name='PostingBody']",
    "edit_postal": "input[name='postal']",
    "edit_city": "input[name='city']",
    "edit_phone": "input[name='contact_phone']",
    # Not `license_number` — that name does not exist here. The form carries a
    # `has_license` radio pair plus this free-text field.
    "edit_license": "input[name='license_info']",
    # The save control, and the reason it has to be named this precisely.
    #
    # `button[type=submit], input[type=submit]` matched THREE elements on this
    # page, in this document order:
    #
    #   1. button[name=go][value=continue]        — save
    #   2. button[name=discard][value=y]          — cancel edit, discards it all
    #   3. button[name=submit][value=feedback]    — the feedback widget
    #
    # Everything here takes `.first`, so that selector was one DOM reordering
    # away from discarding the operator's edit instead of saving it.
    "save_button": "form#postingForm button[name='go'][value='continue']",
    # --- OBSERVED: the image sub-page, ?s=editimage (artifact 8d5419f4).
    # --- Title "south florida | choose images". One figure per image:
    # ---
    # ---   <div class="images ui-sortable">
    # ---     <figure class="imgbox" id="imgID_3:...">
    # ---       <form class="delete ajax" action="/k/<token>" method="post">
    # ---         <button class="imgbox-delete-image" name="go">X</button>
    # ---       </form>
    # ---       <button class="imgbox-rotate-image">…</button>
    # ---       <div class="imgwrap ui-sortable-handle"><img src="..."></div>
    # ---     </figure>
    # ---   </div>
    # ---
    # --- Deleting is AJAX — the figure leaves the DOM without a page load — so
    # --- the replace loop waits on the count rather than on a fixed sleep.
    # --- `ui-sortable` also answers DESIGN_EDITS spike question 2: images can be
    # --- reordered by drag. Full replace does not need it, but it is not a
    # --- constraint we are working around.
    # `:not(.template)` is load-bearing. The gallery ships a 25th, hidden
    # `<figure class="imgbox template">` with no id and no <img>, which the
    # uploader clones for each new image. Counting it would have been the first
    # real write's undoing twice over: the delete loop would have tried to
    # remove a prototype and never reached zero, and the upload assertion
    # `thumbnails == len(photos)` would have read 25 against 24 — both of which
    # raise with mutated=True, i.e. `degraded_live`, after the 24 real images
    # had already been deleted.
    "image_thumb": "figure.imgbox:not(.template)",
    # Deliberately unscoped: the replace loop resolves it *inside* a thumb
    # (`thumb.locator(image_remove)`), so scoping it to the figure here would
    # look for a figure within a figure. It therefore counts one higher than
    # `image_thumb` in the census — that gap is the template, not a fault.
    "image_remove": "button.imgbox-delete-image",
    # Hidden by design (opacity 0, absolutely positioned) with a generated id,
    # so it is matched on type and driven with set_input_files rather than by
    # clicking "Add Images" — which is itself disabled while the 24 slots are
    # full, and only becomes usable once the deletes have run.
    "file_input": "input[type='file']",
    "images_done": "button.done",
    # Confirmation that the edit landed.
    "publish_button": (
        "button[name='go'], button:has-text('publish'), "
        "input[type='submit'][name='go'], input[type='submit'][value*='publish' i]"
    ),
}

# Steps that mutate nothing on Craigslist. Mirrors PRE_MUTATION_STEPS in
# backend/app/services/edits.py — keep the two in sync.
#
# `unsupported_field` is deliberately NOT here, even though it is raised before
# a single character is typed. `apply_attempt` routes an unmapped outcome with a
# pre-mutation step back to 'pending', which for a selector that will never
# match means retrying every 15 seconds forever. Excluding it parks the edit
# instead, with the offending selector named in `failed_message`, which is the
# only form of this failure anyone can act on.
PRE_MUTATION_STEPS = frozenset({
    "launch", "lease", "login_check", "open_account_page", "find_post_row",
    "open_edit_form", "open_edit_step", "read_images", "hydrate", "verify_hash",
    "diff",
})

# What the content hash covers: the fields the edit form actually exposes.
#
# `county` and `service_offered` used to be in here. Craigslist's edit form has
# no control for either — `_read_form` could only ever return "" — so they
# contributed a constant to every hash while making the diff believe they were
# editable content. A change to one registered as a text change, started the
# mutation pass, and reported `applied` having typed nothing.
HASHED_FIELDS = (
    "title", "body", "city",
    "postal_code", "license_number", "phone_number",
)

# Which selector backs each editable field. The pre-flight check in `diff` uses
# this to refuse a change it has no way to make, rather than silently skipping
# it and calling the result a success.
FIELD_SEL = {
    "title": "edit_title",
    "body": "edit_body",
    "postal_code": "edit_postal",
    "city": "edit_city",
    "phone_number": "edit_phone",
    "license_number": "edit_license",
}

# Emitted once per run, on the attempt's step trail. Every selector below is
# unverified, so "which one matched nothing" is the first question of every
# failure — and a count of 2 is as wrong as a count of 0, because `_fill` and
# the click helpers all take `.first`.
CENSUS_KEYS = (
    "posting_row", "row_edit_button",
    "hub_marker", "hub_publish", "hub_cancel",
    "edit_title", "edit_body", "edit_postal",
    "edit_city", "edit_phone", "edit_license", "save_button", "publish_button",
    "file_input", "image_thumb", "image_remove", "images_done",
)

# Craigslist's edit sub-pages, reachable as plain GETs off the hub URL.
HUB_STEP_PREVIEW = "preview"
HUB_STEP_EDIT = "edit"
HUB_STEP_LOCATION = "geoverify"
HUB_STEP_IMAGES = "editimage"


def is_edit_session(url: str) -> bool:
    """Is this URL inside Craigslist's edit wizard at all?

    The wizard is /k/<token>, with the step in `?s=`. Which step you land on is
    not ours to predict: a fresh edit opens at `?s=preview`, but an edit session
    left open resumes wherever it got to. Requiring the preview turned a
    resumable draft into a hard failure.
    """
    return "/k/" in url


def hub_step_url(hub_url: str, step: str) -> str:
    """The URL of one edit sub-page, given the hub we landed on.

    The hub renders these as `onclick="window.location.href=..."` buttons, so
    navigating is exactly what clicking one would do — and it does not depend on
    a selector that can drift.
    """
    base = hub_url.split("?")[0].split("#")[0]
    return f"{base}?s={step}"

# Capture screenshots and HTML on success too, not only on failure. The most
# useful artifact in this project right now is a healthy edit form: it is the
# DOM needed to replace the guesses in SEL. Off by default so steady-state
# upload volume stays flat once the selectors are settled.
TRACE = os.environ.get("CL_EDIT_TRACE", "").strip().lower() in ("1", "true", "yes")


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


def _url_token(url: str | None) -> str | None:
    """The distinctive last path segment of a posting URL.

    For Craigslist's current share form — /view/d/<slug>/<token> — this is the
    base62 token. For the older /d/<slug>/<digits>.html form it is the numeric
    id with the extension stripped.
    """
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    if tail.endswith(".html"):
        tail = tail[: -len(".html")]
    # Anything short enough to collide with an unrelated href is not worth
    # matching on.
    return tail if len(tail) >= 8 else None


def _find_row(page: Page, post_id: str, url: str | None = None):
    """Locate the postings-row for this post. Observed DOM (stats.py:211).

    Three ways in, because `post_id` is not always the account page's id.
    `stats.py` derives it from the posting URL, and Craigslist's current share
    form carries a base62 token rather than the numeric posting id — so a post
    recorded straight from a publish can hold something `data-postingid` will
    never equal. The row is still there; it just has to be found by its link.
    """
    row = page.locator(
        f"{SEL['posting_row']}:has({SEL['row_status_cell']}[data-postingid='{post_id}'])"
    )
    if row.count() > 0:
        return row.first

    token = _url_token(url)
    if token:
        row = page.locator(f'{SEL["posting_row"]}:has(a[href*="{token}"])')
        if row.count() > 0:
            logger.info(f"matched post {post_id} by its URL token rather than its id")
            return row.first

    # Fall back to the visible postingID column before declaring it gone.
    row = page.locator(f"{SEL['posting_row']}:has-text('{post_id}')")
    return row.first if row.count() > 0 else None


def _row_posting_id(row) -> str | None:
    """The numeric id Craigslist itself uses for a matched row.

    Worth reporting whenever it differs from what we stored: that difference is
    the whole reason a row can be present and still not be found by id.
    """
    try:
        cell = row.locator(f"{SEL['row_status_cell']}[data-postingid]")
        if cell.count() == 0:
            return None
        return cell.first.get_attribute("data-postingid")
    except Exception:  # pragma: no cover — defensive
        return None


def _count(page: Page, key: str) -> int:
    """How many elements this selector matches, logged.

    Every lookup goes through here so a failure names the selector that missed
    instead of surfacing as a bare timeout three steps later.
    """
    n = page.locator(SEL[key]).count()
    logger.debug(f"selector {key}={SEL[key]!r} matched {n}")
    return n


def _census(page: Page) -> dict[str, int]:
    return {k: _count(page, k) for k in CENSUS_KEYS}


def _read_form(page: Page) -> dict:
    """Scrape the edit form's current values.

    `county` and `service_offered` are absent rather than "": the form exposes
    no control for them, and reporting an empty string reads as "Craigslist says
    it is blank" — which then overwrote what the posting flow recorded at
    publish time. Omitting them lets the server keep the values it already has.
    """
    def _val(key: str) -> str:
        if _count(page, key) == 0:
            return ""
        loc = page.locator(SEL[key])
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
    account: Account, post_id: str, *, url: str | None = None, headless: bool = False
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
                rows = _count(page, "posting_row")
                log.note("account_page", f"posting_row={rows}")

            with log.step("find_post_row"):
                row = _find_row(page, post_id, url)
                if row is None:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="post_row_missing",
                        post_id=post_id, account=account.name,
                    ))
                    result.update({
                        "error_type": "post_not_found",
                        "error_message": (
                            f"no postings row matched {post_id!r}. The account "
                            f"page listed {_count(page, 'posting_row')} posting(s) "
                            f"— if the ad is visibly there, the id we hold is not "
                            f"the one Craigslist uses for it."
                        ),
                        "live_status": "gone", "editable": False,
                    })
                    return result
                real_id = _row_posting_id(row)
                if real_id and real_id != post_id:
                    log.note("post_id", f"craigslist calls this {real_id}")
                    logger.warning(
                        f"post {post_id} is {real_id} on the account page — "
                        f"stored id came from the URL, not from data-postingid"
                    )
                result["craigslist_post_id"] = real_id

            with log.step("open_edit_form"):
                edit = row.locator(SEL["row_edit_button"])
                if edit.count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="no_edit_button",
                        post_id=post_id, account=account.name,
                    ))
                    # Craigslist drops the edit control from rows it will not
                    # let you edit — expired, deleted, some paid categories. A
                    # page-wide count separates that from a broken selector: if
                    # no row anywhere has one, the selector is the problem.
                    page_wide = _count(page, "row_edit_button")
                    log.note("edit_control", f"row=0 page={page_wide}")
                    result.update({
                        "error_type": "not_editable",
                        "error_message": (
                            "no edit control on this posting's row"
                            + (
                                f"; {page_wide} other posting(s) on the page do "
                                f"have one, so Craigslist is not offering to edit "
                                f"this ad — check whether it has expired or been "
                                f"deleted"
                                if page_wide
                                else "; no posting on the page has one either, so "
                                     "editor.SEL['row_edit_button'] no longer "
                                     "matches Craigslist's markup"
                            )
                        ),
                        "editable": False,
                    })
                    return result
                edit.first.click()
                page.wait_for_load_state("domcontentloaded")
                read_pause(900)
                landed = page.url
                # Keep the base, not the landing URL: every later step is
                # addressed as `?s=<step>` off it, and an edit session left open
                # resumes at whatever step it reached last.
                hub_url = landed.split("?")[0].split("#")[0]
                log.note("hub", landed)
                if not is_edit_session(landed):
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="hub_unrecognised",
                        post_id=post_id, account=account.name,
                    ))
                    result.update({
                        "error_type": "selector_miss",
                        "error_message": (
                            f"clicking edit landed on {landed}, which is not "
                            f"Craigslist's edit wizard"
                        ),
                    })
                    return result

            # The hub is a preview, not a form. The copy lives one GET away.
            with log.step("open_edit_step"):
                page.goto(
                    hub_step_url(hub_url, HUB_STEP_EDIT), wait_until="domcontentloaded"
                )
                read_pause(900)


            with log.step("hydrate"):
                # The census lands on the step trail, so the dashboard's History
                # shows which selectors matched without anyone downloading an
                # artifact. With every selector unverified that is the
                # difference between a five-minute fix and an afternoon.
                census = _census(page)
                log.note("selectors", " ".join(f"{k}={v}" for k, v in census.items()))
                logger.info(f"[{account.name}] selector census post {post_id}: {census}")
                if census["edit_title"] == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="edit_form_unrecognised",
                        post_id=post_id, account=account.name,
                    ))
                    result.update({
                        "error_type": "selector_miss",
                        "error_message": (
                            f"the edit form at {page.url} did not expose the "
                            f"expected title input — selectors in editor.SEL "
                            f"need updating"
                        ),
                    })
                    return result
                fields = _read_form(page)
                if TRACE:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="form_loaded",
                        post_id=post_id, account=account.name,
                    ))

            # Images live on their own sub-page. Reading them from the copy page
            # reported every posting as having none, which would have made the
            # reconcile diff believe it was adding a gallery to a bare ad rather
            # than replacing one.
            with log.step("read_images"):
                page.goto(
                    hub_step_url(hub_url, HUB_STEP_IMAGES),
                    wait_until="domcontentloaded",
                )
                read_pause(900)
                images = _live_images(page)
                log.note(
                    "images",
                    f"image_thumb={_count(page, 'image_thumb')} "
                    f"file_input={_count(page, 'file_input')} "
                    f"image_remove={_count(page, 'image_remove')}",
                )
                if TRACE or not images:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_hydrate", label="images_loaded",
                        post_id=post_id, account=account.name,
                    ))

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


# Craigslist accepts 24 images, and the replace loop deletes then re-uploads,
# so the ceiling on either half is that plus slack for a retry.
MAX_IMAGE_OPS = 30

# How long to wait for the gallery to catch up with a delete or an upload. Both
# are asynchronous; uploads are the slow one, and a full 24-image set on a
# domestic connection is minutes of real work.
THUMB_TIMEOUT_S = 45


def _wait_for_thumb_count(page: Page, expected: int, timeout_s: float = None) -> bool:
    """Wait until the gallery shows exactly `expected` thumbnails."""
    deadline = time.monotonic() + (timeout_s or THUMB_TIMEOUT_S)
    while time.monotonic() < deadline:
        if page.locator(SEL["image_thumb"]).count() == expected:
            return True
        page.wait_for_timeout(400)
    return False


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
        # The delete form is `class="delete ajax"` — the figure leaves the DOM
        # with no page load — so each pass waits for the count to actually drop
        # instead of sleeping and hoping. A fixed sleep here either raced the
        # request or made a 24-image replace needlessly slow.
        for _ in range(MAX_IMAGE_OPS):
            before = page.locator(SEL["image_thumb"]).count()
            if before == 0:
                break
            remove = page.locator(SEL["image_thumb"]).first.locator(SEL["image_remove"])
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
            if not _wait_for_thumb_count(page, before - 1):
                raise EditFailure(
                    "images_remove",
                    f"deleted an image but the gallery still shows {before}"
                    f" after {THUMB_TIMEOUT_S}s",
                    mutated=True,
                )
            removed += 1
            sleep_jitter(0.4, 0.15)
        log.note("images_remove", f"removed {removed} live image(s)")

    try:
        with log.step("images_upload"):
            if photos:
                page.wait_for_selector(SEL["file_input"], state="attached", timeout=30_000)
                file_input = page.locator(SEL["file_input"])
                for i, photo in enumerate(photos, 1):
                    file_input.first.set_input_files(str(photo))
                    # The uploader is asynchronous: the thumbnail appears when
                    # Craigslist has accepted the bytes, which on a 24-image
                    # posting is the difference between a real wait and a
                    # guess. Mirrors poster._wait_for_thumb_increment.
                    if not _wait_for_thumb_count(page, i):
                        raise EditFailure(
                            "images_upload",
                            f"uploaded {photo.name} ({i}/{len(photos)}) but only "
                            f"{page.locator(SEL['image_thumb']).count()} thumbnail(s) "
                            f"appeared within {THUMB_TIMEOUT_S}s",
                            mutated=True,
                        )
                    logger.info(f"  [{i}/{len(photos)}] uploaded {photo.name}")
                    sleep_jitter(0.6, 0.25)
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
        # `mutated` only if this run actually took something away. Reporting it
        # unconditionally turned "could not find the upload control" into
        # `degraded_live` — the loudest alarm the system has — on a posting
        # whose 24 images had never been touched.
        raise EditFailure("images_upload", e, mutated=removed > 0) from e

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
                rows = _count(page, "posting_row")
                log.note("account_page", f"posting_row={rows}")

            with log.step("find_post_row"):
                row = _find_row(page, post_id, desired.get("url"))
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
                    page_wide = _count(page, "row_edit_button")
                    log.note("edit_control", f"row=0 page={page_wide}")
                    return _result(
                        "failed_gone", failed_step="open_edit_form",
                        error_type="not_editable",
                        error_message=(
                            f"posting has no edit control ({page_wide} other "
                            f"posting(s) on the page do)"
                            if page_wide else
                            "no posting on the page has an edit control — "
                            "editor.SEL['row_edit_button'] no longer matches"
                        ),
                    )
                edit.first.click()
                page.wait_for_load_state("domcontentloaded")
                read_pause(900)
                landed = page.url
                # Keep the base, not the landing URL: every later step is
                # addressed as `?s=<step>` off it, and an edit session left open
                # resumes at whatever step it reached last.
                hub_url = landed.split("?")[0].split("#")[0]
                log.note("hub", landed)
                if not is_edit_session(landed):
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="hub_unrecognised",
                        post_id=post_id, account=account.name,
                    ))
                    return _result(
                        "failed_form", failed_step="open_edit_form",
                        error_type="selector_miss",
                        error_message=(
                            f"clicking edit landed on {landed}, which is not "
                            f"Craigslist's edit wizard"
                        ),
                    )

            # The hub is a preview, not a form. The copy lives one GET away.
            with log.step("open_edit_step"):
                page.goto(
                    hub_step_url(hub_url, HUB_STEP_EDIT), wait_until="domcontentloaded"
                )
                read_pause(900)


            with log.step("hydrate"):
                census = _census(page)
                log.note("selectors", " ".join(f"{k}={v}" for k, v in census.items()))
                logger.info(f"[{account.name}] selector census post {post_id}: {census}")
                if census["edit_title"] == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="edit_form_unrecognised",
                        post_id=post_id, account=account.name,
                    ))
                    return _result(
                        "failed_form", failed_step="hydrate",
                        error_type="selector_miss",
                        error_message=(
                            f"the edit form at {page.url} did not expose the "
                            f"expected inputs"
                        ),
                    )
                live = _read_form(page)

            manage_images = bool(desired.get("image_set_managed"))
            live_images: list[dict] = []
            if manage_images:
                # The copy page carries no gallery — counting thumbnails there
                # reported every posting as having none, which would have made
                # the diff believe it was adding a gallery to a bare ad rather
                # than replacing one. Read it where it lives, then come back, so
                # the fill loop and its pre-flight run on the copy page.
                with log.step("read_images"):
                    page.goto(
                        hub_step_url(hub_url, HUB_STEP_IMAGES),
                        wait_until="domcontentloaded",
                    )
                    read_pause(700)
                    live_images = _live_images(page)
                    log.note("images", f"live={len(live_images)} desired={len(photos)}")
                    page.goto(
                        hub_step_url(hub_url, HUB_STEP_EDIT),
                        wait_until="domcontentloaded",
                    )
                    read_pause(700)
                if TRACE:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="form_loaded",
                        post_id=post_id, account=account.name,
                    ))

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
                images_differ = manage_images and len(photos) != len(live_images)
                log.note("diff", f"{len(text_changes)} text field(s), images={images_differ}")

                # Refuse a change we have no control to make, *before* typing
                # anything. Without this the fill loop skipped the field, the
                # run clicked save, and the attempt came back `applied` with
                # `live_rev` advanced — the operator's change recorded as done,
                # forever, having done nothing. Running here keeps the posting
                # untouched, so this is a clean failure rather than a partial
                # edit.
                unreachable = sorted(k for k in text_changes if _count(page, FIELD_SEL[k]) == 0)
                if unreachable:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="unsupported_field",
                        post_id=post_id, account=account.name,
                    ))
                    return _result(
                        "failed_form", failed_step="unsupported_field",
                        error_type="field_not_reachable",
                        error_message=(
                            "cannot edit " + ", ".join(unreachable)
                            + " — no element matched "
                            + ", ".join(f"{k}={SEL[FIELD_SEL[k]]!r}" for k in unreachable)
                        )[:500],
                        images_live_count=len(live_images),
                        images_desired_count=len(photos),
                    )

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
            # Say what is about to change before changing it. The dry-run branch
            # logged its plan; the real path logged nothing until after the fact,
            # which is the wrong way round when something goes wrong mid-edit.
            logger.info(
                f"[{account.name}] applying to post {post_id}: "
                f"fields={sorted(text_changes)} images="
                f"{len(live_images)}->"
                f"{len(photos) if manage_images else len(live_images)}"
            )
            # Every field here is known reachable — the diff step refused the
            # whole attempt otherwise — so `_fill` raising is a genuine surprise
            # rather than the ordinary "this form has no phone box" case.
            for key in ("title", "body"):
                if key in text_changes:
                    with log.step(f"fill_{key}"):
                        _fill(page, f"edit_{key}", desired[key])
            for key, sel_key in (
                ("postal_code", "edit_postal"), ("city", "edit_city"),
                ("phone_number", "edit_phone"), ("license_number", "edit_license"),
            ):
                if key in text_changes:
                    with log.step(f"fill_{key}"):
                        _fill(page, sel_key, desired[key])

            # Submitting the copy sub-page returns to the hub; it does not
            # publish. Nothing reaches the live posting until the hub's publish.
            with log.step("submit_copy"):
                save = page.locator(SEL["save_button"])
                if _count(page, "save_button") == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="no_save_control",
                        post_id=post_id, account=account.name,
                    ))
                    raise EditFailure(
                        "submit_copy", f"no submit control on {page.url}", mutated=True
                    )
                save.first.click()
                page.wait_for_load_state("domcontentloaded")
                sleep_jitter(1.2)

            final_images = len(live_images)
            # `images_differ`, not `manage_images`. Taking control of a gallery
            # is not a reason to delete and re-upload one that already matches:
            # gated on the wrong flag, a one-word title change tore down 24
            # images and put them back, which is 48 avoidable operations against
            # a live posting and 48 chances to fail halfway.
            if manage_images and images_differ:
                try:
                    with log.step("open_images_step"):
                        # Via the hub. Going straight from the copy page to
                        # ?s=editimage got redirected back to ?s=edit, so the
                        # replace ran against a page with no gallery on it.
                        page.goto(
                            hub_step_url(hub_url, HUB_STEP_PREVIEW),
                            wait_until="domcontentloaded",
                        )
                        read_pause(500)
                        page.goto(
                            hub_step_url(hub_url, HUB_STEP_IMAGES),
                            wait_until="domcontentloaded",
                        )
                        read_pause(900)
                        # BUG 3's guard: refuse to touch anything unless this is
                        # really the gallery. Craigslist redirects out of the
                        # image step in states we do not fully understand yet,
                        # and every operation past here is destructive.
                        if _count(page, "file_input") == 0:
                            artifact_ids.extend(artifacts.capture_page(
                                page, flow="edit_reconcile",
                                label="images_step_not_reached",
                                post_id=post_id, account=account.name,
                            ))
                            raise EditFailure(
                                "open_images_step",
                                f"asked for the image step and landed on "
                                f"{page.url} with no upload control — not "
                                f"touching the gallery",
                                mutated=False,
                            )
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

            # Back to the hub, and only now does anything become public. Up to
            # this point every change has been a draft Craigslist would have
            # thrown away on "cancel edit".
            with log.step("publish"):
                done = page.locator(SEL["images_done"])
                if _count(page, "images_done") > 0:
                    done.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    sleep_jitter(1.2)
                page.goto(
                    hub_step_url(hub_url, HUB_STEP_PREVIEW),
                    wait_until="domcontentloaded",
                )
                read_pause(900)
                publish = page.locator(SEL["hub_publish"])
                if publish.count() == 0:
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="no_publish_control",
                        post_id=post_id, account=account.name,
                    ))
                    raise EditFailure(
                        "publish",
                        f"no publish control on the hub at {page.url} — the edit is "
                        f"staged but not applied",
                        mutated=True,
                    )
                publish.first.click()
                page.wait_for_load_state("domcontentloaded")
                sleep_jitter(2.0)
                if TRACE:
                    # Proof the write landed, captured while the page that
                    # accepted it is still on screen.
                    artifact_ids.extend(artifacts.capture_page(
                        page, flow="edit_reconcile", label="after_publish",
                        post_id=post_id, account=account.name,
                    ))

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
        page.wait_for_selector(SEL["file_input"], state="attached", timeout=15_000)
        file_input = page.locator(SEL["file_input"])
        for i, photo in enumerate(photos, 1):
            file_input.first.set_input_files(str(photo))
            _wait_for_thumb_count(page, i)
        ok = page.locator(SEL["image_thumb"]).count() == len(photos)
        logger.info(f"image recovery {'succeeded' if ok else 'did not restore full set'}")
        return ok
    except Exception as e:
        logger.error(f"image recovery failed outright: {e}")
        return False
