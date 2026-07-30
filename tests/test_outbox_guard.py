"""The claim guard must block on unsent *posted* attempts and nothing else.

Regression test for a real production state: 11 stale `skipped_no_eligible`
events sat unsent in the outbox with no reporter configured. Blocking the claim
on those would have stopped posting forever.
"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Point the outbox at a scratch file before importing the module that binds it.
_tmp = Path(tempfile.mkdtemp())
import craigslist_auto.config as config  # noqa: E402

config.DATA_DIR = _tmp
import craigslist_auto.reporter as reporter  # noqa: E402

reporter.OUTBOX_DB = _tmp / "outbox.sqlite"

from craigslist_auto.events import PostAttempt  # noqa: E402

now = datetime.now(timezone.utc)
ok = []

assert reporter.pending_count() == 0
assert reporter.pending_history_count() == 0
ok.append("empty outbox blocks nothing")

# The exact production shape: skips only.
for i in range(11):
    reporter.emit(PostAttempt(ts=now, machine="m", account="(none)",
                              outcome="skipped_no_eligible"))
assert reporter.pending_count() == 11, reporter.pending_count()
assert reporter.pending_history_count() == 0, \
    f"skips must not block the claim, got {reporter.pending_history_count()}"
ok.append("11 unsent 'skipped_no_eligible' events do NOT block the claim")

# Failures and dry runs likewise carry no history.
reporter.emit(PostAttempt(ts=now, machine="m", account="craigs1",
                          outcome="failed_form", failed_step="form_body"))
reporter.emit(PostAttempt(ts=now, machine="m", account="craigs1", outcome="dry_run"))
assert reporter.pending_history_count() == 0
ok.append("unsent failures and dry runs do NOT block the claim")

# A completed post is exactly what must block, because the server's cooldown
# maths would otherwise not know it happened.
reporter.emit(PostAttempt(ts=now, machine="m", account="craigs1",
                          outcome="posted", post_id="7788", draft_id=1))
assert reporter.pending_history_count() == 1, reporter.pending_history_count()
ok.append("an unsent 'posted' attempt DOES block the claim")

# Once delivered it must stop blocking.
with reporter._connect() as c:
    c.execute("UPDATE outbox SET sent_ts = ? WHERE 1", (now.isoformat(),))
assert reporter.pending_history_count() == 0
assert reporter.pending_count() == 0
ok.append("delivering the event clears the block")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
