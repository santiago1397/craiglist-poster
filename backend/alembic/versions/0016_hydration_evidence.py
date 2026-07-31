"""keep the evidence from reading a live edit form

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-31

Hydration is the first thing anyone points at Craigslist's edit form, and the
only thing that answers the question the whole feature depends on: do the
selectors in `editor.SEL` match the real DOM?

It was also the one path whose evidence never left the posting machine. The
desktop captured a screenshot and an HTML dump, uploaded both to `artifacts`,
recorded a per-selector census on its step trail — and then `record_content`
kept the scraped fields and dropped all of it. The artifacts stayed in the table
with nothing pointing at them; the census stayed in `logs/run.log` on a Windows
box the operator is generally not sitting at.

Two JSONB columns rather than a table: this is at most a few dozen rows per
hydration, it is overwritten wholesale on the next read, and it is only ever
fetched alongside the post it belongs to. `post_edit_attempts.steps` stores the
reconcile equivalent the same way.

Both are cleared at the start of every hydration, so what you are looking at
always describes the read that produced the content beside it.
"""
from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE posts
            ADD COLUMN IF NOT EXISTS hydrate_steps        JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS hydrate_artifact_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE posts
            DROP COLUMN IF EXISTS hydrate_artifact_ids,
            DROP COLUMN IF EXISTS hydrate_steps;
        """
    )
