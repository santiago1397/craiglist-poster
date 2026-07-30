"""image stack: generated + uploaded images, and their attachment to drafts

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30

Decisions 12, 13 and 20.

Images are content-addressed by sha256, so the same bytes uploaded twice occupy
one row and one file. Bytes live on a Docker volume behind authenticated routes,
not in Postgres — a few GB of photos would make dumps painful for no benefit.

`status` implements the pending shelf: generated images land as 'pending' and
only become usable once approved, so nothing reaches Craigslist that was not
looked at. `owner_account` implements claim-on-attach — NULL while unclaimed,
set permanently when the image is attached to a draft, after which no other
account may use it. That is the anti-detection invariant: Craigslist notices the
same photo appearing under different sellers.
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE images (
            id             BIGSERIAL PRIMARY KEY,
            sha256         TEXT NOT NULL UNIQUE,
            storage_path   TEXT NOT NULL,
            mime           TEXT NOT NULL DEFAULT 'image/jpeg',
            bytes_size     INTEGER NOT NULL,
            width          INTEGER,
            height         INTEGER,

            source         TEXT NOT NULL DEFAULT 'generated',  -- generated | uploaded
            status         TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | rejected
            kind           TEXT NOT NULL DEFAULT 'photo',      -- photo | cover

            -- NULL until attached to a draft, then permanent (decision 13).
            owner_account  TEXT,

            prompt         TEXT,
            provider       TEXT,
            model          TEXT,
            cost_usd       NUMERIC(10, 5),

            -- Last time this image was actually published, for the reuse
            -- cooldown within its owning account.
            used_at        TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT images_status_check CHECK (status IN ('pending','approved','rejected')),
            CONSTRAINT images_source_check CHECK (source IN ('generated','uploaded')),
            CONSTRAINT images_kind_check   CHECK (kind IN ('photo','cover'))
        );
        CREATE INDEX idx_images_status ON images(status);
        CREATE INDEX idx_images_pool ON images(status, owner_account, kind);
        CREATE INDEX idx_images_created ON images(created_at DESC);
        """
    )

    # Slot 1 is the Craigslist thumbnail — the highest-leverage visual on the
    # listing — so ordering is explicit rather than left to insertion order.
    op.execute(
        """
        CREATE TABLE draft_images (
            draft_id  BIGINT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
            image_id  BIGINT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            slot      INTEGER NOT NULL,
            PRIMARY KEY (draft_id, slot),
            UNIQUE (draft_id, image_id),
            CONSTRAINT draft_images_slot_check CHECK (slot BETWEEN 1 AND 5)
        );
        CREATE INDEX idx_draft_images_image ON draft_images(image_id);
        """
    )

    op.execute(
        """
        ALTER TABLE generation_settings
            ADD COLUMN image_model      TEXT NOT NULL DEFAULT 'image-01',
            ADD COLUMN image_api_base   TEXT NOT NULL DEFAULT 'https://api.minimax.io/v1',
            ADD COLUMN image_prompt     TEXT NOT NULL DEFAULT '',
            ADD COLUMN image_aspect     TEXT NOT NULL DEFAULT '4:3',
            ADD COLUMN image_cost_usd   NUMERIC(10,5) NOT NULL DEFAULT 0.0035,
            ADD COLUMN images_generated INTEGER NOT NULL DEFAULT 0;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE generation_settings
            DROP COLUMN IF EXISTS images_generated,
            DROP COLUMN IF EXISTS image_cost_usd,
            DROP COLUMN IF EXISTS image_aspect,
            DROP COLUMN IF EXISTS image_prompt,
            DROP COLUMN IF EXISTS image_api_base,
            DROP COLUMN IF EXISTS image_model;
        """
    )
    op.execute("DROP TABLE IF EXISTS draft_images")
    op.execute("DROP TABLE IF EXISTS images")
