"""set the edit caps for something a person drives

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-02

0010 gave editing the same caps as posting — 3 a day per account, 5 in a post's
lifetime, 48 hours between edits of one post — on the reading that editing a
live posting is not obviously safer than creating one.

That was the wrong comparison. Posting's caps exist because the volume of *new
listings* is what gets an account banned. Editing your own ad is ordinary
behaviour Craigslist expects, and here it is driven by hand: somebody presses
Apply now and watches it happen. Three a day is not a safety limit for that, it
is an obstruction — and because a failed attempt consumes a slot, three failed
debugging runs used up the day.

What actually stops a broken selector retrying forever is the per-post cooldown:
a reconcile that fails pre-mutation returns to `pending` and cannot be offered
again until it passes. The daily cap is a backstop against runaway automation,
so it is set where it does that job without standing in front of the operator.

Existing installs are moved to the new values only where they still hold 0010's
defaults, so a deliberately tightened setting is left alone.
"""
from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN min_hours_between_edits_same_post SET DEFAULT 2,
            ALTER COLUMN max_edits_per_account_per_day     SET DEFAULT 20,
            ALTER COLUMN max_edits_per_post_lifetime       SET DEFAULT 50;

        UPDATE guardrail_settings
        SET min_hours_between_edits_same_post =
                CASE WHEN min_hours_between_edits_same_post = 48 THEN 2
                     ELSE min_hours_between_edits_same_post END,
            max_edits_per_account_per_day =
                CASE WHEN max_edits_per_account_per_day = 3 THEN 20
                     ELSE max_edits_per_account_per_day END,
            max_edits_per_post_lifetime =
                CASE WHEN max_edits_per_post_lifetime = 5 THEN 50
                     ELSE max_edits_per_post_lifetime END,
            updated_at = NOW()
        WHERE singleton;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN min_hours_between_edits_same_post SET DEFAULT 48,
            ALTER COLUMN max_edits_per_account_per_day     SET DEFAULT 3,
            ALTER COLUMN max_edits_per_post_lifetime       SET DEFAULT 5;
        """
    )
