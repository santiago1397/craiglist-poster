"""Whether a post is still live is derived, not read off the last status string.

Two things this pins down, both of which shipped broken:

1. Craigslist's status arrives as 'Active' *and* 'active' depending on when it
   was scraped, so any `= 'Active'` comparison classifies half the fleet wrong.
2. A post that ends does not get a row saying so — it simply stops appearing in
   the scrape. Its last snapshot keeps asserting 'Active' forever. Liveness has
   to come from comparing the post's newest snapshot against the *account's*
   newest sync, or an ad that expired in July still reads green in December.
"""
from datetime import date, datetime, timedelta, timezone

from app.db import conn, init_pool, tx
from app.services.queries import post_detail, posts_page, sync_freshness

init_pool()

TODAY = date.today()
ok = []


def reset():
    with tx() as c:
        c.execute("TRUNCATE posts, snapshots, ghost_checks CASCADE")


def add_post(post_id, account, posted_days_ago, title="an ad"):
    with tx() as c:
        c.execute(
            "INSERT INTO posts (post_id, account, title, url, posted_ts, source) "
            "VALUES (%s, %s, %s, %s, %s, 'test')",
            (post_id, account, title, f"https://x/{post_id}.html",
             datetime.now(timezone.utc) - timedelta(days=posted_days_ago)),
        )


def add_snapshot(post_id, days_ago, status, impressions=100):
    with tx() as c:
        c.execute(
            "INSERT INTO snapshots (post_id, snapshot_date, snapshot_ts_utc, "
            "status, impressions, views) VALUES (%s, %s, NOW(), %s, %s, 10)",
            (post_id, TODAY - timedelta(days=days_ago), status, impressions),
        )


def liveness_of(items):
    return {r["post_id"]: r["liveness"] for r in items}


# --- 1. capitalisation must not decide whether a post is live --------------
reset()
add_post("cap1", "craigs1", 10)
add_post("cap2", "craigs1", 10)
add_snapshot("cap1", 0, "Active")   # scraped as CL wrote it in July
add_snapshot("cap2", 0, "active")   # ...and as it writes it now
with conn() as c:
    got = liveness_of(posts_page(c, since="all")["items"])
assert got == {"cap1": "live", "cap2": "live"}, got
ok.append("'Active' and 'active' both read as live — case no longer decides it")

# --- 2. falling off the active tab ends a post, with no row saying so ------
reset()
add_post("still", "craigs1", 20)
add_post("gone", "craigs1", 40)
add_snapshot("gone", 5, "active")    # last seen 5 days ago...
add_snapshot("still", 5, "active")
add_snapshot("still", 0, "active")   # ...and the account synced again today
with conn() as c:
    got = liveness_of(posts_page(c, since="all")["items"])
assert got == {"still": "live", "gone": "ended"}, got
ok.append("a post absent from the newest sync reads as ended, though nothing "
          "ever wrote 'inactive' for it")

# --- 3. an explicit gone_from_active marker is honoured too ----------------
reset()
add_post("frozen", "craigs1", 40)
add_snapshot("frozen", 0, "gone_from_active")
with conn() as c:
    got = liveness_of(posts_page(c, since="all")["items"])
assert got == {"frozen": "ended"}, got
ok.append("the desktop's explicit gone_from_active marker reads as ended")

# --- 4. never scraped is its own answer, not 'ended' -----------------------
reset()
add_post("virgin", "craigs1", 1)
with conn() as c:
    page = posts_page(c, since="all")
assert liveness_of(page["items"]) == {"virgin": "unknown"}, page["items"]
ok.append("a post with no snapshot reads 'unknown', never a confident 'ended'")

# --- 5. the filters are not backwards ---------------------------------------
reset()
add_post("live1", "craigs1", 5)
add_post("dead1", "craigs1", 50)
add_snapshot("dead1", 3, "active")
add_snapshot("live1", 0, "active")
with conn() as c:
    live = posts_page(c, since="all", status_filter="active")
    ended = posts_page(c, since="all", status_filter="inactive")
assert [r["post_id"] for r in live["items"]] == ["live1"], live["items"]
assert [r["post_id"] for r in ended["items"]] == ["dead1"], ended["items"]
ok.append("'Live only' returns exactly the live posts and 'Ended only' the "
          "ended ones (both previously returned the opposite)")

# --- 6. counts survive the filter they describe ----------------------------
with conn() as c:
    filtered = posts_page(c, since="all", status_filter="active")
assert filtered["counts"] == {"live": 1, "ended": 1}, filtered["counts"]
ok.append("tallies ignore the liveness filter, so they don't collapse to the "
          "bucket you selected")

# --- 7. a stale scrape is reported, not papered over -----------------------
reset()
add_post("old", "craigs1", 30)
add_snapshot("old", 26, "active")
with conn() as c:
    fresh = sync_freshness(c)
assert fresh["stale"] is True, fresh
assert fresh["accounts"][0]["age_days"] == 26, fresh
ok.append("a 26-day-old scrape reports stale=True with the real age, so a "
          "green badge cannot imply 'checked recently'")

# --- 8. no snapshots anywhere is stale, not fresh --------------------------
reset()
add_post("none", "craigs1", 3)
with conn() as c:
    assert sync_freshness(c)["stale"] is True
ok.append("an account that has never synced counts as stale, not as up to date")

# --- 9. post_detail carries the same derivation as the list ----------------
reset()
add_post("d1", "craigs1", 40)
add_snapshot("d1", 4, "active")
add_snapshot("d1", 0, "gone_from_active")
with conn() as c:
    detail = post_detail(c, "d1")
assert detail["post"]["liveness"] == "ended", detail["post"]
assert detail["post"]["source"] == "test", "detail page still needs posts.source"
assert detail["sync"]["accounts"][0]["account"] == "craigs1"
ok.append("the detail page agrees with the list and keeps the fields it renders")

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
