"""let an operator post one named draft on demand

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-31

Posting has only ever been time-triggered: Task Scheduler fires `cl post` at
9/13/17 and the server picks the draft. There was no way to say "post *this*
one, now" — useful when you have just changed the copy or the images and want
to see one go out without waiting for the next slot.

A nullable timestamp on `drafts` rather than a jobs table, mirroring
`posts.hydrate_requested_at` (0010). The flag *is* the job: it is set by one
UPDATE, read by the machine poll, and cleared by event ingest when the attempt
comes back. Nothing to reconcile between two sources of truth.

`status` is deliberately untouched — a requested draft stays 'queued' until the
normal claim protocol takes it, so queue depth, ordering and every existing
status check keep working with no changes.

`post_requested_by` records who asked, so a post that went out off-schedule can
be explained months later.
"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE drafts ADD COLUMN post_requested_at  TIMESTAMPTZ;
        ALTER TABLE drafts ADD COLUMN post_requested_by  TEXT;
        ALTER TABLE drafts ADD COLUMN post_request_error TEXT;
        CREATE INDEX idx_drafts_post_requested ON drafts(post_requested_at)
            WHERE post_requested_at IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_drafts_post_requested;
        ALTER TABLE drafts DROP COLUMN IF EXISTS post_request_error;
        ALTER TABLE drafts DROP COLUMN IF EXISTS post_requested_by;
        ALTER TABLE drafts DROP COLUMN IF EXISTS post_requested_at;
        """
    )
