from __future__ import annotations

import random
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from patchright.sync_api import BrowserContext, Page, sync_playwright

from . import artifacts
from .config import CL_ACCOUNT_URL, CL_SEARCH_URL, CL_SITE, LOGS_DIR, Account
from .content import Ad, mark_content_used, mark_photos_used
from .covers import is_cover_path, mark_cover_used
from .human import human_click, human_type, read_pause, scroll_a_bit, sleep_jitter

FAILURES_DIR = LOGS_DIR / "failures"

# Steps at or after this one have already pushed image bytes to Craigslist.
# The server uses the reported step to decide whether a failed draft can be
# auto-requeued or must be parked for review (decision 16).
FIRST_ASSET_CONSUMING_STEP = "photo_upload"

# A full-page screenshot plus an HTML dump is ~1-2MB. Every photo slot that
# misses its thumbnail is worth seeing once; five of them in one run is just
# five copies of the same broken selector.
MAX_NONFATAL_CAPTURES = 1


@dataclass
class PostRun:
    """Everything one posting run learned, not just whether it published.

    `url` used to be the entire return value, which meant the run either
    succeeded silently or failed loudly — with no way to say "it published, but
    two photos never landed". Degradations now travel with the result and reach
    the dashboard on the post_attempt event.
    """

    url: str | None = None
    # Human-readable, one line each. These are what the dashboard flags.
    warnings: list[str] = field(default_factory=list)
    # Thumbnails Craigslist actually rendered, vs how many we tried to upload.
    photos_confirmed: int | None = None
    artifact_ids: list[str] = field(default_factory=list)
    # Non-fatal captures already spent this run — see MAX_NONFATAL_CAPTURES.
    nonfatal_captures: int = 0

    def warn(self, message: str) -> None:
        """Record a degradation and log it. Never affects the outcome."""
        logger.warning(f"  DEGRADED: {message}")
        self.warnings.append(message[:300])


# Geoverify's region disambiguation, shown when the ZIP geocodes outside the
# region we post from. Both buttons carry `class="continue"`, so to a selector
# they are indistinguishable from a continue button — and to a human they are a
# business decision. Named here so `_continue` can refuse them and
# `_answer_region_prompt` can own the choice.
REGION_KEEP = "button[name='keep_old_area']"
REGION_CHANGE = "button[name='area_change_ok']"
REGION_KEEP_VISIBLE = f"{REGION_KEEP}:visible"
REGION_PROMPT = f"{REGION_KEEP_VISIBLE}, {REGION_CHANGE}:visible"
# Appended to the generic continue selectors so `.first` can never answer the
# region question by DOM order — which picks "move the ad to the other region".
NOT_REGION = ":not([name='keep_old_area']):not([name='area_change_ok'])"


class PosterFailure(RuntimeError):
    """A post_ad failure that remembers which step it died on.

    post_ad tracked `step` for failure dumps already, but the value never left
    the function. Carrying it on the exception lets the CLI ship it in the
    post_attempt event — along with the artifacts and any degradations observed
    before the run died, which would otherwise be lost with the return value.
    """

    def __init__(self, step: str, original: Exception, run: PostRun | None = None):
        super().__init__(f"{step}: {original!r}")
        self.step = step
        self.original = original
        self.run = run or PostRun()


def _dump_failure(
    page: Page, account_name: str, step: str, err: Exception
) -> list[str]:
    """Capture the page twice: locally for immediate inspection, and spooled
    for upload so it is readable from the dashboard.

    The local copy under `logs/failures/` is kept because it is the only
    evidence available when the machine has no network. The spooled copy is the
    one that matters — a screenshot on a Windows box nobody logs into is not a
    debugging aid (DESIGN.md decision 17).

    Returns the spooled artifact ids for the post_attempt event.
    """
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = FAILURES_DIR / f"{ts}_{account_name}_{step}"
    try:
        page.screenshot(path=str(stem) + ".png", full_page=True)
    except Exception as e:
        logger.warning(f"screenshot failed: {e}")
    try:
        (stem.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
    except Exception as e:
        logger.warning(f"html dump failed: {e}")
    logger.error(f"[{account_name}] FAILED at step '{step}': {err!r}  (dump → {stem}.png/.html)")

    # capture_page never raises — evidence collection must not be the reason a
    # flow dies, and we are already on the failure path.
    return artifacts.capture_page(
        page, flow="post", label=step, account=account_name
    )

# Realistic desktop viewports — variation across accounts helps fingerprint diversity
VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
]

LOCALE = "en-US"
TIMEZONE_ID = "America/New_York"  # South Florida


@contextmanager
def launch_account(
    account: Account,
    headless: bool = False,
    *,
    flow: str = "post",
    lease_blocking: bool = True,
    lease_timeout: float = 900.0,
):
    """
    Launch a persistent context bound to this account's profile.
    Profile persists cookies, localStorage, IndexedDB → CL sees a stable browser.

    Acquiring the machine-wide browser lease is part of launching, not something
    callers opt into (DESIGN_EDITS.md decision 27). `launch_persistent_context`
    takes an exclusive OS lock on the profile directory, so two flows starting at
    once means a refused launch or a corrupted profile. Doing it here means a new
    flow cannot forget.

    `flow` names the holder for diagnostics; `lease_blocking=False` makes the
    caller skip rather than queue, which is what the edit worker wants — an edit
    can always wait for the next poll, a post cannot.
    """
    from . import lease  # local import: keeps `config`-only importers cheap

    account.profile_dir.mkdir(parents=True, exist_ok=True)
    # Deterministic viewport per account so fingerprint doesn't shift between runs
    vp = VIEWPORTS[hash(account.name) % len(VIEWPORTS)]
    with lease.acquire(
        flow, account=account.name, blocking=lease_blocking, timeout=lease_timeout
    ):
        with sync_playwright() as p:
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(account.profile_dir),
                channel="chrome",  # use real Chrome (patchright recommends this)
                headless=headless,
                viewport=vp,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                no_viewport=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            try:
                yield context
            finally:
                context.close()


def is_logged_in(page: Page) -> bool:
    page.goto(CL_ACCOUNT_URL, wait_until="domcontentloaded")
    sleep_jitter(2.0)
    return "settings" in page.url or page.locator("text=postings").count() > 0


