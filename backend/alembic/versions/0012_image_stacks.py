"""separate the cover and photo stacks, and reserve assigned images

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-31

Three changes, all of them consequences of one decision: covers are chosen by
hand and photos are not.

1. **Posts carry 23 photos.** `photos_min`/`photos_max` move to 23/23 rather
   than being replaced by a constant, so the count stays dialable from Settings
   and `imageless_rate` keeps working — a share of drafts still take nothing at
   all, and those need no cover either.

2. **The image top-up loop ships disabled.** Sustaining 24-image posts needs
   roughly a thousand standing photos, which only a background generator can
   supply. That generator is written but off, because the prompts it would
   spend money on are still being tuned. Same pattern as `edits_enabled`.

3. **Assignment becomes exclusive**, which needs an index that can answer "is
   this image attached to a live draft?" cheaply. `idx_draft_images_image`
   (0005) covers the lookup; what was missing is the draft-side status filter,
   so the partial index below is on `drafts(status)` for the live set.

No column is added to `images`: the five buckets a stack is displayed in —
pending, available, assigned, published, rejected — are all derivable from
`status`, `used_at` and the `draft_images` join. Storing a bucket would create
a second source of truth that could disagree with the attachment table.
"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE generation_settings
            ADD COLUMN image_topup_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            -- Available (approved, unassigned, uncooled) photos below which the
            -- loop generates, and the depth it fills to.
            ADD COLUMN image_stack_floor   INTEGER NOT NULL DEFAULT 200,
            ADD COLUMN image_stack_target  INTEGER NOT NULL DEFAULT 400,
            -- Cap per cycle so a flipped switch cannot spend the budget in one
            -- pass. At 0.0035/image the default ceiling is ~$0.07 a cycle.
            ADD COLUMN image_topup_batch   INTEGER NOT NULL DEFAULT 20;
        ALTER TABLE generation_settings
            ADD CONSTRAINT generation_image_stack_range CHECK (
                image_stack_floor >= 0
                AND image_stack_target >= image_stack_floor
                AND image_topup_batch BETWEEN 1 AND 100
            );
        """
    )

    # Every post now carries the Craigslist maximum. Applied to the live row as
    # well as the default, because the singleton already exists in production.
    op.execute(
        """
        ALTER TABLE generation_settings
            ALTER COLUMN photos_min SET DEFAULT 23,
            ALTER COLUMN photos_max SET DEFAULT 23;
        UPDATE generation_settings SET photos_min = 23, photos_max = 23;
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_drafts_live_status ON drafts(id)
            WHERE status IN ('queued', 'claimed', 'needs_attention');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_drafts_live_status;
        ALTER TABLE generation_settings
            ALTER COLUMN photos_min SET DEFAULT 1,
            ALTER COLUMN photos_max SET DEFAULT 5;
        ALTER TABLE generation_settings
            DROP CONSTRAINT IF EXISTS generation_image_stack_range;
        ALTER TABLE generation_settings
            DROP COLUMN IF EXISTS image_topup_batch,
            DROP COLUMN IF EXISTS image_stack_target,
            DROP COLUMN IF EXISTS image_stack_floor,
            DROP COLUMN IF EXISTS image_topup_enabled;
        """
    )
