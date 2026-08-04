"""narrow the posting window to 08:00-17:00

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-04

Eight posts a weekday between 08:00 and 17:00 rather than 08:00 and 18:00.

The window is the easy half. The hard half is that eight fires no longer fit on
whole hours: `TASK_FIRE_TIMES` moved to 45-minute spacing, because a block
spanning three hours puts each account's morning and afternoon slots exactly
five hours apart, and the five-hour cooldown is measured from when the post
*landed* -- a few minutes after the fire that produced it. Exactly five hours
therefore measures as slightly under, and every afternoon fire gets refused.
At 45 minutes each block spans 2h15m and every pair is six hours apart, which
is the same hour of clearance the old hourly schedule had.

`post_window_end_hour = 17` means the last post may start at 16:59: the check is
`start <= hour < end`. The last fire is 16:15, so there is three quarters of an
hour of slack for a slow run before the window closes on it.

Nothing here touches `min_hours_between_posts_same_account`. It stays at 5.
"""
from __future__ import annotations

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN post_window_end_hour SET DEFAULT 17;

        -- The default governs a fresh install only; the live singleton was
        -- written long ago and would otherwise keep posting until 18:00.
        UPDATE guardrail_settings
        SET post_window_start_hour = 8,
            post_window_end_hour   = 17,
            updated_at             = NOW()
        WHERE singleton;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN post_window_end_hour SET DEFAULT 18;

        UPDATE guardrail_settings
        SET post_window_start_hour = 8,
            post_window_end_hour   = 18,
            updated_at             = NOW()
        WHERE singleton;
        """
    )
