"""Every posting failure must reach the VPS with enough to debug it.

No database and no browser needed. These cover the three rules that decide
whether a failure is debuggable at all:

  1. `failed_step` is never None. The server routes the draft on it, and an
     unknown step is treated as post-upload — so a launch failure that consumed
     nothing would still cost a manual rescue.
  2. Every step the poster can die on is classified. A step the server has never
     heard of parks the draft, which is safe but wrong for the pre-upload half.
  3. A degraded post keeps outcome='posted' but carries its warnings, so the
     cooldown maths stays correct while the dashboard can still flag it.

Run:  uv run python tests/test_failure_reporting.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from craigslist_auto.events import PostAttempt  # noqa: E402
from craigslist_auto.poster import PostRun, PosterFailure  # noqa: E402

checks = 0


def check(label: str, condition: bool) -> None:
    global checks
    checks += 1
    if not condition:
        print(f"  FAIL  {label}")
        raise SystemExit(1)
    print(f"  OK  {label}")


# The server's copy of the routing table. Duplicated here rather than imported
# because the backend needs psycopg, which the desktop does not install — and a
# test that cannot run on the machine the code runs on is not much of a test.
PRE_UPLOAD_STEPS = {
    "build_ad",
    "launch", "warmup", "login_check", "open_post_form", "dismiss_reuse_prompt",
    "advance_to_type", "type_service_offered", "category_skilled_trade",
    "advance_to_form", "form_title", "form_zip", "form_city", "form_license",
    "form_phone", "form_body", "form_validation", "map_confirm",
}


# ---------------------------------------------------------------------------
print("\nfailed_step is always populated")
# ---------------------------------------------------------------------------

cli_src = (ROOT / "src" / "craigslist_auto" / "cli.py").read_text(encoding="utf-8")

# The bug this guards: `except Exception` used to report failed_step=None, which
# the server reads as post-upload and parks the draft.
check(
    "cli.py never reports failed_step=None",
    "failed_step=None" not in cli_src,
)
check(
    "the catch-all launch handler reports 'launch'",
    'failed_step="launch"' in cli_src,
)
check(
    "a malformed draft reports 'build_ad'",
    'failed_step="build_ad"' in cli_src,
)


# ---------------------------------------------------------------------------
print("\nevery poster step is classified by the server")
# ---------------------------------------------------------------------------

poster_src = (ROOT / "src" / "craigslist_auto" / "poster.py").read_text(encoding="utf-8")
# Every `step = "..."` assignment inside post_ad is a value the server may be
# asked to route on.
poster_steps = set(re.findall(r'^\s+step = "([a-z_]+)"', poster_src, re.M))
check("poster declares steps at all", len(poster_steps) > 10)

# Steps at or after photo_upload burn assets and must NOT be pre-upload.
ASSET_CONSUMING = {"photo_upload", "preview", "publish", "billing", "confirmation"}
for step in sorted(poster_steps):
    if step in ASSET_CONSUMING:
        check(f"'{step}' parks the draft (assets consumed)", step not in PRE_UPLOAD_STEPS)
    else:
        check(f"'{step}' requeues the draft (nothing consumed)", step in PRE_UPLOAD_STEPS)

check(
    "'build_ad' is routed even though the poster never sets it",
    "build_ad" in PRE_UPLOAD_STEPS,
)


# ---------------------------------------------------------------------------
print("\ndegradations travel with the result, not instead of it")
# ---------------------------------------------------------------------------

run = PostRun()
run.warn("photo 2/5 never rendered a thumbnail")
run.warn("subarea: no county match for 'Monroe'")
check("PostRun collects warnings", len(run.warnings) == 2)
check("warnings are truncated to a sane length", all(len(w) <= 300 for w in run.warnings))

# A degraded post is still a post: the server counts it against the cooldowns.
ev = PostAttempt(
    ts=datetime.now(timezone.utc),
    machine="desktop-test",
    account="craigs1",
    outcome="posted",
    post_id="7812345678",
    warnings=run.warnings,
    photos_confirmed=3,
    photos_attached=["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"],
    artifact_ids=["abc-123"],
)
check("a degraded post keeps outcome='posted'", ev.outcome == "posted")
check("its warnings survive the round trip", len(ev.warnings) == 2)
check(
    "the photo gap is visible (5 attached, 3 confirmed)",
    len(ev.photos_attached) == 5 and ev.photos_confirmed == 3,
)

# Reserialising through the outbox must not drop the new fields.
restored = PostAttempt.model_validate_json(ev.model_dump_json())
check("warnings survive outbox serialisation", restored.warnings == ev.warnings)
check("artifact_ids survive outbox serialisation", restored.artifact_ids == ["abc-123"])

# An outbox written by a build that predates these fields must still validate,
# or events queued during an upgrade are lost.
legacy = (
    '{"event_id":"e1","ts":"2026-07-31T12:00:00Z","event_type":"post_attempt",'
    '"machine":"m","account":"craigs1","outcome":"posted"}'
)
old = PostAttempt.model_validate_json(legacy)
check("a pre-upgrade event still validates", old.outcome == "posted")
check("its warnings default to empty", old.warnings == [])
check("its photos_confirmed defaults to None", old.photos_confirmed is None)


# ---------------------------------------------------------------------------
print("\nfailures carry their evidence")
# ---------------------------------------------------------------------------

failed = PostRun()
failed.warn("thumbnail count mismatch")
failed.artifact_ids.extend(["shot-1", "html-1"])
exc = PosterFailure("form_body", RuntimeError("selector gone"), run=failed)
check("PosterFailure remembers the step", exc.step == "form_body")
check("PosterFailure carries the artifacts", exc.run.artifact_ids == ["shot-1", "html-1"])
check(
    "PosterFailure carries warnings seen before the failure",
    exc.run.warnings == ["thumbnail count mismatch"],
)
# A PosterFailure raised without a run must still be safe to read from.
bare = PosterFailure("launch", RuntimeError("lease held"))
check("a bare PosterFailure still has an empty run", bare.run.artifact_ids == [])


# ---------------------------------------------------------------------------
print("\nthe poster spools evidence to the VPS, not just to disk")
# ---------------------------------------------------------------------------

check(
    "poster imports the artifact spooler",
    "from . import artifacts" in poster_src,
)
check(
    "the failure dump captures for upload",
    "artifacts.capture_page(" in poster_src,
)
check(
    "post_attempt can carry artifact ids",
    "artifact_ids" in (ROOT / "src" / "craigslist_auto" / "events.py").read_text(encoding="utf-8"),
)


print(f"\n{checks} checks passed")
