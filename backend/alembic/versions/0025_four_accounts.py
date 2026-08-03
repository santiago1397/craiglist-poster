"""four accounts, eight posts a day, two per account

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-03

A fourth account (`craigs4`) joins the rotation and throughput goes from three
ads a day to eight — two per account, one in a morning block (08/09/10/11) and
one in an afternoon block (14/15/16/17), inside an 08:00-18:00 window.

Adding the account itself needs no schema at all: there is no accounts table,
every account list in this codebase is a `SELECT DISTINCT account` over observed
data, and the registry lives in `src/craigslist_auto/config.py` on the desktop.
What needed changing is the throttles, because a fourth account against the old
caps would have added exactly zero ads a day — a 20-hour cooldown and a 3/24h
total meant three accounts already saturated it, and a fourth would only have
made each one post less often.

Two of the numbers below are one higher than the figure they enforce, and this
is the part worth reading twice.

**The daily and weekly caps are counted over a rolling window, not a calendar
period.** `_posts_last_24h_total` counts `posted_ts >= NOW() - 24h`, and a post
lands a few minutes *after* the fire that produced it. So yesterday's post at
the same clock time is always still inside the window, and at every fire in
steady state the rolling count already reads 8. `max_posts_per_day_total = 8`
would therefore refuse every fire, all day, and look exactly like a broken
scheduler. The cap has to be the calendar target plus one:

    max_posts_per_day_total        = 9   -> 8 posts per calendar day
    max_posts_per_account_per_week = 11  -> 10 posts per account per week

`max_posts_per_account_per_day` is new and is the opposite: a **calendar-day**
count in DISPLAY_TZ, so it is set to the figure it enforces. Making it rolling
would reproduce the trap above and shut the morning slot permanently.

It exists because "two per account per day" was otherwise only implied, not
stated. A 5-hour cooldown against a 10-hour window does make a third post
arithmetically impossible today — but that guarantee is invisible, lives in an
arithmetic argument rather than in the code, and evaporates the moment somebody
drags the window end to 19:00 in Settings. Now the rule is a rule, the dashboard
can say "daily cap: 2/2 today", and the window is free to move.

The cooldown drops 20h -> 5h to allow the second post at all. The morning and
afternoon fires for a given account are six hours apart, so five leaves an hour
of slack for a slow run or a displaced slot without dropping a fire.

Queue depth moves 8/15 -> 10/20 per account: a floor of 10 is five days of that
account's own posting, and `4 x (20 - 10) = 40` is inside the generator's
per-run limit of 50, so a single top-up run still refills all four accounts.
"""
from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ADD COLUMN IF NOT EXISTS max_posts_per_account_per_day INTEGER NOT NULL DEFAULT 2;

        ALTER TABLE guardrail_settings
            ALTER COLUMN min_hours_between_posts_same_account SET DEFAULT 5,
            ALTER COLUMN max_posts_per_day_total              SET DEFAULT 9,
            ALTER COLUMN max_posts_per_account_per_week       SET DEFAULT 11,
            ALTER COLUMN post_window_end_hour                 SET DEFAULT 18,
            ALTER COLUMN queue_depth_floor                    SET DEFAULT 10,
            ALTER COLUMN queue_depth_target                   SET DEFAULT 20;

        -- The defaults above only govern a fresh install. The live singleton row
        -- was written by 0002 and would keep the old values forever, so it is
        -- updated to match: a deploy that changed the schema but left the
        -- running system on 3/day would be the worst of both.
        UPDATE guardrail_settings
        SET min_hours_between_posts_same_account = 5,
            max_posts_per_account_per_day        = 2,
            max_posts_per_day_total              = 9,
            max_posts_per_account_per_week       = 11,
            post_window_start_hour               = 8,
            post_window_end_hour                 = 18,
            queue_depth_floor                    = 10,
            queue_depth_target                   = 20,
            updated_at                           = NOW()
        WHERE singleton;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN min_hours_between_posts_same_account SET DEFAULT 20,
            ALTER COLUMN max_posts_per_day_total              SET DEFAULT 3,
            ALTER COLUMN max_posts_per_account_per_week       SET DEFAULT 7,
            ALTER COLUMN post_window_end_hour                 SET DEFAULT 19,
            ALTER COLUMN queue_depth_floor                    SET DEFAULT 8,
            ALTER COLUMN queue_depth_target                   SET DEFAULT 15;

        UPDATE guardrail_settings
        SET min_hours_between_posts_same_account = 20,
            max_posts_per_day_total              = 3,
            max_posts_per_account_per_week       = 7,
            post_window_start_hour               = 8,
            post_window_end_hour                 = 19,
            queue_depth_floor                    = 8,
            queue_depth_target                   = 15,
            updated_at                           = NOW()
        WHERE singleton;

        ALTER TABLE guardrail_settings
            DROP COLUMN IF EXISTS max_posts_per_account_per_day;
        """
    )
