"""Guards that keep the edit flow from damaging live postings.

Covers the three things most likely to go wrong quietly:
  - the content hash that decides whether an edit is stale (decision 26)
  - edit guardrail clamping, including "editing stays off by default"
  - the posting-slot guard that keeps edits out of posting's way (decision 28)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from craigslist_auto.config import (  # noqa: E402
    CEILING_MAX_EDITS_PER_ACCOUNT_PER_DAY, CEILING_MAX_EDITS_PER_POST_LIFETIME,
    FLOOR_MIN_HOURS_BETWEEN_EDITS_SAME_POST, clamp_guardrails, compiled_guardrails,
)

failures = []

# ---------------------------------------------------------------- content hash
# Imported lazily: editor pulls in patchright, which the no-DB test tier should
# not require just to check a hash function.
try:
    from craigslist_auto.editor import content_hash
except Exception as e:  # pragma: no cover
    content_hash = None
    print(f"(skipping content_hash checks: {e})")

if content_hash is not None:
    base = {
        "title": "Metal Roofing in Hollywood",
        "body": "Line one\nLine two",
        "county": "Broward", "city": "Hollywood", "service_offered": "Roofing",
        "postal_code": "33020", "license_number": "CCC1334317",
        "phone_number": "(954) 634-7370",
    }
    if content_hash(base) != content_hash(dict(base)):
        failures.append("content_hash is not deterministic")

    # Craigslist round-trips textareas with inconsistent trailing whitespace.
    # If that moved the hash, every edit would park as stale and the feature
    # would be unusable.
    noisy = dict(base, body="Line one   \nLine two\n", title="  Metal Roofing in Hollywood ")
    if content_hash(noisy) != content_hash(base):
        failures.append("content_hash changed on whitespace-only differences")

    # A real edit must move it, or staleness detection is vacuous.
    changed = dict(base, body="Line one\nLine three")
    if content_hash(changed) == content_hash(base):
        failures.append("content_hash did not change when the body changed")

# ------------------------------------------------------------------- clamping
# Editing must be off unless the server explicitly turns it on: the Craigslist
# edit form has never been exercised, so the safe default is "do nothing".
if compiled_guardrails().edits_enabled:
    failures.append("edits_enabled is compiled ON — it must default to OFF")

g, _ = clamp_guardrails({})
if g.edits_enabled:
    failures.append("a server that says nothing must leave editing OFF")

g, notes = clamp_guardrails({"max_edits_per_account_per_day": 99})
if g.max_edits_per_account_per_day != CEILING_MAX_EDITS_PER_ACCOUNT_PER_DAY:
    failures.append(f"99 edits/day not clamped: got {g.max_edits_per_account_per_day}")
if not notes:
    failures.append("clamping edits produced no note, so nothing would be reported")

g, _ = clamp_guardrails({"min_hours_between_edits_same_post": 0})
if g.min_hours_between_edits_same_post != FLOOR_MIN_HOURS_BETWEEN_EDITS_SAME_POST:
    failures.append("a 0h edit cooldown was not clamped up to the floor")

g, _ = clamp_guardrails({"max_edits_per_post_lifetime": 500})
if g.max_edits_per_post_lifetime != CEILING_MAX_EDITS_PER_POST_LIFETIME:
    failures.append("lifetime edit cap not clamped")

# A legal tightening passes through untouched and silently.
g, notes = clamp_guardrails({
    "edits_enabled": True, "max_edits_per_account_per_day": 1,
    "min_hours_between_edits_same_post": 72,
})
if not g.edits_enabled or g.max_edits_per_account_per_day != 1:
    failures.append("a legal tightening was not honoured")
if g.min_hours_between_edits_same_post != 72:
    failures.append("a longer cooldown than the floor was wrongly clamped")
if notes:
    failures.append(f"legal values produced clamp notes: {notes}")

# --------------------------------------------------------- posting-slot guard
try:
    from craigslist_auto.edit_worker import near_posting_slot
except Exception as e:  # pragma: no cover
    near_posting_slot = None
    print(f"(skipping posting-slot checks: {e})")

if near_posting_slot is not None:
    ET = ZoneInfo("America/New_York")

    def at(hour: int, minute: int) -> datetime:
        return datetime(2026, 7, 30, hour, minute, tzinfo=ET).astimezone(timezone.utc)

    # An edit started at 08:58 could still be typing into a form at 09:00.
    if not near_posting_slot(at(8, 58)):
        failures.append("08:58 was not treated as too close to the 09:00 slot")
    if not near_posting_slot(at(13, 5)):
        failures.append("13:05 was not treated as too close to the 13:00 slot")
    if near_posting_slot(at(11, 30)):
        failures.append("11:30 was wrongly treated as near a posting slot")
    if near_posting_slot(at(22, 0)):
        failures.append("22:00 was wrongly treated as near a posting slot")

# --- finding a posting whose id is not Craigslist's id ----------------------
# `stats._extract_post_id` falls back to the base62 token in Craigslist's
# /view/d/<slug>/<token> share URL when the numeric id is not in the URL. That
# token is never what `data-postingid` holds on the account page, so a post
# recorded straight from a publish cannot be found by id — it has to be found
# by its link. Getting this wrong reports a live, visible ad as "gone".
try:
    from craigslist_auto.editor import _url_token
except Exception as e:  # pragma: no cover
    _url_token = None
    print(f"(skipping url-token checks: {e})")

if _url_token is not None:
    cases = [
        ("https://www.craigslist.org/view/d/miami-emergency-roof/xvbywnthPhu59jd5tMPpGP",
         "xvbywnthPhu59jd5tMPpGP", "base62 share token"),
        ("https://miami.craigslist.org/mdc/trd/d/slug/7950716823.html",
         "7950716823", "legacy numeric url"),
        ("https://www.craigslist.org/view/d/slug/AbC123_-xyz9876/",
         "AbC123_-xyz9876", "trailing slash"),
        ("https://www.craigslist.org/view/d/slug/xvbywnthPhu59jd5tMPpGP?x=1",
         "xvbywnthPhu59jd5tMPpGP", "query string stripped"),
        ("https://example.com/short", None, "too short to match safely"),
        (None, None, "no url"),
    ]
    for url, want, label in cases:
        got = _url_token(url)
        if got != want:
            failures.append(f"url token ({label}): expected {want!r}, got {got!r}")


# --- the gallery's hidden template must never be counted -------------------
# ?s=editimage renders a 25th <figure class="imgbox template"> with no id and no
# <img>, cloned by the uploader for each new image. If the thumbnail selector
# counts it, a 24-image replace deletes 24 real photos and then fails its own
# "thumbnails == photos" assertion at 25 vs 24 — raising with mutated=True,
# which is `degraded_live`, on a posting that was healthy a minute earlier.
try:
    from craigslist_auto.editor import SEL as _SEL
except Exception as e:  # pragma: no cover
    _SEL = None
    print(f"(skipping gallery-selector checks: {e})")

if _SEL is not None:
    if ".template" not in _SEL["image_thumb"]:
        failures.append(
            "image_thumb no longer excludes the gallery's hidden template figure"
        )
    if "imgbox" not in _SEL["image_thumb"]:
        failures.append("image_thumb no longer targets the observed figure.imgbox")


# --- a matching gallery must not be torn down and rebuilt -------------------
# The replace was gated on `manage_images` rather than on whether the images
# actually differed, so taking control of a gallery meant every later text-only
# edit deleted 24 images and re-uploaded them: 48 avoidable operations against a
# live posting, and 48 chances to fail halfway with it empty.
try:
    import inspect as _inspect
    from craigslist_auto import editor as _editor
    _recon = _inspect.getsource(_editor.reconcile_post)
    _replace = _inspect.getsource(_editor._replace_images)
except Exception as e:  # pragma: no cover
    _recon = _replace = None
    print(f"(skipping replace-gating checks: {e})")

if _recon is not None:
    if "if manage_images and images_differ:" not in _recon:
        failures.append(
            "the image replace is no longer gated on images_differ — a matching "
            "gallery will be deleted and re-uploaded on every text edit"
        )
    # `degraded_live` is the loudest alarm the system has. It must mean images
    # really were taken away, not that a selector went missing.
    if "mutated=removed > 0" not in _replace:
        failures.append(
            "an upload failure claims mutation unconditionally — a missing "
            "upload control would report degraded_live on an untouched gallery"
        )


if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print(
    "edit guards OK: hash ignores whitespace but catches real changes, editing "
    "defaults OFF and clamps to compiled ceilings, posting slots are protected"
)