def post_ad(account: Account, ad: Ad, *, headless: bool = False, dry_run: bool = False) -> PostRun:
    """
    Post one ad. Returns a PostRun carrying the URL plus anything that went
    wrong without stopping the run.
    NOTE: CL's posting UI changes periodically. If selectors break, run with
    headless=False and step through visually to update them.
    """
    logger.info(f"[{account.name}] posting: {ad.title!r}  photos={len(ad.photos)}")
    run = PostRun()
    step = "launch"
    with launch_account(account, headless=headless) as ctx:
        page = ctx.new_page()
        try:
            step = "warmup"
            logger.debug(f"step: {step}")
            page.goto(CL_SITE, wait_until="domcontentloaded")
            read_pause(800)
            scroll_a_bit(page)
            sleep_jitter(1.5)

            step = "login_check"
            logger.debug(f"step: {step}")
            if not is_logged_in(page):
                logger.error(
                    f"[{account.name}] not logged in. Run `uv run cl init-account {account.name}` first."
                )
                raise RuntimeError(
                    f"account {account.name} is not logged in; "
                    f"run `cl init-account {account.name}`"
                )

            step = "open_post_form"
            logger.debug(f"step: {step}")
            page.goto("https://post.craigslist.org/c/mia", wait_until="domcontentloaded")
            read_pause(400)

            step = "dismiss_reuse_prompt"
            logger.debug(f"step: {step}")
            _dismiss_reuse_prompt(page)

            step = "advance_to_type"
            logger.debug(f"step: {step}")
            _advance_until(page, expect_selector="input[name='id'][value='so']", county=ad.county, max_steps=5, run=run)

            step = "type_service_offered"
            logger.debug(f"step: {step}")
            _click_radio_by_value(page, name="id", value="so")
            _continue(page)

            step = "category_skilled_trade"
            logger.debug(f"step: {step}")
            # value="83" = skilled trade services on CL South Florida
            _click_radio_by_value(page, name="id", value="83")
            _continue(page)

            step = "advance_to_form"
            logger.debug(f"step: {step}")
            _advance_until(page, expect_selector="input[name='PostingTitle']", county=ad.county, max_steps=4, run=run)

            step = "form_title"
            logger.debug(f"step: {step}")
            page.wait_for_selector("input[name='PostingTitle']", timeout=30_000)
            read_pause(400)
            human_type(page.locator("input[name='PostingTitle']"), ad.title)
            sleep_jitter(0.7)

            if ad.postal_code:
                step = "form_zip"
                logger.debug(f"step: {step}")
                zip_input = page.locator("input[name='postal']")
                if zip_input.count():
                    human_type(zip_input, ad.postal_code)
                    sleep_jitter(0.4)

            # Craigslist's "city or neighborhood" box. Free text, so the draft
            # may widen it beyond the city — see Ad.geo_text.
            if ad.geo_text:
                step = "form_city"
                logger.debug(f"step: {step}")
                geo = page.locator("input[name='geographic_area']")
                if geo.count():
                    human_type(geo, ad.geo_text)
                    sleep_jitter(0.3)

            if ad.license_number:
                step = "form_license"
                logger.debug(f"step: {step}")
                # Click 'licensed' radio (value=1) to enable license_info field
                lic_radio = page.locator("input[name='has_license'][value='1']")
                if lic_radio.count():
                    human_click(page, lic_radio)
                    sleep_jitter(0.4)
                    lic = page.locator("input[name='license_info']")
                    if lic.count():
                        human_type(lic, ad.license_number)
                        sleep_jitter(0.3)

            if ad.phone_number:
                step = "form_phone"
                logger.debug(f"step: {step}")
                # Every checkbox first, the number last. Toggling one of these
                # re-renders the contact block and wipes whatever is in
                # contact_phone — see _ensure_contact_phone.
                #
                # show_phone_ok gates the other two (`data-depends-on`), so it
                # has to be ticked before they exist to tick.
                _tick_contact_box(page, "show_phone_ok")
                # Enable both call and text by default
                for cb_name in ("contact_phone_ok", "contact_text_ok"):
                    if not _tick_contact_box(page, cb_name):
                        run.warn(
                            f"{cb_name} never appeared on the form; the ad may "
                            f"not accept calls or texts"
                        )
                _ensure_contact_phone(page, ad.phone_number, run=run)

            step = "form_body"
            logger.debug(f"step: {step}")
            body_el = page.locator("textarea[name='PostingBody']")
            # Click into the textarea like a human, then paste the whole body.
            # Real users paste long descriptions; typing 7000 chars is slow AND bot-like.
            human_click(page, body_el)
            sleep_jitter(0.5)
            body_el.fill(ad.body)
            # Brief "reading what I just pasted" pause
            sleep_jitter(1.5, 0.4)

            if ad.phone_number:
                # Last look before the form goes. The body fill and its pause
                # sit between the number and the submission, and the whole
                # point of this bug is that the field can empty itself without
                # anyone touching it. Cheap when the value is already right: one
                # read, no typing.
                #
                # Deliberately still `form_phone` rather than a step of its own:
                # the server routes a failed draft on this string, and a name
                # missing from its PRE_UPLOAD_STEPS would park the draft for a
                # human on a run that uploaded nothing.
                step = "form_phone"
                _ensure_contact_phone(page, ad.phone_number, run=run)

            # Back to the step that owns the submission. Same reason as above:
            # 'form_body' is a name the server already routes as pre-upload.
            step = "form_body"
            _continue(page)

            # Did that submission actually take? Everything below assumes we
            # have moved on from the details form, and a rejected form looks
            # identical to a slow one until a much later selector times out.
            step = "form_validation"
            logger.debug(f"step: {step}")
            _assert_form_accepted(page)

            step = "map_confirm"
            logger.debug(f"step: {step}")
            _continue(page, optional=True)
            # Again after the map step: geoverify can bounce us back to the
            # form, and the same silence applies.
            _assert_form_accepted(page)
            # The region question is injected once the ZIP has geocoded, which
            # can land either side of the click above — so ask after it, not
            # before.
            if _answer_region_prompt(page):
                _assert_form_accepted(page)
                # Answering *is* the map submission: the pickbutton posts the
                # form and Craigslist goes straight on to images. Continuing
                # again unconditionally here clicked through the uploader onto
                # the preview page, where photo_upload timed out on a page that
                # has no file input either — the same symptom one step further
                # along, observed on craigs3 at 09:54 on 2026-08-05.
                #
                # So only continue if the map is demonstrably still on screen.
                # If it is not, the run is already where it needs to be, and
                # _assert_left_map below is what proves it.
                if _still_on_map(page):
                    _continue(page, optional=True)

            step = "map_validation"
            logger.debug(f"step: {step}")
            _assert_left_map(page)

            step = "photo_upload"
            logger.info(f"[{account.name}] photo_upload: {len(ad.photos)} file(s) queued")
            for idx, photo in enumerate(ad.photos):
                tag = "COVER" if is_cover_path(photo) else "photo"
                size_kb = photo.stat().st_size / 1024 if photo.exists() else -1
                logger.info(
                    f"  slot {idx + 1}: [{tag}] {photo.name}  ({size_kb:.0f} KB)  path={photo}"
                )
            if ad.photos:
                # `state="attached"`, not the default `visible`: Craigslist's
                # uploader input is styled opacity-0 and absolutely positioned,
                # and is driven with set_input_files rather than by clicking
                # "Add Images" — which works on a hidden input and does not need
                # it painted. editor.py has waited this way since it was written;
                # this side had kept the default, one CSS change away from a
                # 30-second timeout on an uploader that was there all along.
                page.wait_for_selector(
                    "input[type='file']", state="attached", timeout=30_000
                )
                file_input = page.locator("input[type='file']")
                for i, photo in enumerate(ad.photos, 1):
                    is_cover = is_cover_path(photo)
                    tag = "COVER" if is_cover else "photo"
                    before = _count_uploaded_thumbs(page)
                    t0 = datetime.now(timezone.utc)
                    logger.info(
                        f"  [{i}/{len(ad.photos)}] uploading [{tag}] {photo.name}  "
                        f"(thumbs before: {before})"
                    )
                    file_input.set_input_files(str(photo))
                    landed = _wait_for_thumb_increment(page, expected=before + 1, timeout_s=45)
                    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                    after = _count_uploaded_thumbs(page)
                    if landed:
                        logger.info(
                            f"    ✓ landed in {elapsed:.1f}s  (thumbs now: {after})"
                        )
                    else:
                        run.warn(
                            f"photo {i}/{len(ad.photos)} ({photo.name}) never rendered a "
                            f"thumbnail after {elapsed:.1f}s (thumbs {before} → {after}) — "
                            f"upload failed, or the thumbnail selector no longer matches CL"
                        )
                        # Dump the page so we can pick the right selector. Once
                        # per run: five identical dumps of one broken selector
                        # is five uploads that teach nothing new.
                        if run.nonfatal_captures < MAX_NONFATAL_CAPTURES:
                            run.nonfatal_captures += 1
                            run.artifact_ids.extend(
                                _dump_photo_page(page, account.name, f"slot{i}_no_thumb")
                            )
                    if is_cover:
                        # Only burn the cover once we've actually seen it land on
                        # CL's servers. Prior behavior burned it after set_input_files
                        # returned (which only queues the upload) — that's why covers
                        # were being consumed even when they raced later uploads and
                        # lost the thumbnail slot.
                        if landed:
                            mark_cover_used(photo)
                            logger.info(f"    cover consumed → moved to used/")
                        else:
                            run.warn(
                                f"cover {photo.name} kept claimed (not marked used): "
                                f"its upload never confirmed, so the ad's thumbnail "
                                f"slot is not the cover we intended"
                            )
                    sleep_jitter(0.8, 0.3)
                final_thumbs = _count_uploaded_thumbs(page)
                run.photos_confirmed = final_thumbs
                logger.info(
                    f"[{account.name}] photo_upload done: expected {len(ad.photos)} "
                    f"thumbnail(s), CL shows {final_thumbs}"
                )
                if final_thumbs != len(ad.photos):
                    run.warn(
                        f"thumbnail count mismatch: uploaded {len(ad.photos)}, "
                        f"CL shows {final_thumbs} — an upload was rejected, or the "
                        f"count selector missed some"
                    )
                _log_thumbnail_order(page)
                sleep_jitter(1.5)
            else:
                run.photos_confirmed = 0
            _click_text(page, "done with images")

            step = "preview"
            logger.debug(f"step: {step}")
            page.wait_for_load_state("domcontentloaded")
            read_pause(1200)
            scroll_a_bit(page)
            sleep_jitter(2.0)

            if dry_run:
                logger.warning(f"[{account.name}] DRY RUN — not clicking publish.")
                return run

            step = "publish"
            logger.debug(f"step: {step}")
            _click_text(page, "publish")

            step = "billing"
            logger.debug(f"step: {step}")
            page.wait_for_load_state("domcontentloaded", timeout=60_000)
            sleep_jitter(2.0)
            _handle_billing(page, account.name)

            step = "confirmation"
            logger.debug(f"step: {step}")
            page.wait_for_load_state("domcontentloaded", timeout=60_000)
            sleep_jitter(2.0)

            run.url = _extract_post_url(
                page, run=run, expected_title=ad.title, account=account.name
            )
            mark_photos_used(ad.photos)
            mark_content_used(ad)
            logger.success(f"[{account.name}] published: {run.url}")
            if run.warnings:
                logger.warning(
                    f"[{account.name}] published WITH {len(run.warnings)} degradation(s) — "
                    f"see the post's Diagnostics entry"
                )
            return run
        except Exception as e:
            # Degradations observed before the failure travel with the exception;
            # they are often the explanation for it.
            run.artifact_ids.extend(_dump_failure(page, account.name, step, e))
            raise PosterFailure(step, e, run=run) from e


