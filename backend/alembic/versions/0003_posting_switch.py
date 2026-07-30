"""posting on/off switch

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30

A single operator-controlled kill switch for posting, stored alongside the
other server-owned throttles (decision 14).

It lives here rather than as a desktop setting deliberately: the server already
decides whether a claim is granted, so one flag in one place stops every
machine at once, takes effect on the next claim with no redeploy, and survives
a desktop restart. Pausing does not touch the queue — drafts keep their order
and resume exactly where they left off.

Defaults to TRUE so an upgrade never silently stops a working system.
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ADD COLUMN posting_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN paused_at       TIMESTAMPTZ,
            ADD COLUMN paused_reason   TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            DROP COLUMN IF EXISTS paused_reason,
            DROP COLUMN IF EXISTS paused_at,
            DROP COLUMN IF EXISTS posting_enabled;
        """
    )
