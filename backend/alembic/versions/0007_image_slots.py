"""raise the per-post image limit to Craigslist's real maximum

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30

The slot cap was 5, inherited from the old desktop poster's photo rule. The
actual Craigslist limit is 24 per posting — one thumbnail plus 23 more — so the
constraint was rejecting attachments the site would have accepted.

How many a post *should* carry is a separate question from how many it *may*,
so the auto-attach range becomes a setting rather than a constant. It stays at
1-5 by default: each upload blocks until Craigslist confirms the thumbnail, so
24 photos is minutes of browser time per post, and that trade-off is the
operator's to make, not something to change silently under them.
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE draft_images DROP CONSTRAINT IF EXISTS draft_images_slot_check;
        ALTER TABLE draft_images ADD CONSTRAINT draft_images_slot_check
            CHECK (slot BETWEEN 1 AND 24);
        """
    )
    op.execute(
        """
        ALTER TABLE generation_settings
            ADD COLUMN photos_min INTEGER NOT NULL DEFAULT 1,
            ADD COLUMN photos_max INTEGER NOT NULL DEFAULT 5,
            -- Share of posts that deliberately go out with no pictures at all,
            -- so an account does not look mechanically identical every time.
            ADD COLUMN imageless_rate DOUBLE PRECISION NOT NULL DEFAULT 0.10;
        ALTER TABLE generation_settings
            ADD CONSTRAINT generation_photos_range CHECK (
                photos_min >= 0 AND photos_max >= photos_min AND photos_max <= 24
                AND imageless_rate >= 0 AND imageless_rate <= 1
            );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE generation_settings DROP CONSTRAINT IF EXISTS generation_photos_range;
        ALTER TABLE generation_settings
            DROP COLUMN IF EXISTS imageless_rate,
            DROP COLUMN IF EXISTS photos_max,
            DROP COLUMN IF EXISTS photos_min;
        """
    )
    op.execute(
        """
        ALTER TABLE draft_images DROP CONSTRAINT IF EXISTS draft_images_slot_check;
        ALTER TABLE draft_images ADD CONSTRAINT draft_images_slot_check
            CHECK (slot BETWEEN 1 AND 5);
        """
    )
