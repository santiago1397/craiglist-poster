"""apply a queued edit on demand

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-01

An edit had no way to be run from the dashboard. It waited for the edit window,
the per-post cooldown, and the desktop's next poll — fine for steady state, and
useless while bringing the feature up, when you have just staged a change and
want to watch it happen.

Mirrors `drafts.post_requested_at` from 0014 exactly: a nullable timestamp is
the job. Set by one UPDATE, read by the machine poll, cleared when the attempt
comes back. Nothing to reconcile between two sources of truth.

What a request changes is *when*, not *whether*. It skips the edit window and
the per-post cooldown, because both are pacing rules and the operator is
standing there asking for it now. It does not skip the master posting switch,
`edits_enabled`, the per-account daily cap, or the per-post lifetime cap — those
are safety, and a button that quietly stepped over them would be the thing that
gets an account banned.

`reconcile_requested_by` records who asked, so an edit that went out off-cadence
can be explained later.
"""
from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_desired_state
            ADD COLUMN IF NOT EXISTS reconcile_requested_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS reconcile_requested_by TEXT,
            ADD COLUMN IF NOT EXISTS reconcile_request_error TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_desired_state
            DROP COLUMN IF EXISTS reconcile_request_error,
            DROP COLUMN IF EXISTS reconcile_requested_by,
            DROP COLUMN IF EXISTS reconcile_requested_at;
        """
    )