# ─── Photo upload helpers ─────────────────────────────────────────────────────

# CL's photo widget varies over time. We try several signals and take the max:
#   - <img> served from CL's image hosts
#   - <img> with a data: URL (client-side preview before upload completes)
#   - <img> or <li> inside common photo-list containers
# Taking the max means we err on the side of "new thumbnail appeared" even if
# one selector misses.
_UPLOADED_THUMB_SELECTORS = [
    "img[src*='images.craigslist.org']",
    "img[src*='craigslist-images']",
    "img[src*='post.craigslist']",
    "img[src^='data:image']",
    "img[src^='blob:']",
    "#images img",
    ".images img",
    "ul.image_list img",
    "ul.image_list li",
    ".uploaded img",
    ".upload_item",
    "[class*='thumb'] img",
]


def _count_uploaded_thumbs(page: Page) -> int:
    best = 0
    for sel in _UPLOADED_THUMB_SELECTORS:
        try:
            n = page.locator(sel).count()
            if n > best:
                best = n
        except Exception:
            continue
    return best


def _dump_photo_page(page: Page, account_name: str, label: str) -> list[str]:
    """Save HTML + screenshot of the current photo-upload page for inspection.

    Same dual capture as `_dump_failure`: local for offline inspection, spooled
    so it is readable from the dashboard. Returns the spooled artifact ids.
    """
    from pathlib import Path
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = FAILURES_DIR / f"{ts}_{account_name}_photo_{label}"
    try:
        page.screenshot(path=str(stem) + ".png", full_page=True)
    except Exception as e:
        logger.warning(f"    screenshot dump failed: {e}")
    try:
        Path(str(stem) + ".html").write_text(page.content(), encoding="utf-8")
        logger.info(f"    dumped photo page → {stem}.png / .html")
    except Exception as e:
        logger.warning(f"    html dump failed: {e}")
    return artifacts.capture_page(
        page, flow="post", label=f"photo_{label}", account=account_name
    )


