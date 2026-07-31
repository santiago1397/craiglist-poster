"""The browser lease must actually serialise access to a Chrome profile.

Two flows in one profile directory is not a race we survive: Chrome either
refuses to launch or corrupts the profile. Before DESIGN_EDITS decision 27 the
only thing keeping `post` and `stats-sync` apart was that their Scheduled Tasks
fired at different times, which an opportunistic edit worker breaks.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from craigslist_auto import lease  # noqa: E402

failures = []

# Start clean — a lease left behind by an earlier run would mask everything.
lease.LOCK_PATH.unlink(missing_ok=True)

# 1. A second flow must be refused while the first holds it.
with lease.acquire("post", account="craigs1", blocking=False):
    if lease.current_holder() is None:
        failures.append("current_holder() returned None while the lease was held")
    try:
        with lease.acquire("edit", blocking=False):
            failures.append("edit acquired the lease while post held it")
    except lease.LeaseBusy:
        pass

# 2. Releasing must let the next flow in.
if lease.current_holder() is not None:
    failures.append("lease was not released when the block exited")
with lease.acquire("edit", blocking=False):
    pass

# 3. A blocking caller must give up rather than hang forever.
with lease.acquire("post", blocking=False):
    started = time.monotonic()
    try:
        with lease.acquire("stats_sync", blocking=True, timeout=1.0, poll=0.1):
            failures.append("blocking acquire succeeded while the lease was held")
    except lease.LeaseBusy:
        waited = time.monotonic() - started
        if waited < 0.9:
            failures.append(f"blocking acquire gave up after only {waited:.2f}s")

# 4. A lease whose holder died must be reclaimable, or one crash during a post
#    would wedge every browser flow on the machine until someone noticed.
lease.LOCK_PATH.write_text(json.dumps({
    "flow": "post",
    "account": "craigs1",
    "pid": 999999,
    "host": "dead-machine",
    "acquired_at": time.time() - 10_000,
    "heartbeat_at": time.time() - 10_000,   # long past STALE_AFTER
}), encoding="utf-8")

if lease.current_holder() is not None:
    failures.append("a stale lease was reported as a live holder")
try:
    with lease.acquire("edit", blocking=False) as info:
        if info["flow"] != "edit":
            failures.append("stale lease was not taken over correctly")
except lease.LeaseBusy:
    failures.append("could not reclaim a stale lease — a crash would wedge the machine")

# 5. A *fresh* heartbeat must NOT be reclaimed, or a slow post gets trampled
#    mid-upload by an edit that thought it had died.
lease.LOCK_PATH.write_text(json.dumps({
    "flow": "post",
    "account": "craigs1",
    "pid": 999999,
    "host": "other",
    "acquired_at": time.time() - 10_000,   # held a long time...
    "heartbeat_at": time.time(),           # ...but still alive
}), encoding="utf-8")
try:
    with lease.acquire("edit", blocking=False):
        failures.append("reclaimed a lease that was still heartbeating")
except lease.LeaseBusy:
    pass

lease.LOCK_PATH.unlink(missing_ok=True)

if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print(
    "lease OK: mutual exclusion holds, release frees it, blocking acquire times "
    "out, dead holders are reclaimed, live holders are not"
)
