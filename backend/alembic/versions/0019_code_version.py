"""record which code a machine is running

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-02

Twice a fix has been merged, pulled and restarted, and the daemon has gone on
running the previous version — once because the pull landed in a different
checkout from the one the scheduled task runs. Both times it was diagnosed by
fingerprinting the code from side effects in the step trail: a message whose
wording had changed, a field a newer version always populates.

The daemon now reports its commit at startup and it is stored here, so "is the
machine on the code I just shipped" is a query rather than an inference.
"""
from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scheduler_configs ADD COLUMN IF NOT EXISTS code_version TEXT;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE scheduler_configs DROP COLUMN IF EXISTS code_version;")