def _wait_for_thumb_increment(page: Page, *, expected: int, timeout_s: float = 45) -> bool:
    """
    Poll until CL renders a new uploaded thumbnail (or timeout).
    Returns True if the count reached `expected`, False on timeout.
    Blocking here is intentional — starting the next set_input_files before
    the current upload finishes lets CL reorder thumbnails by completion time,
    which is how the cover ends up demoted out of slot 1.
    """
    import time

    deadline = time.monotonic() + timeout_s
    last_seen = -1
    while time.monotonic() < deadline:
        n = _count_uploaded_thumbs(page)
        if n != last_seen:
            logger.debug(f"    waiting for thumb: have {n}, need {expected}")
            last_seen = n
        if n >= expected:
            return True
        time.sleep(0.5)
    return False


def _log_thumbnail_order(page: Page) -> None:
    """Dump the order + src of each thumbnail CL is currently showing."""
    # Try each selector; use the one that returns the most matches (same rule
    # as _count_uploaded_thumbs so the "order" view lines up with the count).
    srcs: list[str] = []
    for sel in _UPLOADED_THUMB_SELECTORS:
        try:
            found = page.locator(sel).evaluate_all(
                "els => els.map(e => e.getAttribute('src') || e.getAttribute('data-src') || '(no src)')"
            )
        except Exception:
            continue
        if len(found) > len(srcs):
            srcs = found
    if not srcs:
        logger.info("  thumbnail order: (none visible)")
        return
    logger.info(f"  thumbnail order on page ({len(srcs)}):")
    for i, s in enumerate(srcs, 1):
        marker = "  <-- slot 1 (will be the ad thumbnail)" if i == 1 else ""
        logger.info(f"    {i}. {s}{marker}")


# ─── Selector helpers ─────────────────────────────────────────────────────────

def _advance_until(
    page: Page,
    *,
    expect_text: str | None = None,
    expect_selector: str | None = None,
    county: str | None = None,
    max_steps: int = 5,
    run: PostRun | None = None,
) -> None:
    """
    Click 'continue' through intermediate confirmation pages (area, subarea,
    geoverify, etc.) until either `expect_text` appears on the page or
    `expect_selector` is present. If a subarea radio list is detected, pick
    the radio matching `county` before clicking continue.
    """
    for i in range(max_steps):
        if expect_selector:
            try:
                page.wait_for_selector(expect_selector, timeout=2500, state="attached")
                logger.debug(f"  reached target selector after {i} step(s)")
                return
            except Exception:
                pass
        if expect_text:
            try:
                if page.get_by_text(expect_text, exact=False).first.count():
                    logger.debug(f"  reached target text after {i} step(s)")
                    return
            except Exception:
                pass

        # If this is a subarea page, pick the right county radio
        if page.locator("body.subarea").count() or page.locator("p.formnote >> text=choose the location").count():
            _select_subarea(page, county, run=run)

        btn = page.locator("button[type='submit']").first
        if not btn.count():
            logger.debug(f"  no submit button on page; stopping at step {i}")
            return
        logger.debug(f"  advancing intermediate page (step {i + 1})")
        human_click(page, btn)
        page.wait_for_load_state("domcontentloaded")
        sleep_jitter(1.2)
    msg = (
        f"navigation exhausted {max_steps} steps without reaching "
        f"{expect_selector or expect_text!r} — Craigslist's page flow has changed"
    )
    if run is not None:
        run.warn(msg)
    else:
        logger.warning(f"  {msg}")


def _select_subarea(page: Page, county: str | None, *, run: PostRun | None = None) -> None:
    """Pick the miami-dade / broward / palm-beach radio that matches `county`."""
    c = (county or "").lower()
    if "palm" in c:
        match = "palm beach"
    elif "broward" in c:
        match = "broward"
    elif "miami" in c or "dade" in c:
        match = "miami"
    else:
        # Default: pick the first radio so the form can proceed. This files the
        # ad under a county nobody chose, so it is a degradation, not a detail.
        match = None
        msg = (
            f"subarea: no county match for {county!r} — filed under the first "
            f"option on the page, which may be the wrong county"
        )
        if run is not None:
            run.warn(msg)
        else:
            logger.warning(f"  {msg}")
    if match:
        label = page.locator("label").filter(has_text=match).first
    else:
        label = page.locator("input[type='radio'][name='n']").first
    if label.count():
        logger.debug(f"  subarea: selecting '{match or 'first'}'")
        human_click(page, label)
        sleep_jitter(0.4)


def _dismiss_reuse_prompt(page: Page) -> None:
    """
    CL shows a 'Re-use selected data from your previous posting?' screen when
    you have a recent post. Click the 'skip' (brand_new_post) button so we
    build the new post from scratch.
    Silent no-op if the prompt isn't present.
    """
    skip_btn = page.locator("button[name='brand_new_post']").first
    if skip_btn.count():
        logger.debug("  dismissing reuse prompt via skip button (brand_new_post)")
        human_click(page, skip_btn)
        page.wait_for_load_state("domcontentloaded")
        sleep_jitter(1.0)
        return
    # Fallback for older flow variants
    for text in ("start a new posting", "new posting", "no thanks", "start fresh"):
        try:
            el = page.get_by_text(text, exact=False).first
            if el.count() and el.is_visible():
                logger.debug(f"  dismissing reuse prompt via '{text}'")
                human_click(page, el)
                page.wait_for_load_state("domcontentloaded")
                sleep_jitter(1.0)
                return
        except Exception:
            continue


def _click_radio(page: Page, *, label_contains: str) -> None:
    """Click the radio whose nearby label text contains `label_contains` (case-insensitive)."""
    label = page.locator(f"label").filter(has_text=label_contains).first
    label.wait_for(timeout=15_000)
    human_click(page, label)


def _click_radio_by_value(page: Page, *, name: str, value: str) -> None:
    """Click a radio by exact name/value attributes. Strict, won't pick a wrong option."""
    radio = page.locator(f"input[type='radio'][name='{name}'][value='{value}']").first
    radio.wait_for(timeout=15_000)
    logger.debug(f"  clicking radio name='{name}' value='{value}'")
    human_click(page, radio)
    sleep_jitter(0.4)


