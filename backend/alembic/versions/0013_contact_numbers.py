"""move the call-tracking phone numbers out of code and into the dashboard

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-31

`reference.PHONE_NUMBERS` was a three-item Python list, so adding a number for
the call tracking meant editing a source file and redeploying. It is the kind of
thing that changes because a campaign changed, not because the software did.

A table rather than a JSON column on a settings singleton: numbers get retired
as often as they get added, and `active` retires one without deleting it —
drafts already carrying that number keep it, and the history of what published
under which number stays intact. `position` sets rotation order and which one
the composer prefills.

Seeded from the three numbers that were in code, so nothing changes on upgrade.
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE contact_numbers (
            id         BIGSERIAL PRIMARY KEY,
            number     TEXT NOT NULL UNIQUE,
            label      TEXT NOT NULL DEFAULT '',
            active     BOOLEAN NOT NULL DEFAULT TRUE,
            position   INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT contact_numbers_number_not_blank CHECK (btrim(number) <> '')
        );
        CREATE INDEX idx_contact_numbers_active ON contact_numbers(position)
            WHERE active;
        """
    )
    # The three that were compiled in. Ordered as they appeared there.
    op.execute(
        """
        INSERT INTO contact_numbers (number, label, position) VALUES
            ('(954) 634-7370', '', 1),
            ('(954) 634-7420', '', 2),
            ('(954) 634-7360', '', 3)
        ON CONFLICT (number) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS contact_numbers")
