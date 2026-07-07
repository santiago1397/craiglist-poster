from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from patchright.sync_api import BrowserContext, Page, sync_playwright

from .config import CL_SITE, LOGS_DIR, Account
from .content import Ad, mark_content_used, mark_photos_used
from .covers import is_cover_path, mark_cover_used
from .human import human_click, human_type, read_pause, scroll_a_bit, sleep_jitter

FAILURES_DIR = LOGS_DIR / "failures"


def _dump_failure(page: Page, account_name: str, step: str, err: Exception) -> None:
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
def launch_account(account: Account, headless: bool = False):
    """
    Launch a persistent context bound to this account's profile.
    Profile persists cookies, localStorage, IndexedDB → CL sees a stable browser.
    """
    account.profile_dir.mkdir(parents=True, exist_ok=True)
    # Deterministic viewport per account so fingerprint doesn't shift between runs
    vp = VIEWPORTS[hash(account.name) % len(VIEWPORTS)]
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
    page.goto("https://accounts.craigslist.org/login/home", wait_until="domcontentloaded")
    sleep_jitter(2.0)
    return "settings" in page.url or page.locator("text=postings").count() > 0


def post_ad(account: Account, ad: Ad, *, headless: bool = False, dry_run: bool = False) -> str | None:
    """
    Post one ad. Returns the post URL if successful.
    NOTE: CL's posting UI changes periodically. If selectors break, run with
    headless=False and step through visually to update them.
    """
    logger.info(f"[{account.name}] posting: {ad.title!r}  photos={len(ad.photos)}")
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
                return None

            step = "open_post_form"
            logger.debug(f"step: {step}")
            page.goto("https://post.craigslist.org/c/mia", wait_until="domcontentloaded")
            read_pause(400)

            step = "dismiss_reuse_prompt"
            logger.debug(f"step: {step}")
            _dismiss_reuse_prompt(page)

            step = "advance_to_type"
            logger.debug(f"step: {step}")
            _advance_until(page, expect_selector="input[name='id'][value='so']", county=ad.county, max_steps=5)

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
            _advance_until(page, expect_selector="input[name='PostingTitle']", county=ad.county, max_steps=4)

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

            if ad.city:
                step = "form_city"
                logger.debug(f"step: {step}")
                geo = page.locator("input[name='geographic_area']")
                if geo.count():
                    human_type(geo, ad.city)
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
                # Check 'show_phone_ok' checkbox to enable phone fields
                show_phone = page.locator("input[name='show_phone_ok']")
                if show_phone.count() and not show_phone.is_checked():
                    human_click(page, show_phone)
                    sleep_jitter(0.4)
                phone = page.locator("input[name='contact_phone']")
                if phone.count():
                    human_type(phone, ad.phone_number)
                    sleep_jitter(0.3)
                # Enable both call and text by default
                for cb_name in ("contact_phone_ok", "contact_text_ok"):
                    cb = page.locator(f"input[name='{cb_name}']")
                    if cb.count() and not cb.is_checked():
                        human_click(page, cb)
                        sleep_jitter(0.2)

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
            _continue(page)

            step = "map_confirm"
            logger.debug(f"step: {step}")
            _continue(page, optional=True)

            step = "photo_upload"
            logger.info(f"[{account.name}] photo_upload: {len(ad.photos)} file(s) queued")
            for idx, photo in enumerate(ad.photos):
                tag = "COVER" if is_cover_path(photo) else "photo"
                size_kb = photo.stat().st_size / 1024 if photo.exists() else -1
                logger.info(
                    f"  slot {idx + 1}: [{tag}] {photo.name}  ({size_kb:.0f} KB)  path={photo}"
                )
            if ad.photos:
                page.wait_for_selector("input[type='file']", timeout=30_000)
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
                        logger.warning(
                            f"    ✗ did NOT observe a new thumbnail after {elapsed:.1f}s  "
                            f"(thumbs: {before} → {after}).  Either the upload failed OR "
                            f"the thumbnail selector doesn't match CL's current DOM."
                        )
                        # Dump the page so we can pick the right selector.
                        _dump_photo_page(page, account.name, f"slot{i}_no_thumb")
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
                            logger.error(
                                f"    KEEPING cover claimed (not marking used): "
                                f"upload did not confirm. File: {photo.name}"
                            )
                    sleep_jitter(0.8, 0.3)
                final_thumbs = _count_uploaded_thumbs(page)
                logger.info(
                    f"[{account.name}] photo_upload done: expected {len(ad.photos)} "
                    f"thumbnail(s), CL shows {final_thumbs}"
                )
                if final_thumbs != len(ad.photos):
                    logger.warning(
                        f"  thumbnail count mismatch — CL may have rejected an upload "
                        f"or the count selector missed some. Inspect the page."
                    )
                _log_thumbnail_order(page)
                sleep_jitter(1.5)
            _click_text(page, "done with images")

            step = "preview"
            logger.debug(f"step: {step}")
            page.wait_for_load_state("domcontentloaded")
            read_pause(1200)
            scroll_a_bit(page)
            sleep_jitter(2.0)

            if dry_run:
                logger.warning(f"[{account.name}] DRY RUN — not clicking publish.")
                return None

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

            post_url = _extract_post_url(page)
            mark_photos_used(ad.photos)
            mark_content_used(ad)
            logger.success(f"[{account.name}] published: {post_url}")
            return post_url
        except Exception as e:
            _dump_failure(page, account.name, step, e)
            raise


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