def _click_radio_by_label_exact(page: Page, label_text: str) -> None:
    """
    Click a radio whose surrounding <label>'s visible text equals `label_text`
    (case-insensitive, whitespace-normalized). Avoids the fuzzy-text foot-gun
    where 'skilled trade' accidentally matches a tooltip or helper text.
    """
    import re

    target = " ".join(label_text.strip().lower().split())
    # Try playwright's role-based selector first (most accurate for radios)
    try:
        el = page.get_by_role("radio", name=re.compile(rf"^\s*{re.escape(target)}\s*$", re.I)).first
        if el.count():
            logger.debug(f"  clicking radio by role name='{label_text}'")
            human_click(page, el)
            sleep_jitter(0.4)
            return
    except Exception:
        pass
    # Fallback: scan all labels and match normalized text exactly
    n = page.locator("label").count()
    for i in range(n):
        lbl = page.locator("label").nth(i)
        try:
            text = " ".join((lbl.inner_text() or "").strip().lower().split())
        except Exception:
            continue
        if text == target:
            logger.debug(f"  clicking label[{i}] with exact text '{label_text}'")
            human_click(page, lbl)
            sleep_jitter(0.4)
            return
    raise RuntimeError(f"no radio label exactly matches {label_text!r}")


def _tick_contact_box(page: Page, name: str, *, timeout_ms: int = 5_000) -> bool:
    """Check one checkbox in the contact block. Returns whether it ended up on.

    Waits for the box instead of sampling `count()` once. `contact_phone_ok` and
    `contact_text_ok` carry `data-depends-on="show_phone_ok"`, so they can be a
    beat behind the tick that reveals them. The old ordering got away with a bare
    `count()` because it typed the phone number in between — seconds of
    character-by-character typing, which was accidentally the wait. Ticking them
    back to back removes that, and a `count()` of 0 would skip the box in silence
    and post an ad nobody can ring.

    `state="attached"` rather than `visible`: that is what the old `count()` test
    accepted, and Craigslist styles some of these inputs behind their labels.
    Clicking one that is attached but genuinely unclickable still fails, in
    `human_click`, the same way it always did.
    """
    sel = f"input[name='{name}']"
    try:
        page.wait_for_selector(sel, state="attached", timeout=timeout_ms)
    except Exception:
        return False
    cb = page.locator(sel)
    if not cb.count():
        return False
    if not cb.first.is_checked():
        human_click(page, cb.first)
        sleep_jitter(0.3)
    return True


def _ensure_contact_phone(
    page: Page, phone: str, *, run: PostRun | None = None, attempts: int = 3
) -> None:
    """Make `contact_phone` actually hold `phone` when the form is submitted.

    Craigslist's json-form re-renders the contact block when a checkbox inside
    it is toggled, and the re-render restores the block from form state —
    discarding anything typed into `contact_phone` beforehand. The site then
    rejects the submission with "If users can contact you by phone, please
    include a contact phone number". That cost craigs4 a posting slot on
    2026-08-05 and again on 2026-08-06, both times on draft 71, whose phone
    number was present and well-formed the whole time.

    Accounts with saved contact preferences never see it: their checkboxes
    arrive already ticked, so nothing is toggled and nothing re-renders. It only
    bites a freshly logged-in profile, which is why it survived months of
    healthy runs and appeared the day after craigs4 was re-logged-in.

    Two things follow, and the second is the one that matters. The caller ticks
    the checkboxes *before* calling this, so the known re-render happens while
    the field is still empty. And the value is read back rather than assumed,
    because ordering only fixes the re-render we have already seen — a form that
    clears the field for some other reason has to fail here, loudly, rather than
    at `form_validation` after the whole form has been walked.

    Raises rather than posting a phoneless ad: `form_phone` is a pre-upload step
    (`PRE_UPLOAD_STEPS` server-side), so the draft returns to the queue with its
    images untouched.
    """
    field = page.locator("input[name='contact_phone']")
    if not field.count():
        show_phone = page.locator("input[name='show_phone_ok']")
        if show_phone.count() and show_phone.first.is_checked():
            raise RuntimeError(
                "show_phone_ok is checked but the form has no contact_phone "
                "field; Craigslist would reject the submission"
            )
        # A form variant with no phone option at all. Not fatal — the number is
        # in the body copy either way — but it changes what the ad looks like,
        # so it must not pass silently.
        if run is not None:
            run.warn("no contact_phone field on the form; posted without a phone")
        return

    for attempt in range(attempts):
        if (field.first.input_value() or "").strip() == phone:
            return
        if attempt == 0:
            human_type(field.first, phone)
        else:
            # Deterministic retry. `human_type` appends rather than replaces,
            # and by now the field may hold a partial value from a re-render
            # that landed mid-type.
            field.first.fill("")
            field.first.fill(phone)
        sleep_jitter(0.3)

    got = (field.first.input_value() or "").strip()
    if got == phone:
        return
    raise RuntimeError(
        f"contact_phone would not hold {phone!r} after {attempts} attempt(s) "
        f"(field reads {got!r}); Craigslist would reject the form"
    )


def _continue(page: Page, optional: bool = False) -> None:
    """Click the 'continue' button. If optional and missing, skip."""
    # Try specific continue selectors first; some pages also have a disabled
    # secondary submit button (e.g. geoverify's "find").
    #
    # `:visible` is load-bearing. Geoverify ships `#regular_continue_button`
    # with `style="display: none"` while it is waiting for the region question
    # to be answered, and a hidden button still satisfies `:not([disabled])`.
    # Clicking it submitted nothing, `optional=True` swallowed the miss, and the
    # run went to photo_upload still standing on the map — where it spent 30s
    # waiting for a file input that page has never had (craigs3, 2026-08-05).
    #
    # `NOT_REGION` is the other half: the region pickbuttons are `continue`-
    # classed *answers*, not continues, and `.first` would answer by DOM order.
    candidates = [
        f"button.continue:visible{NOT_REGION}:not([disabled])",
        "button[name='go'][value='continue']:visible:not([disabled])",
        f"button[type='submit']:visible{NOT_REGION}:not([disabled])",
    ]
    btn = None
    for sel in candidates:
        loc = page.locator(sel).first
        if loc.count():
            btn = loc
            break
    if btn is None:
        if optional:
            return
        raise RuntimeError("continue button not found")
    human_click(page, btn)
    page.wait_for_load_state("domcontentloaded")
    sleep_jitter(1.2)


