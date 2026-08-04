"""back an account off after a failed post instead of retrying it every fire

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-04

An account whose card is not set up fails at the `billing` step. Nothing about
that failure was recorded anywhere the rotation looks: `_last_post_by_account`
reads the `posts` table, and a failed attempt never reaches it. So the broken
account stayed the longest-idle one, was picked again at the very next fire,
and failed again.

At eight fires a weekday that is eight consecutive failures on one account, and
because `billing` is after `photo_upload`, every one of them parks its draft
for a human and burns the images it had already uploaded. The other three
accounts post nothing all day, because the broken one wins the rotation every
time.

Two columns, two different problems:

**`failure_backoff_minutes` (60)** covers transient failures - a timeout, a
selector that missed, a browser lease conflict. Long enough to skip the next
fire, short enough that a blip does not cost the day.

**`billing_failure_backoff_minutes` (720)** covers the one that does not
self-heal. A missing payment method is a configuration problem: retrying it in
an hour fails exactly the same way, and each attempt costs another parked draft
and another set of burned images. Twelve hours means one attempt per day, which
is enough to notice and not enough to hurt.

Neither is a kill switch. The account resumes on its own once the backoff
expires, so a genuinely transient failure needs no human at all, and a real one
shows up in Diagnostics either way.
"""
from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ADD COLUMN IF NOT EXISTS failure_backoff_minutes         INTEGER NOT NULL DEFAULT 60,
            ADD COLUMN IF NOT EXISTS billing_failure_backoff_minutes INTEGER NOT NULL DEFAULT 720;

        UPDATE guardrail_settings
        SET failure_backoff_minutes         = 60,
            billing_failure_backoff_minutes = 720,
            updated_at                      = NOW()
        WHERE singleton;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            DROP COLUMN IF EXISTS billing_failure_backoff_minutes,
            DROP COLUMN IF EXISTS failure_backoff_minutes;
        """
    )
