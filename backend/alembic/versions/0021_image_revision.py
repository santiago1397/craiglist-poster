"""track whether the staged gallery has changed since it was last published

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-02

The reconcile decided whether to touch the gallery by comparing counts:

    images_differ = manage_images and len(photos) != len(live_images)

Swap twenty-four photos for twenty-four different photos and that is 24 == 24,
so nothing happens and the run reports `no_change` — which is precisely what an
operator replacing a gallery is trying to do.

Counts were used because identity is not available: images already on Craigslist
when we hydrate are URLs on their servers with no sha256 we can match against
our own. So the question is answered from our side instead. `image_rev` is
bumped whenever the staged set is touched, `live_image_rev` is set to it when an
apply succeeds, and a difference between them means the operator has changed the
gallery since we last pushed one.

Existing rows start equal, so a posting whose images have not been touched since
this migration is not treated as needing a replace.
"""
from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_desired_state
            ADD COLUMN IF NOT EXISTS image_rev      INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS live_image_rev INTEGER NOT NULL DEFAULT 0;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_desired_state
            DROP COLUMN IF EXISTS live_image_rev,
            DROP COLUMN IF EXISTS image_rev;
        """
    )