def _answer_region_prompt(page: Page) -> bool:
    """Answer geoverify's "which region should this be searchable in?".

    Craigslist asks whenever the ZIP geocodes outside the region the account
    posts from: 33410 (Palm Beach Gardens) resolves to Treasure Coast while the
    run is posting South Florida. Until it is answered the page **hides its
    continue button**, so this is not a nicety — an unanswered prompt is a dead
    run, and it killed craigs3's 08:47 slot on 2026-08-05.

    We always keep the region we are posting from. Everything else about the run
    is South Florida — `/c/mia`, category 83, the account, the other seventeen
    queued drafts — and `area_change_ok` would move this one ad to a region
    where none of that holds, in front of an audience outside the service area.
    Craigslist's own default is the same: it offers to keep, and asks first.

    Returns True if a prompt was there and answered.
    """
    # Visible only: the prompt's markup can outlive the question once it has
    # been answered, and clicking a stale copy would resubmit the map step.
    keep = page.locator(REGION_KEEP_VISIBLE).first
    if not keep.count():
        return False
    # Not a `run.warn`: this is routine for a multi-city queue, and marking every
    # such post `degraded` would bury the degradations that mean something. It is
    # still worth a line, because a ZIP outside the posting region can equally
    # mean the draft's ZIP is simply wrong.
    logger.warning(
        "geoverify asked which region this posting belongs to — keeping the "
        "region we post from (check the draft's ZIP if this repeats)"
    )
    human_click(page, keep)
    page.wait_for_load_state("domcontentloaded")
    sleep_jitter(1.0)
    return True


def _still_on_map(page: Page) -> bool:
    """Is geoverify still on screen? Structural, so it holds whether the map is
    waiting on the region question or on a plain continue."""
    try:
        return bool(
            page.locator(REGION_PROMPT).count()
            or page.locator("#leafletForm:visible").count()
        )
    except Exception:  # mid-navigation — treat as gone, the assert re-checks
        return False


def _assert_left_map(page: Page) -> None:
    """Raise if the run is still on the map step when it should be uploading.

    The map page has no file input and never will, so standing on it surfaced as
    `photo_upload: Timeout 30000ms exceeded waiting for input[type='file']` —
    which reads as a broken uploader and is not one. Worse, `photo_upload` is
    asset-consuming, so the server parked the draft and retired its images for a
    run that never showed Craigslist a single photo. Exactly the failure mode
    `_assert_form_accepted` was written for, one page later.

    Failing here keeps it pre-upload: the draft requeues, the images stay clean,
    and the error names the page we are actually stuck on.

    Deliberately narrow. A false positive here stops posting altogether, so the
    generic arm also requires the uploader to be absent — if a file input is
    reachable, this never fires no matter what else the page is holding.
    """
    if page.locator(REGION_PROMPT).count():
        raise RuntimeError(
            "still on the map step: Craigslist is asking which region this "
            "posting should be searchable in, and the question went unanswered"
        )
    try:
        # Present on the images step and nowhere else in this flow, so it is the
        # one signal that says "we are where photo_upload expects to be".
        if page.locator("input[type='file']").count():
            return
        on_map = page.locator("#leafletForm:visible").count()
        # The far side of the same mistake: one continue too many walks straight
        # past the uploader onto the preview/publish page, which has no file
        # input either — so it also died as a photo_upload timeout, 30s spent on
        # a page whose problem is that we arrived too late, not too early.
        overshot = page.locator("form#publish_top").count()
    except Exception:  # navigating out from under us — that is the good case
        return
    if on_map:
        raise RuntimeError(
            f"still on the map step after continuing ({page.url}) — the photo "
            f"uploader is not reachable from this page"
        )
    if overshot:
        raise RuntimeError(
            f"overshot the images step onto the preview page ({page.url}) — "
            f"one continue too many; the ad would publish with no photos"
        )


def _assert_form_accepted(page: Page) -> None:
    """Raise if Craigslist redisplayed the posting form with validation errors.

    A rejected form comes back as HTTP 200 carrying the same page, so nothing
    downstream notices — the run simply keeps going and dies at whatever
    selector it looks for next. A body 403 characters over the 16,000 limit
    surfaced as a 30-second timeout waiting for the photo uploader, and because
    `photo_upload` is the first asset-consuming step, the server parked the
    draft and retired all 24 attached images. Nothing had been uploaded: the
    browser never left the details page.

    So this is not cosmetic. Catching the rejection here keeps the failure
    classified as pre-upload, which requeues the draft and burns nothing, and
    puts Craigslist's own wording in the error instead of a selector timeout.
    """
    # The primary signal is structural, not textual: if the body textarea is
    # still on the page, the submission did not advance us. Craigslist ships
    # the error markup as a hidden template on a clean form, so keying off
    # `.err` alone would abort every healthy run — a false positive here is
    # safe (it requeues) but would stop posting entirely, which is worse than
    # the bug it fixes.
    still_on_form = page.locator("textarea[name='PostingBody']")
    try:
        if not still_on_form.count() or not still_on_form.first.is_visible():
            return
    except Exception:  # navigating out from under us — that is the good case
        return

    messages: list[str] = []
    errors = page.locator("div.error-list, span.err")
    for i in range(min(errors.count(), 6)):
        try:
            text = (errors.nth(i).inner_text() or "").strip()
        except Exception:  # detached mid-read; not worth failing over
            continue
        if text:
            messages.append(" ".join(text.split()))
    detail = " | ".join(dict.fromkeys(messages))[:400] or (
        "the details form was redisplayed with no visible reason"
    )
    raise RuntimeError(f"Craigslist rejected the form: {detail}")


def _click_text(page: Page, text: str) -> None:
    el = page.get_by_role("button", name=text).first
    if not el.count():
        el = page.locator(f"input[type='submit'][value*='{text}' i]").first
    if not el.count():
        el = page.get_by_text(text, exact=False).first
    human_click(page, el)


def _is_receipt_page(page: Page) -> bool:
    """The post-publish 'payment confirmation' page shown after the saved card
    is auto-charged. Distinct from a pre-payment form because it has the
    receipt copy and a PostingID."""
    try:
        html = page.content()
    except Exception:
        return False
    needles = ("Purchase Receipt", "Payment ID:", "Thanks for posting", "PostingID")
    return sum(1 for n in needles if n in html) >= 2


def _on_billing_page(page: Page) -> bool:
    # Receipt page also has 's=billing' in the URL but it's DONE — not billing.
    if _is_receipt_page(page):
        return False
    url = (page.url or "").lower()
    if "s=billing" in url or "/pay" in url:
        return True
    try:
        if page.locator("text=/payment|credit card|purchase|checkout|total/i").first.count():
            return True
    except Exception:
        pass
    return False


def _needs_payment_method(page: Page) -> bool:
    """Is checkout asking us to *enter* a card rather than confirm a saved one?

    Deliberately looks for the input fields rather than the words: 'credit card'
    appears on a review page that has a card on file just as readily as on one
    that does not, but a card-number box only appears when there is nothing to
    charge.
    """
    for sel in (
        "input[name*='card' i]",
        "input[id*='cardnumber' i]",
        "input[autocomplete='cc-number']",
        "iframe[title*='card number' i]",
        "input[name*='cvv' i]",
        "input[name*='cvc' i]",
    ):
        try:
            if page.locator(sel).first.count():
                return True
        except Exception:
            continue
    try:
        if page.locator(
            "text=/add a (payment|credit card)|no card on file|enter your card/i"
        ).first.count():
            return True
    except Exception:
        pass
    return False


