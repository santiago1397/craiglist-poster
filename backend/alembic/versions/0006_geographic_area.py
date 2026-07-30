"""separate the Craigslist 'city or neighborhood' field from the structured city

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-30

`drafts.city` is a structured value: it picks the seed, drives the reference
dropdown, and feeds the copy prompt. It was also being typed straight into
Craigslist's free-text "city or neighborhood" box, which conflated two
different jobs.

That box accepts anything — a neighbourhood, a list of nearby towns, a service
area — and widening it is a cheap way to catch more searches. So it gets its own
column, free text, falling back to `city` when blank. Nothing breaks for
existing drafts: NULL means "use the city", exactly as before.
"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE drafts ADD COLUMN geographic_area TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE drafts DROP COLUMN IF EXISTS geographic_area")
