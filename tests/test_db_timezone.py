"""Date boundaries in the database must be local ones.

`snapshots.snapshot_date` is written by the desktop scraper as a calendar date
in America/New_York. Postgres on the VPS runs in UTC, so `CURRENT_DATE` and
`timestamptz::date` roll over at 20:00 Eastern -- four hours before the data
does. Between 20:00 and midnight every comparison between the two was off by
one, silently and only in the evening:

  * `sync_freshness` reported a scrape that had just run as a day old, so
    "liveness is stale" warnings appeared on the Posts page every night.
  * `days_active` gained a day, deflating every per-day rate.
  * the young-post filter excluded posts a day early from rate rankings.

The pool now opens connections with `-c timezone=<DISPLAY_TZ>`, so the whole
app agrees with the data. This test fails if that is ever removed -- and it
fails at any hour, because it compares against the wall clock in the display
timezone rather than assuming the run happens during the broken window.

Needs the scratch database -- see tests/README.md.
"""
from datetime import datetime, timedelta

from app.config import get_settings
from app.db import conn, init_pool, tx
from app.services import queries

init_pool()
ok = []

TZ = get_settings().display_zoneinfo
today_local = datetime.now(TZ).date()

# ---------------------------------------------------------------------------
# The session itself
# ---------------------------------------------------------------------------

with conn() as c:
    session_tz = c.execute("SHOW TimeZone").fetchone()["TimeZone"]
    db_today = c.execute("SELECT CURRENT_DATE AS d").fetchone()["d"]

assert session_tz == get_settings().display_tz, (
    f"pooled connections report TimeZone={session_tz!r}, expected "
    f"{get_settings().display_tz!r} -- the `options` kwarg in db.init_pool is gone"
)
ok.append(f"pooled connections run in {session_tz}, not the server default")

assert db_today == today_local, (
    f"CURRENT_DATE is {db_today}, but today in {get_settings().display_tz} is "
    f"{today_local}. Date comparisons against snapshot_date will be off by one."
)
ok.append(f"CURRENT_DATE ({db_today}) agrees with the display timezone")

# ---------------------------------------------------------------------------
# The bug this was actually costing
# ---------------------------------------------------------------------------

with tx() as c:
    c.execute("TRUNCATE posts, snapshots CASCADE")
    c.execute(
        "INSERT INTO posts (post_id, account, title, url, posted_ts, source) "
        "VALUES ('tz1','craigs1','t','u', NOW() - INTERVAL '10 days','stats_sync')"
    )
    # Exactly what the scraper writes after a run today.
    c.execute(
        "INSERT INTO snapshots (post_id, snapshot_date, snapshot_ts_utc, status, "
        "impressions, views, shares, favorites) VALUES (%s,%s,NOW(),%s,%s,%s,0,0)",
        ("tz1", today_local, "Active", 100, 5),
    )

with conn() as c:
    fresh = queries.sync_freshness(c)

account = fresh["accounts"][0]
assert account["age_days"] == 0, (
    f"a scrape that ran today reports age_days={account['age_days']}, expected 0"
)
assert fresh["stale"] is False, "a scrape that ran today was reported as stale"
ok.append("a scrape that ran today reports age_days=0 at any hour")

# A genuinely old scrape must still be caught -- the fix must not make
# staleness undetectable, only correct.
with tx() as c:
    c.execute("DELETE FROM snapshots WHERE post_id = 'tz1'")
    c.execute(
        "INSERT INTO snapshots (post_id, snapshot_date, snapshot_ts_utc, status, "
        "impressions, views, shares, favorites) VALUES (%s,%s,NOW(),%s,%s,%s,0,0)",
        ("tz1", today_local - timedelta(days=5), "Active", 100, 5),
    )

with conn() as c:
    fresh = queries.sync_freshness(c)

assert fresh["accounts"][0]["age_days"] == 5, fresh["accounts"][0]
assert fresh["stale"] is True, "a five-day-old scrape was not reported as stale"
ok.append("a five-day-old scrape is still correctly reported as stale")

# ---------------------------------------------------------------------------
# `days_active`, which divides every per-day rate
# ---------------------------------------------------------------------------

with tx() as c:
    c.execute("TRUNCATE posts, snapshots CASCADE")
    # Posted at 22:00 local, which is already tomorrow in UTC. Under the old
    # session timezone `posted_ts::date` landed a day late and days_active came
    # out one short.
    local_2200 = datetime.combine(today_local - timedelta(days=4),
                                  datetime.min.time(), tzinfo=TZ).replace(hour=22)
    c.execute(
        "INSERT INTO posts (post_id, account, title, url, posted_ts, source) "
        "VALUES ('tz2','craigs1','t','u',%s,'stats_sync')",
        (local_2200,),
    )
    c.execute(
        "INSERT INTO snapshots (post_id, snapshot_date, snapshot_ts_utc, status, "
        "impressions, views, shares, favorites) VALUES (%s,%s,NOW(),%s,%s,%s,0,0)",
        ("tz2", today_local, "Active", 400, 20),
    )

with conn() as c:
    page = queries.posts_page(c, since="all")

post = page["items"][0]
assert post["days_active"] == 4, (
    f"a post made 4 days ago at 22:00 local reports days_active="
    f"{post['days_active']}, expected 4"
)
assert abs(post["views_per_day"] - 5.0) < 0.001, post["views_per_day"]
ok.append("days_active counts local days, so a 22:00 post is not aged a day early")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