def _billing_stall_reason(page: Page) -> str:
    if _needs_payment_method(page):
        return (
            "no payment method on this account — Craigslist is asking for card "
            "details at checkout. Add a card to the account; retrying will not "
            "help"
        )
    return "billing flow stuck — no matching button found"


def _handle_billing(page: Page, account_name: str, max_steps: int = 4) -> None:
    """
    Drive through CL's paid-category checkout using the card already saved on
    the account. CL's billing flow varies, but the pattern is:
      1. Review/total page → "continue" or "use saved card"
      2. Confirm purchase → final "purchase" / "submit payment" button
      3. Redirect to confirmation page with /d/ link
    We try multiple candidate buttons per step. If none match, dump the page
    so selectors can be updated.
    """
    if not _on_billing_page(page):
        logger.debug("  not on billing page; skipping billing handler")
        return

    logger.info(f"[{account_name}] billing page detected → driving through checkout")

    # Candidate buttons in priority order. CL has used variations of these.
    candidates = [
        # explicit "use saved card" / "use card on file" buttons
        "button:has-text('use card on file')",
        "button:has-text('use saved card')",
        "input[type='submit'][value*='card on file' i]",
        "input[type='submit'][value*='saved card' i]",
        # final purchase / pay buttons
        "button:has-text('purchase')",
        "button:has-text('submit payment')",
        "button:has-text('place order')",
        "button:has-text('pay now')",
        "button:has-text('complete purchase')",
        "input[type='submit'][value*='purchase' i]",
        "input[type='submit'][value*='pay' i]",
        # generic continue / confirm
        "button.continue:not([disabled])",
        "button[name='go'][value='continue']:not([disabled])",
        "button:has-text('continue')",
        "button:has-text('confirm')",
        "button[type='submit']:not([disabled])",
    ]

    for i in range(max_steps):
        # Already left billing? We're done.
        if not _on_billing_page(page):
            logger.success(f"  billing complete after {i} click(s)")
            return

        clicked = False
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible():
                    logger.debug(f"  billing step {i+1}: clicking '{sel}'")
                    human_click(page, btn)
                    page.wait_for_load_state("domcontentloaded", timeout=60_000)
                    sleep_jitter(2.5)
                    clicked = True
                    break
            except Exception as e:
                logger.debug(f"    selector {sel!r} failed: {e}")
                continue

        if not clicked:
            # No dump here: post_ad's own handler captures the same page a
            # moment later, and capturing twice uploads two copies of one
            # screenshot. Raise with the URL so the artifact is identifiable.
            #
            # Separate the two reasons this happens, because they need opposite
            # responses. A page asking for card details means no payment method
            # is saved on the account: no retry will ever pass, and the fix is
            # in Craigslist's account settings, not in these selectors. Anything
            # else is a selector that has drifted.
            raise RuntimeError(
                f"{_billing_stall_reason(page)} (on {page.url})"
            )

    if _on_billing_page(page):
        raise RuntimeError("billing flow exceeded max_steps without completing")


def _capture_unverified(
    page: Page, *, account: str | None, label: str, run: PostRun | None
) -> None:
    """Photograph a page that claims a post published without proving it.

    Artifacts used to be captured on exceptions only, so the one outcome nobody
    can explain afterwards — "published, but we could not find the ad" — was the
    single outcome with no picture attached. Both craigs1 runs that ended this
    way on 2026-08-05 and 2026-08-06 reported `artifact_ids = []`, and answering
    "was that ad ever live?" then took a database archaeology session and a
    `scan-ended` two days later. The answer was no.

    Wrapped in its own try even though `capture_page` is documented not to
    raise: this runs on the *success* path, and evidence collection must never
    be the reason a published ad gets reported as a failure.
    """
    if account is None or run is None:
        return
    try:
        run.artifact_ids.extend(
            artifacts.capture_page(page, flow="post", label=label, account=account)
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"could not capture the unverified page: {e}")


def _slug_words(text: str) -> list[str]:
    """Words from a title long enough to be distinctive in a URL slug."""
    import re

    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 3]


def _url_matches_title(href: str, title: str) -> bool:
    """Does this /d/ link plausibly belong to the ad we just published?

    Craigslist builds the slug from the posting title, so a real match shares
    several distinctive words with it. Two hits is deliberately lenient — the
    slug is truncated — but more than enough to reject an unrelated listing.
    """
    words = _slug_words(title)[:6]
    if not words:
        return False
    slug = (href or "").lower()
    return sum(1 for w in words if w in slug) >= 2


def _normalise_title(text: str | None) -> str:
    """Lowercase alphanumeric words only, for comparing two renderings of a title.

    Emoji and punctuation are dropped rather than preserved. The comparison in
    `_pick_posting_row` is a prefix one, so a title that differs only in its
    *first* character fails it completely — and a leading emoji is exactly that
    case. craigs1 published "🏠 Roofer in Hialeah …" on 2026-08-06, and if
    Craigslist's postings table had rendered that title without the emoji,
    neither string would have prefixed the other.

    Dropping punctuation costs nothing here: it is uninformative for identifying
    a listing, and the generator emits `&`, `%`, `|` and `->` freely, any of
    which Craigslist is free to escape differently in a table cell than in the
    form that accepted it.
    """
    import re

    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _pick_posting_row(
    rows: list[dict], *, post_id: str | None = None, expected_title: str | None = None
) -> str | None:
    """Choose our just-published posting out of the account's postings table.

    Every row here belongs to the signed-in account, so unlike the confirmation
    page there is no risk of picking up a stranger's ad — the only question is
    which of our own postings this is.

    A PostingID match is exact and therefore authoritative. Falling back to the
    title is for the runs where the receipt never showed an id: Craigslist
    truncates long titles in this table, so the comparison is a prefix one in
    either direction, and the first hit wins because the table is newest-first.
    """
    if post_id:
        for r in rows:
            if r.get("post_id") == post_id and "/d/" in (r.get("href") or ""):
                return r["href"]
        return None

    want = _normalise_title(expected_title)
    if not want:
        return None
    for r in rows:
        got = _normalise_title(r.get("title"))
        # A handful of characters is not enough to tell two roofing ads apart.
        if len(got) < 12 or "/d/" not in (r.get("href") or ""):
            continue
        if want.startswith(got) or got.startswith(want):
            return r["href"]
    return None


