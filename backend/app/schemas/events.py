"""
DO NOT EDIT DIRECTLY.

This file is a copy of src/craigslist_auto/events.py. Both sides need it:
  - The Windows script imports from craigslist_auto.events
  - The backend imports from app.schemas.events

Sync mechanism:
  - Docker builds: the Dockerfile COPYs src/craigslist_auto/events.py over this
    file, so production always matches the script.
  - Local dev: run `python scripts/sync_schemas.py` at the repo root after any
    change to the source of truth.

Source of truth: src/craigslist_auto/events.py
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _uuid_str() -> str:
    return str(uuid4())


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=_uuid_str)
    ts: datetime


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
    post_id: str | None = None
    post_url: str | None = None
    ad_title: str | None = None
    photos_attached: list[str] = Field(default_factory=list)
    cover_photo: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class SnapshotTaken(_EventBase):
    event_type: Literal["snapshot_taken"] = "snapshot_taken"
    snapshot_date: str
    post_id: str
    account: str
    title: str | None = None
    url: str | None = None
    posted_ts: datetime | None = None
    status: str | None = None
    impressions: int | None = None
    views: int | None = None
    shares: int | None = None
    favorites: int | None = None
    area: str | None = None
    category: str | None = None
    expires_in_days: int | None = None
    autorepost: str | None = None
    freshness_note: str | None = None


class PhotoInventory(_EventBase):
    event_type: Literal["photo_inventory"] = "photo_inventory"
    account: str
    photos_total: int
    photos_never_used: int
    photos_eligible: int
    covers_total: int
    covers_never_used: int
    covers_eligible: int


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
    next_eligible_at: datetime | None = None
    block_reasons: list[str] = Field(default_factory=list)
    posts_last_24h_total: int
    posts_last_7d_this_account: int
    last_post_at: datetime | None = None
    last_post_url: str | None = None
    stats_sync_health: StatsSyncHealth | None = None


class GhostCheck(_EventBase):
    event_type: Literal["ghost_check"] = "ghost_check"
    post_id: str
    account: str
    ghosted: bool


class SchedulerConfig(_EventBase):
    event_type: Literal["scheduler_config"] = "scheduler_config"
    machine: str
    posting_cadence: str | None = None
    stats_sync_cadence: str | None = None
    min_hours_between_posts_same_account: int
    max_posts_per_day_total: int
    max_posts_per_account_per_week: int
    post_window_start_hour: int
    post_window_end_hour: int
    post_weekdays_only: bool


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
    model_config = ConfigDict(extra="forbid")
    event: AnyEvent


class EventBatch(BaseModel):
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
