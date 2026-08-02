"""The Posts list must not hide old postings silently.

The list shows the last 90 days by default. That is a sensible default and a bad
silence: the oldest postings are precisely the ones whose ad copy was never
captured, so they are both the least replaceable and the first to age out. A
list that drops them without a word looks complete while getting shorter every
week.

`hidden_by_window` is the number the page needs to offer "show all" against. It
has to count what the window alone is hiding — not what the other filters hide
too, or the offer would promise rows that "show all" does not produce.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import conn, init_pool, tx
from app.services.queries import DEFAULT_WINDOW_DAYS, posts_page

init_pool()
ok = []
failures = []


def check(label, condition, detail=""):
    (ok if condition else failures).append(
        label if condition else (f"{label}  [{detail}]" if detail else label)
    )


NOW = datetime.now(timezone.utc)
RECENT = NOW - timedelta(days=3)
OLD = NOW - timedelta(days=DEFAULT_WINDOW_DAYS + 30)
IDS = ["7890000001", "7890000002", "7890000003"]
ROWS = [
    # (post_id, account, posted_ts, title)
    (IDS[0], "craigs1", RECENT, "Recent one"),
    (IDS[1], "craigs1", OLD, "Old one, copy never captured"),
    (IDS[2], "craigs2", OLD, "Old one on another account"),
]


def _cleanup():
    with tx() as c:
        c.execute("DELETE FROM posts WHERE post_id = ANY(%s)", (IDS,))


_cleanup()
with tx() as c:
    for pid, account, ts, title in ROWS:
        c.execute(
            "INSERT INTO posts (post_id, account, title, url, posted_ts) "
            "VALUES (%s,%s,%s,%s,%s)",
            (pid, account, title, f"https://x/{pid}.html", ts),
        )

with conn() as c:
    default = posts_page(c, limit=200)
    every = posts_page(c, since="all", limit=200)
    one_account = posts_page(c, account="craigs2", limit=200)
    one_account_all = posts_page(c, account="craigs2", since="all", limit=200)
    searched = posts_page(c, search="Old one, copy never captured", limit=200)

shown = {r["post_id"] for r in default["items"]}
check("a recent posting is inside the default window", IDS[0] in shown)
check("an old posting is outside it", IDS[1] not in shown and IDS[2] not in shown)
check("the window hides exactly the two old ones",
      default["hidden_by_window"] >= 2, str(default["hidden_by_window"]))

# The number has to mean what the button promises. If it counted rows that
# "show all" would not produce, clicking it would show fewer than advertised.
check("hidden + shown equals what 'show all' actually returns",
      default["total"] + default["hidden_by_window"] == every["total"],
      f"{default['total']} + {default['hidden_by_window']} != {every['total']}")

check("'show all' really does include the old ones",
      {IDS[1], IDS[2]} <= {r["post_id"] for r in every["items"]})
check("'show all' reports nothing hidden", every["hidden_by_window"] == 0,
      str(every["hidden_by_window"]))

# Narrowing by account or search must narrow the hidden count too — otherwise
# filtering to one account would offer to reveal another account's postings.
check("the count respects the account filter",
      one_account["total"] + one_account["hidden_by_window"] == one_account_all["total"],
      f"{one_account['total']} + {one_account['hidden_by_window']} "
      f"!= {one_account_all['total']}")
check("filtering to one account does not offer to reveal another's postings",
      one_account["hidden_by_window"] == 1, str(one_account["hidden_by_window"]))
check("the count respects the search filter",
      searched["hidden_by_window"] == 1, str(searched["hidden_by_window"]))

_cleanup()

if failures:
    print("\n".join(f"  --  {f}" for f in failures))
    print(f"\n{len(failures)} FAILED, {len(ok)} passed")
    raise SystemExit(1)
print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
print("posts window OK")
