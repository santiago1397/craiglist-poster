"""post editing: desired state, edit attempts, artifacts

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-31

Schema notes (see DESIGN_EDITS.md for the decisions behind these):

- `posts` gains the content columns it never had. Only hydration writes them
  (decision 23) — `post_attempt` must not, or re-posting would overwrite the
  live truth we just read off Craigslist. `posts.images` holds the *live* image
  state as seen on Craigslist (their URLs, which we don't own); our own desired
  images live in `post_desired_images` and reference the `images` table.
- `post_desired_state` is one row per post, not a queue (decision 25). Editing
  twice bumps `desired_rev` instead of stacking a second job. A post needs
  reconciling exactly when `desired_rev > live_rev`.
- `base_hash` is decision 26's optimistic concurrency token: the content hash
  the operator was looking at when they edited. The desktop re-hydrates at apply
  time and refuses to clobber if it no longer matches.
- `post_desired_images` deliberately mirrors `draft_images` from 0005 — same
  slot semantics, same 1..5 constraint — so image attachment works identically
  whether the target is a queued draft or a live posting. Ownership and reuse
  cooldowns stay owned by `images` (owner_account / used_at), not duplicated
  here.
- `post_edit_attempts` is the append-only full-flow log, keyed by event_id like
  every other event table. `steps` holds the breadcrumb trail.
- `artifacts` finally implements DESIGN.md decision 17: screenshots and HTML
  dumps reach the VPS instead of dying in logs/failures/ on the Windows box.
  Stored as BYTEA with a 30-day TTL — small, ephemeral, and deliberately not on
  the image volume, which holds things we intend to keep.
- Edit guardrails land on `guardrail_settings` beside the posting ones so there
  is a single settings row to read. `edits_enabled` defaults FALSE: the CL edit
  form has never been exercised by this codebase, so nothing may touch a live
  post until the spike is done and it is switched on deliberately.
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- posts
    op.execute(
        """
        ALTER TABLE posts
            ADD COLUMN body                 TEXT,
            ADD COLUMN county               TEXT,
            ADD COLUMN city                 TEXT,
            ADD COLUMN service_offered      TEXT,
            ADD COLUMN postal_code          TEXT,
            ADD COLUMN license_number       TEXT,
            ADD COLUMN phone_number         TEXT,
            ADD COLUMN images               JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN content_hash         TEXT,
            ADD COLUMN live_status          TEXT,
            ADD COLUMN editable             BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN hydrated_at          TIMESTAMPTZ,
            ADD COLUMN hydrate_requested_at TIMESTAMPTZ,
            ADD COLUMN hydrate_error        TEXT;
        CREATE INDEX idx_posts_hydrate_pending ON posts(hydrate_requested_at)
            WHERE hydrate_requested_at IS NOT NULL;
        """
    )

    # ------------------------------------------------- post_desired_state
    op.execute(
        """
        CREATE TABLE post_desired_state (
            post_id             TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
            account             TEXT NOT NULL,

            title               TEXT NOT NULL,
            body                TEXT NOT NULL,
            county              TEXT NOT NULL DEFAULT '',
            city                TEXT NOT NULL DEFAULT '',
            service_offered     TEXT NOT NULL DEFAULT '',
            postal_code         TEXT NOT NULL DEFAULT '',
            license_number      TEXT NOT NULL DEFAULT '',
            phone_number        TEXT NOT NULL DEFAULT '',

            -- FALSE means "leave the images alone" — the reconcile only touches
            -- images once you have explicitly taken control of them, so a text
            -- edit can never disturb a working image set.
            image_set_managed   BOOLEAN NOT NULL DEFAULT FALSE,

            -- desired_rev > live_rev  =>  needs reconcile (decision 25)
            desired_rev         INTEGER NOT NULL DEFAULT 1,
            live_rev            INTEGER NOT NULL DEFAULT 0,

            -- Optimistic concurrency token (decision 26)
            base_hash           TEXT,

            status              TEXT NOT NULL DEFAULT 'pending',
            failed_step         TEXT,
            failed_message      TEXT,
            attempts            INTEGER NOT NULL DEFAULT 0,
            last_attempt_at     TIMESTAMPTZ,

            claimed_at          TIMESTAMPTZ,
            claimed_by_machine  TEXT,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT pds_status_check CHECK (
                status IN (
                    'pending',        -- desired_rev > live_rev, waiting for the desktop
                    'applying',       -- claimed by a machine right now
                    'applied',        -- live_rev == desired_rev
                    'parked_stale',   -- decision 26: live content moved under us
                    'parked_gone',    -- post expired/deleted on CL
                    'degraded_live',  -- decision 32: mutated and could not recover
                    'failed'
                )
            )
        );
        CREATE INDEX idx_pds_account ON post_desired_state(account);
        CREATE INDEX idx_pds_status ON post_desired_state(status);
        -- The reconcile worker's hot query.
        CREATE INDEX idx_pds_pending ON post_desired_state(account, updated_at)
            WHERE status = 'pending';
        """
    )

    # ------------------------------------------------ post_desired_images
    # Mirrors draft_images (0005) so attachment behaves identically for a live
    # posting and a queued draft.
    op.execute(
        """
        CREATE TABLE post_desired_images (
            post_id   TEXT NOT NULL REFERENCES post_desired_state(post_id) ON DELETE CASCADE,
            image_id  BIGINT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            slot      INTEGER NOT NULL,
            PRIMARY KEY (post_id, slot),
            UNIQUE (post_id, image_id),
            CONSTRAINT post_desired_images_slot_check CHECK (slot BETWEEN 1 AND 5)
        );
        CREATE INDEX idx_pdi_image ON post_desired_images(image_id);
        """
    )

    # ------------------------------------------------- post_edit_attempts
    op.execute(
        """
        CREATE TABLE post_edit_attempts (
            event_id             TEXT PRIMARY KEY,
            ts                   TIMESTAMPTZ NOT NULL,
            machine              TEXT NOT NULL,
            account              TEXT NOT NULL,
            post_id              TEXT NOT NULL,

            outcome              TEXT NOT NULL,
            duration_seconds     DOUBLE PRECISION,

            desired_rev          INTEGER,
            applied_rev          INTEGER,

            steps                JSONB NOT NULL DEFAULT '[]'::jsonb,

            failed_step          TEXT,
            error_type           TEXT,
            error_message        TEXT,

            images_live_count    INTEGER,
            images_desired_count INTEGER,

            artifact_ids         JSONB NOT NULL DEFAULT '[]'::jsonb,
            received_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_pea_post_ts ON post_edit_attempts(post_id, ts DESC);
        CREATE INDEX idx_pea_ts ON post_edit_attempts(ts DESC);
        CREATE INDEX idx_pea_outcome ON post_edit_attempts(outcome, ts DESC);
        """
    )

    # ------------------------------------------------------------ artifacts
    op.execute(
        """
        CREATE TABLE artifacts (
            id            TEXT PRIMARY KEY,
            machine       TEXT NOT NULL,
            kind          TEXT NOT NULL,
            content_type  TEXT NOT NULL,
            size_bytes    INTEGER NOT NULL,
            post_id       TEXT,
            account       TEXT,
            flow          TEXT,
            label         TEXT,
            data          BYTEA NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at    TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days',
            CONSTRAINT artifacts_kind_check CHECK (kind IN ('screenshot', 'html'))
        );
        CREATE INDEX idx_artifacts_post ON artifacts(post_id) WHERE post_id IS NOT NULL;
        CREATE INDEX idx_artifacts_expiry ON artifacts(expires_at);
        """
    )

    # --------------------------------------------- edit guardrails (dec. 30)
    op.execute(
        """
        ALTER TABLE guardrail_settings
            ADD COLUMN edits_enabled                     BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN min_hours_between_edits_same_post INTEGER NOT NULL DEFAULT 48,
            ADD COLUMN max_edits_per_account_per_day     INTEGER NOT NULL DEFAULT 3,
            ADD COLUMN max_edits_per_post_lifetime       INTEGER NOT NULL DEFAULT 5,
            ADD COLUMN edit_window_start_hour            INTEGER NOT NULL DEFAULT 8,
            ADD COLUMN edit_window_end_hour              INTEGER NOT NULL DEFAULT 19,
            ADD COLUMN edits_paused_reason               TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE guardrail_settings
            DROP COLUMN IF EXISTS edits_paused_reason,
            DROP COLUMN IF EXISTS edit_window_end_hour,
            DROP COLUMN IF EXISTS edit_window_start_hour,
            DROP COLUMN IF EXISTS max_edits_per_post_lifetime,
            DROP COLUMN IF EXISTS max_edits_per_account_per_day,
            DROP COLUMN IF EXISTS min_hours_between_edits_same_post,
            DROP COLUMN IF EXISTS edits_enabled;
        """
    )
    op.execute("DROP TABLE IF EXISTS artifacts")
    op.execute("DROP TABLE IF EXISTS post_edit_attempts")
    op.execute("DROP TABLE IF EXISTS post_desired_images")
    op.execute("DROP TABLE IF EXISTS post_desired_state")
    op.execute(
        """
        DROP INDEX IF EXISTS idx_posts_hydrate_pending;
        ALTER TABLE posts
            DROP COLUMN IF EXISTS hydrate_error,
            DROP COLUMN IF EXISTS hydrate_requested_at,
            DROP COLUMN IF EXISTS hydrated_at,
            DROP COLUMN IF EXISTS editable,
            DROP COLUMN IF EXISTS live_status,
            DROP COLUMN IF EXISTS content_hash,
            DROP COLUMN IF EXISTS images,
            DROP COLUMN IF EXISTS phone_number,
            DROP COLUMN IF EXISTS license_number,
            DROP COLUMN IF EXISTS postal_code,
            DROP COLUMN IF EXISTS service_offered,
            DROP COLUMN IF EXISTS city,
            DROP COLUMN IF EXISTS county,
            DROP COLUMN IF EXISTS body;
        """
    )
