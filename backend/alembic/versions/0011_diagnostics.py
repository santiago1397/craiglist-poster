"""diagnostics: post artifacts, degradation warnings, error acknowledgement

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-31

Everything here exists to answer one question from the dashboard instead of from
psql: *what is broken right now, and what does the page look like?*

- `post_attempts.artifact_ids` — posting was the only flow whose failure
  screenshots never left the Windows box. `post_edit_attempts` and `posts` have
  carried artifact ids since 0010; posting is the flow that runs unattended
  three times a day, so it needed them more, not less.

- `post_attempts.warnings` / `photos_confirmed` — a post could publish with two
  of its five photos missing, or filed under a county nobody chose, and the row
  said `outcome='posted'` with no hint. These record the degradation *without*
  touching `outcome`, because eligibility and cooldown maths read
  `WHERE outcome = 'posted'` and must keep counting a degraded post as a post.

- `flow_errors.acknowledged_at` — the table has been write-only since 0002.
  Once it has a UI, an unbounded list of every error ever recorded is unusable;
  acknowledging is what separates "still broken" from "seen and handled".

- `idx_drafts_claimed` — supports the stale-claim sweep. Without a reaper, a
  desktop that died between claiming a draft and reporting the outcome left the
  draft 'claimed' forever: invisible to `queue_depths`, which counts only
  'queued', so the account reported "queue empty" and quietly stopped posting.
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_attempts
            ADD COLUMN artifact_ids     JSONB   NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN warnings         JSONB   NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN photos_confirmed INTEGER
        """
    )
    # Partial index: the dashboard's "published, but degraded" filter only ever
    # asks for rows that have warnings, and those are the rare ones.
    op.execute(
        """
        CREATE INDEX idx_post_attempts_degraded ON post_attempts(ts DESC)
        WHERE jsonb_array_length(warnings) > 0
        """
    )

    op.execute("ALTER TABLE flow_errors ADD COLUMN acknowledged_at TIMESTAMPTZ")
    op.execute(
        """
        CREATE INDEX idx_flow_errors_open ON flow_errors(ts DESC)
        WHERE acknowledged_at IS NULL
        """
    )

    op.execute(
        """
        CREATE INDEX idx_drafts_claimed ON drafts(claimed_at)
        WHERE status = 'claimed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_drafts_claimed")
    op.execute("DROP INDEX IF EXISTS idx_flow_errors_open")
    op.execute("ALTER TABLE flow_errors DROP COLUMN IF EXISTS acknowledged_at")
    op.execute("DROP INDEX IF EXISTS idx_post_attempts_degraded")
    op.execute(
        """
        ALTER TABLE post_attempts
            DROP COLUMN IF EXISTS artifact_ids,
            DROP COLUMN IF EXISTS warnings,
            DROP COLUMN IF EXISTS photos_confirmed
        """
    )
