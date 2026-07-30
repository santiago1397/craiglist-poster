"""post queue: drafts, guardrail settings, machine tokens, error events

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30

Schema notes (see DESIGN.md for the decisions behind these):

- `drafts` is the queue. One row per planned posting. `position` orders the
  queue *within an account* (decision 2); the claim picks head-of-queue from
  whichever eligible account idled longest (decision 3).
- Draft status is a small state machine:
      queued -> claimed -> posted
                        \\-> queued        (failed before any upload)
                        \\-> needs_attention (failed after uploads began)
  Decision 16 routes on `failed_step`, which the desktop now reports.
- `guardrail_settings` is a single-row table (enforced by `singleton`). The
  server owns the throttles (decision 14) but the desktop clamps whatever it
  receives to ceilings compiled into config.py.
- `machine_tokens` stores argon2 hashes of per-machine bearer tokens, separate
  from INGEST_BEARER_TOKEN so one can be revoked without breaking ingest
  (decision 19).
- `flow_errors` is the end-to-end error log. Every flow writes here, not just
  posting, so a silent failure anywhere is visible from the dashboard.
- pg_trgm backs the advisory similarity score (decision 6). Scored on head text
  only, so `drafts.body_head` is stored separately from the assembled body.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE drafts (
            id                  BIGSERIAL PRIMARY KEY,
            account             TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'queued',
            position            DOUBLE PRECISION NOT NULL,
            reviewed            BOOLEAN NOT NULL DEFAULT FALSE,

            -- Craigslist form fields. Mirrors craigslist_auto.content.Ad.
            title               TEXT NOT NULL,
            body                TEXT NOT NULL,
            body_head           TEXT,
            county              TEXT NOT NULL DEFAULT '',
            city                TEXT NOT NULL DEFAULT '',
            service_offered     TEXT NOT NULL DEFAULT '',
            postal_code         TEXT NOT NULL DEFAULT '',
            license_number      TEXT NOT NULL DEFAULT '',
            phone_number        TEXT NOT NULL DEFAULT '',

            -- Scheduling windows (decision 2). Both optional.
            not_before          TIMESTAMPTZ,
            expires_at          TIMESTAMPTZ,

            -- Claim bookkeeping (decision 4). Claim is atomic at slot time.
            claimed_at          TIMESTAMPTZ,
            claimed_by_machine  TEXT,

            -- Outcome
            posted_post_id      TEXT,
            posted_at           TIMESTAMPTZ,
            failed_step         TEXT,
            failed_message      TEXT,
            attempts            INTEGER NOT NULL DEFAULT 0,

            source              TEXT NOT NULL DEFAULT 'manual',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT drafts_status_check CHECK (
                status IN ('queued', 'claimed', 'posted', 'needs_attention', 'expired')
            )
        );
        CREATE INDEX idx_drafts_queue ON drafts(account, position)
            WHERE status = 'queued';
        CREATE INDEX idx_drafts_status ON drafts(status);
        CREATE INDEX idx_drafts_created ON drafts(created_at DESC);
        CREATE INDEX idx_drafts_head_trgm ON drafts USING GIN (body_head gin_trgm_ops);
        """
    )

    # Single-row settings table. The CHECK + unique index make a second row
    # impossible, so readers can rely on `SELECT ... LIMIT 1` being total.
    op.execute(
        """
        CREATE TABLE guardrail_settings (
            singleton                              BOOLEAN PRIMARY KEY DEFAULT TRUE,
            min_hours_between_posts_same_account   INTEGER NOT NULL DEFAULT 20,
            max_posts_per_day_total                INTEGER NOT NULL DEFAULT 3,
            max_posts_per_account_per_week         INTEGER NOT NULL DEFAULT 7,
            post_window_start_hour                 INTEGER NOT NULL DEFAULT 8,
            post_window_end_hour                   INTEGER NOT NULL DEFAULT 19,
            post_weekdays_only                     BOOLEAN NOT NULL DEFAULT TRUE,
            queue_depth_floor                      INTEGER NOT NULL DEFAULT 8,
            queue_depth_target                     INTEGER NOT NULL DEFAULT 15,
            updated_at                             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT guardrail_settings_singleton CHECK (singleton)
        );
        INSERT INTO guardrail_settings (singleton) VALUES (TRUE);
        """
    )

    op.execute(
        """
        CREATE TABLE machine_tokens (
            id            BIGSERIAL PRIMARY KEY,
            machine       TEXT NOT NULL,
            label         TEXT NOT NULL DEFAULT '',
            token_hash    TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at  TIMESTAMPTZ,
            revoked_at    TIMESTAMPTZ
        );
        CREATE INDEX idx_machine_tokens_active ON machine_tokens(machine)
            WHERE revoked_at IS NULL;
        """
    )

    # post_attempt now carries which draft it was for and the poster's internal
    # step name, so ingest can route a failed draft (decision 16).
    op.execute(
        """
        ALTER TABLE post_attempts
            ADD COLUMN draft_id    BIGINT,
            ADD COLUMN failed_step TEXT;
        CREATE INDEX idx_post_attempts_draft ON post_attempts(draft_id)
            WHERE draft_id IS NOT NULL;
        """
    )

    op.execute(
        """
        CREATE TABLE flow_errors (
            event_id      TEXT PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL,
            machine       TEXT NOT NULL,
            flow          TEXT NOT NULL,
            step          TEXT,
            account       TEXT,
            error_type    TEXT NOT NULL,
            error_message TEXT,
            context       JSONB NOT NULL DEFAULT '{}'::jsonb,
            received_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_flow_errors_ts ON flow_errors(ts DESC);
        CREATE INDEX idx_flow_errors_flow_ts ON flow_errors(flow, ts DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_post_attempts_draft")
    op.execute("ALTER TABLE post_attempts DROP COLUMN IF EXISTS failed_step")
    op.execute("ALTER TABLE post_attempts DROP COLUMN IF EXISTS draft_id")
    op.execute("DROP TABLE IF EXISTS flow_errors")
    op.execute("DROP TABLE IF EXISTS machine_tokens")
    op.execute("DROP TABLE IF EXISTS guardrail_settings")
    op.execute("DROP TABLE IF EXISTS drafts")
