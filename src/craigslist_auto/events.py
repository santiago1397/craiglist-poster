"""
Event schema shared between the Windows script and the FastAPI backend.

Every event has:
  event_id   — UUID generated at emit time; the backend uses it as an
               idempotency key so retries of the outbox never double-insert.
  ts         — UTC ISO timestamp of when the *thing* actually happened,
               not when the row was delivered.
  event_type — literal string discriminator; the backend dispatches on it.

The `Envelope` wraps a payload for transport. The reporter serialises the
envelope to JSON and POSTs it to /events (or /events/batch).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _uuid_str() -> str:
    return str(uuid4())


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=_uuid_str)
    ts: datetime  # UTC


# ---------------------------------------------------------------------------
# 1. post_attempt — every `cl post` invocation, success or failure.
# ---------------------------------------------------------------------------

PostOutcome = Literal[
    "posted",
    "skipped_no_eligible",
    "failed_login",
    "failed_form",
    "failed_other",
    "dry_run",
]


class PostAttempt(_EventBase):
    event_type: Literal["post_attempt"] = "post_attempt"
    machine: str
    account: str
    outcome: PostOutcome
    duration_seconds: float | None = None

    # Populated when outcome == "posted"
    post_id: str | None = None
    post_url: str | None = None
    ad_title: str | None = None
    photos_attached: list[str] = Field(default_factory=list)  # filenames only
    cover_photo: str | None = None

    # Populated on failures
    error_type: str | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# 2. snapshot_taken — mirrors one row of the local stats.sqlite `snapshots`
# ---------------------------------------------------------------------------

class SnapshotTaken(_EventBase):
    event_type: Literal["snapshot_taken"] = "snapshot_taken"
    snapshot_date: str  # YYYY-MM-DD in America/New_York
    post_id: str
    account: str

    # Post dimension fields (backend upserts into its posts table too)
    title: str | None = None
    url: str | None = None
    posted_ts: datetime | None = None

    # Metric fields
    status: str | None = None
    impressions: int | None = None
    views: int | None = None
    shares: int | None = None
    favorites: int | None = None

    # Dimension fields captured on the snapshot
    area: str | None = None
    category: str | None = None
    expires_in_days: int | None = None
    autorepost: str | None = None
    freshness_note: str | None = None


# ---------------------------------------------------------------------------
# 3. photo_inventory — nightly cron per account.
# ---------------------------------------------------------------------------

class PhotoInventory(_EventBase):
    event_type: Literal["photo_inventory"] = "photo_inventory"
    account: str

    photos_total: int
    photos_never_used: int
    photos_eligible: int

    covers_total: int
    covers_never_used: int
    covers_eligible: int


# ---------------------------------------------------------------------------
# 4. account_state — heartbeat from the reporter daemon (every ~5 min).
# ---------------------------------------------------------------------------

class StatsSyncHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    last_run_ts: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


class AccountState(_EventBase):
    event_type: Literal["account_state"] = "account_state"
    machine: str
    account: str

    eligible_now: bool
    next_eligible_at: datetime | None = None  # None when weekly cap keeps it out for >1w
    block_reasons: list[str] = Field(default_factory=list)

    posts_last_24h_total: int
    posts_last_7d_this_account: int

    last_post_at: datetime | None = None
    last_post_url: str | None = None

    stats_sync_health: StatsSyncHealth | None = None


# ---------------------------------------------------------------------------
# 5. ghost_check — after cl check-ghosts marks a post visible/ghosted.
# ---------------------------------------------------------------------------

class GhostCheck(_EventBase):
    event_type: Literal["ghost_check"] = "ghost_check"
    post_id: str
    account: str
    ghosted: bool


# ---------------------------------------------------------------------------
# 6. scheduler_config — sent by the daemon on startup so the dashboard knows
#    the actual Task Scheduler cadence + current guardrail constants.
# ---------------------------------------------------------------------------

class SchedulerConfig(_EventBase):
    event_type: Literal["scheduler_config"] = "scheduler_config"
    machine: str

    posting_cadence: str | None = None       # free-form; e.g. "every 3h 08-19 Mon-Fri"
    stats_sync_cadence: str | None = None

    # Snapshot of the guardrails currently compiled into the script
    min_hours_between_posts_same_account: int
    max_posts_per_day_total: int
    max_posts_per_account_per_week: int
    post_window_start_hour: int
    post_window_end_hour: int
    post_weekdays_only: bool


# ---------------------------------------------------------------------------
# Envelope + discriminated union
# ---------------------------------------------------------------------------

AnyEvent = Annotated[
    Union[
        PostAttempt,
        SnapshotTaken,
        PhotoInventory,
        AccountState,
        GhostCheck,
        SchedulerConfig,
    ],
    Field(discriminator="event_type"),
]


class EventEnvelope(BaseModel):
    """Single-event ingest payload for POST /events."""
    model_config = ConfigDict(extra="forbid")
    event: AnyEvent


class EventBatch(BaseModel):
    """Batched ingest payload for POST /events/batch."""
    model_config = ConfigDict(extra="forbid")
    events: list[AnyEvent] = Field(min_length=1, max_length=500)


__all__ = [
    "AnyEvent",
    "EventBatch",
    "EventEnvelope",
    "PostAttempt",
    "SnapshotTaken",
    "PhotoInventory",
    "AccountState",
    "GhostCheck",
    "SchedulerConfig",
    "StatsSyncHealth",
    "PostOutcome",
]
