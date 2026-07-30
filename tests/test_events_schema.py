"""Round-trip the event schema the way the reporter and the backend actually use it."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from craigslist_auto.events import (  # noqa: E402
    EventBatch, EventEnvelope, FlowError, PostAttempt,
)

now = datetime.now(timezone.utc)
failures = []

# 1. PostAttempt with the new fields, serialised by reporter.emit()
pa = PostAttempt(
    ts=now, machine="desktop-eseva3c", account="craigs2", outcome="failed_form",
    draft_id=47, failed_step="form_body", error_type="failed_form",
    error_message="boom", duration_seconds=12.5,
)
raw = pa.model_dump_json()
back = EventEnvelope.model_validate_json('{"event": %s}' % raw).event
if back.draft_id != 47 or back.failed_step != "form_body":
    failures.append(f"PostAttempt round-trip lost fields: {back!r}")
if type(back).__name__ != "PostAttempt":
    failures.append(f"discriminator picked {type(back).__name__}")

# 2. New outcome literal must be accepted
try:
    PostAttempt(ts=now, machine="m", account="(none)", outcome="skipped_no_drafts")
except Exception as e:
    failures.append(f"skipped_no_drafts rejected: {e}")

# 3. FlowError must dispatch through the union (this is the new event type)
fe = FlowError(
    ts=now, machine="desktop-eseva3c", flow="queue_sync", step="fetch",
    error_type="QueueUnavailable", error_message="connection refused",
    context={"clamps": ["max_posts_per_day_total: server sent 30, clamped down to 5"]},
)
batch = EventBatch.model_validate({"events": [fe.model_dump(mode="json"), pa.model_dump(mode="json")]})
kinds = [type(e).__name__ for e in batch.events]
if kinds != ["FlowError", "PostAttempt"]:
    failures.append(f"batch dispatch wrong: {kinds}")
if batch.events[0].context["clamps"][0].startswith("max_posts") is False:
    failures.append("FlowError context did not survive")

# 4. extra="forbid" must still reject junk (guards against typo'd field names)
try:
    PostAttempt(ts=now, machine="m", account="a", outcome="posted", notafield=1)
    failures.append("extra=forbid is not being enforced")
except Exception:
    pass

# 5. Old-shape event (no draft_id/failed_step) must still validate — the outbox
#    can hold events written before this upgrade.
old = ('{"event_type":"post_attempt","event_id":"x","ts":"%s","machine":"m",'
       '"account":"craigs1","outcome":"posted","post_id":"123"}' % now.isoformat())
try:
    ev = EventEnvelope.model_validate_json('{"event": %s}' % old).event
    if ev.draft_id is not None:
        failures.append("legacy event got a non-null draft_id")
except Exception as e:
    failures.append(f"legacy outbox event rejected: {e}")

if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print("event schema OK: PostAttempt(draft_id, failed_step), skipped_no_drafts, "
      "FlowError union dispatch, extra=forbid, legacy compatibility")