# Same DOM contract the stats scrape reads (stats._scrape_current_page).
_MY_POSTINGS_JS = """
() => {
    const out = [];
    for (const tr of document.querySelectorAll('tr.posting-row')) {
        const statusCell = tr.querySelector('td.status');
        const rawId = (statusCell && statusCell.dataset.postingid)
            || (tr.querySelector('td.postingID')?.textContent.trim() || '');
        const a = tr.querySelector('td.title a');
        out.push({
            post_id: String(rawId).replace(/\\D/g, ''),
            title: a ? a.textContent.trim() : '',
            href: a ? a.href : '',
        });
    }
    return out;
}
"""


def _resolve_via_my_postings(
    page: Page,
    *,
    post_id: str | None = None,
    expected_title: str | None = None,
    run: PostRun | None = None,
    attempts: int = 3,
) -> str | None:
    """Read the live /view/d/ URL off the account's own postings page.

    This is the only reliable way back from a PostingID to the URL a visitor
    would see. It costs one extra page load per post, on a page the session is
    already authenticated for.

    Retried because a posting can take a few seconds to appear in the table
    after checkout — which is exactly the window this runs in.
    """
    for attempt in range(1, attempts + 1):
        try:
            page.goto(CL_ACCOUNT_URL, wait_until="domcontentloaded", timeout=30_000)
            sleep_jitter(2.0)
            if (
                page.locator("input[type='password']").count()
                and not page.locator("tr.posting-row").count()
            ):
                if run is not None:
                    run.warn(
                        "the account's postings page asked for a login, so the "
                        "post URL could not be resolved there."
                    )
                return None
            try:
                page.wait_for_selector("tr.posting-row", timeout=10_000)
            except Exception:
                pass
            rows = page.evaluate(_MY_POSTINGS_JS)
            href = _pick_posting_row(
                rows, post_id=post_id, expected_title=expected_title
            )
            if href:
                found_by = f"PostingID={post_id}" if post_id else "title"
                logger.info(
                    f"  resolved {found_by} → {href} "
                    f"(account postings page, attempt {attempt})"
                )
                return href
        except Exception as e:
            logger.warning(f"  postings-page lookup attempt {attempt} failed: {e}")
        if attempt < attempts:
            read_pause(1500)
            sleep_jitter(4.0)
    return None


def _extract_post_url(
    page: Page,
    *,
    run: PostRun | None = None,
    expected_title: str | None = None,
    account: str | None = None,
) -> str | None:
    # 1. Best case: the confirmation page links straight to the live post.
    #
    # This used to take `.first` unconditionally, which is wrong: Craigslist
    # renders other people's listings on that page, so the first /d/ link is
    # not necessarily ours. A real run captured a stranger's Dodge Charger ad
    # as the post URL. Recording a foreign URL is worse than recording none —
    # it silently corrupts history, ghost checks and stats, and looks correct.
    # So we match candidates against the title and never guess.
    links = page.locator("a[href*='/d/']")
    candidates = []
    for i in range(min(links.count(), 20)):
        try:
            href = links.nth(i).get_attribute("href")
        except Exception:
            continue
        if href:
            candidates.append(href)

    if candidates:
        if not expected_title:
            return candidates[0]
        for href in candidates:
            if _url_matches_title(href, expected_title):
                return href
        # Links present but none are ours. Fall through to PostingID rather
        # than return someone else's ad.
        msg = (
            f"{len(candidates)} /d/ link(s) on the confirmation page, none "
            f"matching this ad's title — ignored them rather than record a "
            f"foreign listing."
        )
        if run is not None:
            run.warn(msg)
        else:
            logger.warning(f"  {msg}")
    # 2. The paid-category receipt page carries no link to the live post, only
    #    a PostingID. This used to resolve that id through
    #    /search/sss?postingID=<id>, which does not work and quietly never did:
    #    Craigslist ignores the parameter and serves an ordinary for-sale
    #    results page, so the first /d/ link on it was always a stranger's ad.
    #    The title guard below rejected it (correctly) and the search URL got
    #    saved instead — a link that opens a page of other people's listings.
    #
    #    The account's own postings page is the real mapping from PostingID to
    #    the live URL, and this session is already signed in to it.
    #
    #    Whatever happens next, we navigate away, so remember the receipt URL
    #    for the last resort below.
    receipt_url = page.url
    post_id = None
    try:
        import re
        m = re.search(r"PostingID\s*[:#]?\s*(\d{6,})", page.content())
        if m:
            post_id = m.group(1)
            logger.info(f"  extracted PostingID={post_id} from the receipt page")
    except Exception as e:
        logger.warning(f"  PostingID extraction failed: {e}")

    # No link to the ad and no receipt id. Every healthy run has one or the
    # other — a free posting returns on its /d/ link above, a paid one lands on
    # a receipt carrying a PostingID — so this is the signature of a publish
    # that did not complete, and the page in front of us is the only record of
    # why. Photograph it now: resolving by title navigates away, and after that
    # the evidence is gone for good.
    if post_id is None and not candidates:
        _capture_unverified(
            page, account=account, label="confirmation_unverified", run=run
        )

    # With no id we can still find the posting by title — every row on that
    # page is ours, and the newest matching one is the ad just published.
    href = _resolve_via_my_postings(
        page, post_id=post_id, expected_title=expected_title, run=run
    )
    if href:
        return href

    if post_id:
        # The id is real and the post published; only the URL is missing. Record
        # the search URL so the id still reaches history — post_id is parsed
        # back out of it, and without one the account is free to post again
        # inside its cooldown. The nightly stats sync reads the same postings
        # page and repairs the URL.
        search_url = f"{CL_SEARCH_URL}?postingID={post_id}"
        if run is not None:
            run.warn(
                f"PostingID={post_id} was not on the account's postings page — "
                f"saved a search URL that will not open the post. The link "
                f"repairs itself at the next stats sync."
            )
        return search_url

    # 3. Last resort — the receipt URL. It is session-bound and 404s once the
    # session ends, so the post is recorded under a URL that will not resolve
    # later, and carries no id. Worth flagging loudly.
    #
    # Photograph the postings page too. This is the moment we searched our own
    # account for the ad and did not find it, so what that table contained is
    # the evidence for whether the ad exists at all — and the pair of artifacts
    # (confirmation page above, this one here) is what turns the next
    # occurrence into a two-minute answer instead of a two-day one.
    _capture_unverified(
        page, account=account, label="postings_page_no_match", run=run
    )
    if run is not None:
        run.warn(
            "no /d/ link and no PostingID on the confirmation page, and the "
            "posting was not found on the account's postings page — recorded "
            "the raw page URL, which is probably session-bound and will 404. "
            "Craigslist may never have published this ad at all: the two runs "
            "that ended this way on 2026-08-05 and 2026-08-06 left no trace in "
            "the account's active, inactive or deleted tabs. Check the account "
            "before assuming this ad is live."
        )
    else:
        logger.warning("  no /d/ link and no PostingID found; falling back to page.url")
    return receipt_url
