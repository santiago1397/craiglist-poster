"""turn live-post editing on

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-31

`edits_enabled` has defaulted FALSE since 0010, because the Craigslist edit-form
selectors in `src/craigslist_auto/editor.py` were inferred from the posting form
and the account page rather than observed on the edit form itself. The operator
has decided to turn editing on and iterate against production with the tracing
added alongside this change.

The switch is data, not code, and that is the point: it can be reversed from the
dashboard under Settings -> Guardrails, or with one UPDATE, and takes effect on
the desktop's next config fetch — no redeploy, no restart. The reason lands in
`edits_paused_reason` and surfaces verbatim in Diagnostics' global blocks.

    UPDATE guardrail_settings
    SET edits_enabled = FALSE, edits_paused_reason = '<why>';

The desktop's own compiled default in `config.py` stays FALSE deliberately. It
governs only when the server says nothing, and a machine that cannot reach the
VPS should not start editing live listings on its own initiative.

Everything else that gates an edit is unchanged: the edit window, the per-post
cooldown, the per-account daily cap, the lifetime cap, and the master posting
switch, which pauses editing too.
"""
from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN edits_enabled SET DEFAULT TRUE;

        UPDATE guardrail_settings
        SET edits_enabled       = TRUE,
            edits_paused_reason = NULL,
            updated_at          = NOW()
        WHERE singleton;

        -- 24 slots, matching drafts. `post_desired_images` was created in 0010
        -- copying `draft_images`' original 1..5 rule, which 0007 had already
        -- raised to 1..24 — so a live posting could hold five images where the
        -- draft that produced it held twenty-four. Editing one meant throwing
        -- nineteen photos away.
        ALTER TABLE post_desired_images
            DROP CONSTRAINT IF EXISTS post_desired_images_slot_check;
        ALTER TABLE post_desired_images
            ADD CONSTRAINT post_desired_images_slot_check CHECK (slot BETWEEN 1 AND 24);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ALTER COLUMN edits_enabled SET DEFAULT FALSE;

        UPDATE guardrail_settings
        SET edits_enabled       = FALSE,
            edits_paused_reason = 'rolled back by migration 0015 downgrade',
            updated_at          = NOW()
        WHERE singleton;

        -- Drop anything above slot 5 first, or the tightened constraint cannot
        -- be validated against rows this migration made legal.
        DELETE FROM post_desired_images WHERE slot > 5;
        ALTER TABLE post_desired_images
            DROP CONSTRAINT IF EXISTS post_desired_images_slot_check;
        ALTER TABLE post_desired_images
            ADD CONSTRAINT post_desired_images_slot_check CHECK (slot BETWEEN 1 AND 5);
        """
    )
