"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-05

Schema notes:

- `posts` is the dimension table (one row per Craigslist posting). Multiple
  event types can bring a post into existence — post_attempt (with outcome
  'posted') creates it, snapshot_taken can also create it if we somehow see a
  snapshot for an unknown post (backfill / recovery).
- `snapshots` is one row per (post_id, snapshot_date) with the cumulative
  Craigslist counters. Matches the Windows-side sqlite table shape 1:1.
- `post_attempts`, `ghost_checks`, `photo_inventory_snapshots`,
  `account_states`, `scheduler_configs` are event log tables keyed by
  event_id — the reporter's idempotency key. ON CONFLICT DO NOTHING makes
  the ingest naturally idempotent.
- Frequent "latest per X" queries are served by `DISTINCT ON` with the
  supporting index below.
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE posts (
            post_id       TEXT PRIMARY KEY,
            account       TEXT NOT NULL,
            title         TEXT,
            url           TEXT,
            posted_ts     TIMESTAMPTZ,
            source        TEXT NOT NULL DEFAULT 'event_ingest',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_posts_account ON posts(account);
        CREATE INDEX idx_posts_posted_ts ON posts(posted_ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE snapshots (
            post_id           TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
            snapshot_date     DATE NOT NULL,
            snapshot_ts_utc   TIMESTAMPTZ NOT NULL,
            status            TEXT,
            impressions       INTEGER,
            views             INTEGER,
            shares            INTEGER,
            favorites         INTEGER,
            area              TEXT,
            category          TEXT,
            expires_in_days   INTEGER,
            autorepost        TEXT,
            freshness_note    TEXT,
            PRIMARY KEY (post_id, snapshot_date)
        );
        CREATE INDEX idx_snapshots_date ON snapshots(snapshot_date DESC);
        CREATE INDEX idx_snapshots_post_date ON snapshots(post_id, snapshot_date DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE post_attempts (
            event_id           TEXT PRIMARY KEY,
            ts                 TIMESTAMPTZ NOT NULL,
            machine            TEXT NOT NULL,
            account            TEXT NOT NULL,
            outcome            TEXT NOT NULL,
            duration_seconds   REAL,
            post_id            TEXT,     -- soft link; not FK because post may not exist yet on failures
            post_url           TEXT,
            ad_title           TEXT,
            photos_attached    JSONB NOT NULL DEFAULT '[]'::jsonb,
            cover_photo        TEXT,
            error_type         TEXT,
            error_message      TEXT,
            received_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_post_attempts_account_ts ON post_attempts(account, ts DESC);
        CREATE INDEX idx_post_attempts_outcome ON post_attempts(outcome);
        CREATE INDEX idx_post_attempts_ts ON post_attempts(ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE ghost_checks (
            event_id     TEXT PRIMARY KEY,
            ts           TIMESTAMPTZ NOT NULL,
            post_id      TEXT NOT NULL,
            account      TEXT NOT NULL,
            ghosted      BOOLEAN NOT NULL,
            received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_ghost_checks_post_ts ON ghost_checks(post_id, ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE photo_inventory_snapshots (
            event_id             TEXT PRIMARY KEY,
            ts                   TIMESTAMPTZ NOT NULL,
            account              TEXT NOT NULL,
            photos_total         INTEGER NOT NULL,
            photos_never_used    INTEGER NOT NULL,
            photos_eligible      INTEGER NOT NULL,
            covers_total         INTEGER NOT NULL,
            covers_never_used    INTEGER NOT NULL,
            covers_eligible      INTEGER NOT NULL,
            received_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_photo_inventory_account_ts
            ON photo_inventory_snapshots(account, ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE account_states (
            event_id                       TEXT PRIMARY KEY,
            ts                             TIMESTAMPTZ NOT NULL,
            machine                        TEXT NOT NULL,
            account                        TEXT NOT NULL,
            eligible_now                   BOOLEAN NOT NULL,
            next_eligible_at               TIMESTAMPTZ,
            block_reasons                  JSONB NOT NULL DEFAULT '[]'::jsonb,
            posts_last_24h_total           INTEGER NOT NULL,
            posts_last_7d_this_account     INTEGER NOT NULL,
            last_post_at                   TIMESTAMPTZ,
            last_post_url                  TEXT,
            stats_sync_health              JSONB,
            received_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_account_states_account_ts
            ON account_states(account, ts DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE scheduler_configs (
            event_id                                  TEXT PRIMARY KEY,
            ts                                        TIMESTAMPTZ NOT NULL,
            machine                                   TEXT NOT NULL,
            posting_cadence                           TEXT,
            stats_sync_cadence                        TEXT,
            min_hours_between_posts_same_account      INTEGER NOT NULL,
            max_posts_per_day_total                   INTEGER NOT NULL,
            max_posts_per_account_per_week            INTEGER NOT NULL,
            post_window_start_hour                    INTEGER NOT NULL,
            post_window_end_hour                      INTEGER NOT NULL,
            post_weekdays_only                        BOOLEAN NOT NULL,
            received_at                               TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_scheduler_configs_machine_ts
            ON scheduler_configs(machine, ts DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scheduler_configs")
    op.execute("DROP TABLE IF EXISTS account_states")
    op.execute("DROP TABLE IF EXISTS photo_inventory_snapshots")
    op.execute("DROP TABLE IF EXISTS ghost_checks")
    op.execute("DROP TABLE IF EXISTS post_attempts")
    op.execute("DROP TABLE IF EXISTS snapshots")
    op.execute("DROP TABLE IF EXISTS posts")
