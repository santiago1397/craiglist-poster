"""A post that ends must leave this machine, not just this machine's sqlite.

`_freeze_missing_posts` has always written a `gone_from_active` row locally.
The row never went anywhere: `sync_all` emitted only the *scraped* rows, so the
dashboard's last word on an expired ad stayed its final 'Active' snapshot. This
pins the frozen rows to the outbox, in the shape ingest expects.

No database and no browser — sqlite and the event schema only.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ok = []

# Point the module's paths at a scratch dir before it opens anything real.
tmp = Path(tempfile.mkdtemp(prefix="cl_stats_test_"))
from craigslist_auto import stats as stats_mod

stats_mod.STATS_DB = tmp / "stats.sqlite"

from craigslist_auto.config import Account
from craigslist_auto.events import SnapshotTaken

account = Account(
    name="craigs1",
    email="craigs1@example.com",
    profile_dir=tmp / "profile",
    photo_dir=tmp / "photos",
    allowed_machine="test-machine",
)

conn = stats_mod._connect()
YESTERDAY, TODAY = "2026-07-30", "2026-07-31"

with conn:
    for pid, title in (("111", "still up"), ("222", "expired overnight")):
        conn.execute(
            "INSERT INTO posts(post_id, account, title, url, posted_ts, source) "
            "VALUES(?,?,?,?,?,'test')",
            (pid, account.name, title, f"https://cl/{pid}.html", "2026-07-01T12:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO snapshots(post_id, snapshot_date, snapshot_ts_utc, status, "
            "impressions, views, shares, favorites) VALUES(?,?,?,'Active',500,50,2,3)",
            (pid, YESTERDAY, "2026-07-30T10:00:00+00:00"),
        )

# Today's scrape saw only 111. 222 fell off the active tab.
with conn:
    frozen = stats_mod._freeze_missing_posts(conn, account, {"111"}, TODAY)

assert isinstance(frozen, list), f"expected rows back, got {type(frozen)}"
assert [r["post_id"] for r in frozen] == ["222"], frozen
ok.append("the post missing from today's scrape is returned, not just counted")

row = frozen[0]
assert row["status"] == "gone_from_active", row
assert row["impressions"] == 500 and row["views"] == 50, \
    "last known counters must carry forward, or the ad's final numbers are lost"
assert row["title"] == "expired overnight" and row["url"] == "https://cl/222.html", row
ok.append("it carries its last counters and its post dimension fields")

# The shape has to survive the event schema, or it dies inside the outbox where
# nobody looks.
ev = SnapshotTaken(
    ts="2026-07-31T11:00:00+00:00",
    snapshot_date=TODAY,
    post_id=row["post_id"],
    account=account.name,
    title=row["title"],
    url=row["url"],
    status=row["status"],
    impressions=row["impressions"],
    views=row["views"],
    shares=row["shares"],
    favorites=row["favorites"],
    area=row["area"],
    category=row["category"],
    expires_in_days=row["expires_in_days"],
    autorepost=row["autorepost"],
    freshness_note=row["freshness_note"],
)
assert ev.status == "gone_from_active" and ev.event_type == "snapshot_taken"
ok.append("a frozen row validates as a SnapshotTaken event ingest can accept")

# Freezing twice on the same day must not double-report.
with conn:
    again = stats_mod._freeze_missing_posts(conn, account, {"111"}, TODAY)
assert again == [], f"re-freezing the same day re-emitted: {again}"
ok.append("re-running the sync the same day freezes nothing a second time")

# A post already frozen must not thrash back to active on the next day's run.
with conn:
    tomorrow = stats_mod._freeze_missing_posts(conn, account, {"111"}, "2026-08-01")
assert tomorrow == [], f"an already-ended post was frozen again: {tomorrow}"
ok.append("an already-ended post stays ended instead of being re-reported daily")

conn.close()
print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
