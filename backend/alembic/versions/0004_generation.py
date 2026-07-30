"""AI draft generation: seed ads + generation settings

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30

`seed_ads` holds the workbook rows (decision 9). It serves two jobs at once:

  1. The *brief* for AI generation — county, city, zip, phone, license.
  2. The *fallback copy* — if the model is unreachable or returns something
     unusable, the row's own title and body are used verbatim, so the queue
     keeps filling and posting never stops because an API had a bad night.

It lives in Postgres rather than being read from data/*.xlsx because generation
runs on the VPS, and `data/` is gitignored — the workbook is not there and
should not be, since it is live operator content.

`generation_settings` is a singleton holding the model, the editable prompts and
the shared keyword tail. Storing the tail once rather than per row keeps ~6,000
identical words out of every generated draft's round trip.
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE seed_ads (
            id                BIGSERIAL PRIMARY KEY,
            county            TEXT NOT NULL,
            city              TEXT NOT NULL,
            postal_code       TEXT NOT NULL DEFAULT '',
            phone_number      TEXT NOT NULL DEFAULT '',
            license_number    TEXT NOT NULL DEFAULT '',
            service_offered   TEXT NOT NULL DEFAULT '',
            -- Verbatim fallback copy, used when the model is unavailable.
            fallback_title    TEXT NOT NULL,
            fallback_head     TEXT NOT NULL,
            active            BOOLEAN NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_seed_ads_active ON seed_ads(active) WHERE active;
        CREATE INDEX idx_seed_ads_account_pool ON seed_ads(county, city);
        """
    )

    op.execute(
        """
        CREATE TABLE generation_settings (
            singleton       BOOLEAN PRIMARY KEY DEFAULT TRUE,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            model           TEXT NOT NULL DEFAULT 'MiniMax-Text-01',
            api_base        TEXT NOT NULL DEFAULT 'https://api.minimax.io/v1',
            temperature     DOUBLE PRECISION NOT NULL DEFAULT 0.9,
            system_prompt   TEXT NOT NULL DEFAULT '',
            user_template   TEXT NOT NULL DEFAULT '',
            tail_template   TEXT NOT NULL DEFAULT '',
            -- Observability for the background loop.
            last_run_at     TIMESTAMPTZ,
            last_source     TEXT,          -- 'ai' | 'fallback' | NULL
            last_error      TEXT,
            generated_total INTEGER NOT NULL DEFAULT 0,
            fallback_total  INTEGER NOT NULL DEFAULT 0,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT generation_settings_singleton CHECK (singleton)
        );
        INSERT INTO generation_settings (singleton) VALUES (TRUE);
        """
    )

    # Which seed produced a draft, and whether the model or the fallback wrote
    # it — so a silent slide into all-fallback output is visible rather than
    # something you notice weeks later in the ad copy.
    op.execute(
        """
        ALTER TABLE drafts
            ADD COLUMN seed_ad_id BIGINT REFERENCES seed_ads(id) ON DELETE SET NULL,
            ADD COLUMN generated_by TEXT;
        CREATE INDEX idx_drafts_generated_by ON drafts(generated_by);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_drafts_generated_by")
    op.execute("ALTER TABLE drafts DROP COLUMN IF EXISTS generated_by")
    op.execute("ALTER TABLE drafts DROP COLUMN IF EXISTS seed_ad_id")
    op.execute("DROP TABLE IF EXISTS generation_settings")
    op.execute("DROP TABLE IF EXISTS seed_ads")