def _dump_photo_page(page: Page, account_name: str, label: str) -> None:
    """Save HTML + screenshot of the current photo-upload page for inspection."""
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
            _select_subarea(page, county)

        btn = page.locator("button[type='submit']").first
        if not btn.count():
            logger.debug(f"  no submit button on page; stopping at step {i}")
            return
        logger.debug(f"  advancing intermediate page (step {i + 1})")
        human_click(page, btn)
        page.wait_for_load_state("domcontentloaded")
        sleep_jitter(1.2)
    logger.warning(f"  _advance_until exhausted {max_steps} steps without reaching target")


def _select_subarea(page: Page, county: str | None) -> None:
    """Pick the miami-dade / broward / palm-beach radio that matches `county`."""
    c = (county or "").lower()
    if "palm" in c:
        match = "palm beach"
    elif "broward" in c:
        match = "broward"
    elif "miami" in c or "dade" in c:
        match = "miami"
    else:
        # Default: pick the first radio so the form can proceed
        match = None
        logger.warning(f"  subarea: no county match for {county!r}, picking first option")
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


def _continue(page: Page, optional: bool = False) -> None:
    """Click the 'continue' button. If optional and missing, skip."""
    # Try specific continue selectors first; some pages also have a disabled
    # secondary submit button (e.g. geoverify's "find").
    candidates = [
        "button.continue:not([disabled])",
        "button[name='go'][value='continue']:not([disabled])",
        "button[type='submit']:not([disabled])",
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
            # No known button on this page — dump for inspection
            _dump_failure(
                page,
                account_name,
                "billing_unknown_page",
                RuntimeError(f"no billing button matched on {page.url}"),
            )
            raise RuntimeError(
                f"billing flow stuck — no matching button found. "
                f"See logs/failures/ for screenshot of the page."
            )

    if _on_billing_page(page):
        _dump_failure(
            page,
            account_name,
            "billing_too_many_steps",
            RuntimeError("billing flow did not complete within max_steps"),
        )
        raise RuntimeError("billing flow exceeded max_steps without completing")


def _extract_post_url(page: Page) -> str | None:
    # 1. Best case: confirmation page has a direct /d/ link to the live post.
    link = page.locator("a[href*='/d/']").first
    if link.count():
        return link.get_attribute("href")
    # 2. Paid-category receipt page: extract PostingID and resolve it to the
    #    canonical /d/ URL by loading the search-by-posting-ID page. That page
    #    renders the matching post at the top with a normal /d/... link, so
    #    we hand back the same URL a public visitor would copy from their
    #    address bar — what dashboards and ghost-checks should compare on.
    #    The /k/...?s=billing receipt URL itself is session-bound and 404s
    #    once the session ends, so we must NOT save it.
    try:
        import re
        html = page.content()
        m = re.search(r"PostingID\s*[:#]?\s*(\d{6,})", html)
        if m:
            post_id = m.group(1)
            search_url = f"https://miami.craigslist.org/search/sss?postingID={post_id}"
            logger.info(f"  extracted PostingID={post_id} from receipt page; resolving → {search_url}")
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                read_pause(800)
                sleep_jitter(1.0)
                canonical = page.locator("a[href*='/d/']").first
                if canonical.count():
                    href = canonical.get_attribute("href")
                    logger.info(f"  resolved PostingID={post_id} → {href}")
                    return href
                # Post may not be indexed yet, or could be ghosted already.
                # Either way, the search URL still resolves the post live.
                logger.warning(
                    f"  PostingID={post_id} search page returned no /d/ link "
                    f"(post not yet indexed, or ghosted). Saving search URL."
                )
                return search_url
            except Exception as e:
                logger.warning(
                    f"  PostingID={post_id} resolution failed ({e!r}); "
                    f"falling back to search URL."
                )
                return search_url
    except Exception as e:
        logger.warning(f"  PostingID extraction failed: {e}")
    # 3. Last resort — return current URL (may be session-bound, not durable).
    logger.warning(f"  no /d/ link and no PostingID found; falling back to page.url")
    return page.url
