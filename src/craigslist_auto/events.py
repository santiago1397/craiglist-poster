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
    "skipped_no_drafts",
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

    # Which queued draft this attempt was for. Ingest uses it to move the draft
    # to posted / back to the queue / into needs_attention, so outcome reporting
    # rides the same durable outbox as everything else.
    draft_id: int | None = None

    # Populated when outcome == "posted"
    post_id: str | None = None
    post_url: str | None = None
    ad_title: str | None = None
    photos_attached: list[str] = Field(default_factory=list)  # filenames only
    cover_photo: str | None = None

    # Populated on failures
    error_type: str | None = None
    error_message: str | None = None
    # The poster's internal step name (e.g. "form_body", "billing"). Decides
    # whether a failed draft can be auto-requeued or must be parked, because
    # anything at or after "photo_upload" has already pushed images to CL.
    failed_step: str | None = None

    # Non-fatal degradations seen during an otherwise successful run: a photo
    # that never rendered a thumbnail, a county we had to guess, a post URL we
    # could not resolve to a durable /d/ link.
    #
    # These deliberately do NOT change `outcome`. The ad published, so it must
    # still count against the cooldowns — flipping the outcome would corrupt
    # the history the server's eligibility maths depends on. They exist so a
    # green "posted" badge can never hide a half-broken ad.
    warnings: list[str] = Field(default_factory=list)
    # Thumbnails Craigslist actually rendered. `photos_attached` is only what we
    # intended to upload; a gap between the two is the signal that images were
    # silently dropped.
    photos_confirmed: int | None = None

    # Screenshots / HTML dumps spooled for upload (DESIGN.md decision 17).
    # Mirrors PostEditAttempt: an error string without the page behind it is not
    # debuggable, and posting is the flow that runs unattended three times a day.
    artifact_ids: list[str] = Field(default_factory=list)


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
# 7. flow_error — any flow that raises, anywhere, reports it here.
#
# post_attempt already carries posting failures; this covers everything else
# (stats sync, ghost check, queue sync, image download, cover generation) so a
# silent failure in a background job is visible from the dashboard instead of
# only in run.log on a machine nobody is looking at.
# ---------------------------------------------------------------------------

class FlowError(_EventBase):
    event_type: Literal["flow_error"] = "flow_error"
    machine: str
    flow: str                      # "stats_sync" | "ghost_check" | "queue_sync" | ...
    step: str | None = None
    account: str | None = None
    error_type: str
    error_message: str | None = None
    context: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 8. post_content — the result of hydrating a live posting's edit form
#    (DESIGN_EDITS decision 23).
#
# The VPS has never stored what a post says: `posts` carries only
# post_id/account/title/url/posted_ts. Editing requires the real current text,
# and for the 24 postings that predate the queue there is no draft to fall back
# on. So the desktop opens the actual CL edit form, reads it, and ships it here.
#
# `content_hash` is what decision 26's optimistic concurrency compares against:
# it is recomputed at apply time and a mismatch parks the edit instead of
# clobbering a change you never saw.
# ---------------------------------------------------------------------------

class EditStep(BaseModel):
    """One breadcrumb in a hydrate or reconcile walk.

    Carries the selector census, which is the first thing worth reading when
    an edit or a hydration fails against a form nobody has observed.
    """
    model_config = ConfigDict(extra="forbid")
    name: str
    ok: bool
    duration_seconds: float | None = None
    note: str | None = None


class PostImage(BaseModel):
    """One image as it appears on the live posting, in slot order."""
    model_config = ConfigDict(extra="forbid")
    slot: int
    url: str | None = None
    # sha256 of the bytes when we sourced the image ourselves. Images already on
    # CL when we hydrated have no local bytes, so this is None for them.
    sha256: str | None = None


class PostContent(_EventBase):
    event_type: Literal["post_content"] = "post_content"
    machine: str
    account: str
    post_id: str

    # None means hydration failed; error_* carries why. A failed hydration is
    # still reported so the dashboard can stop showing a spinner forever.
    ok: bool = True
    error_type: str | None = None
    error_message: str | None = None

    title: str | None = None
    body: str | None = None
    county: str | None = None
    city: str | None = None
    service_offered: str | None = None
    postal_code: str | None = None
    license_number: str | None = None
    phone_number: str | None = None

    images: list[PostImage] = Field(default_factory=list)

    # Stable hash over the normalised editable fields — see editor.content_hash.
    content_hash: str | None = None
    # Whether CL still shows this posting as editable at all.
    editable: bool = True

    # The walk that produced this read, including the selector census. Hydration
    # is the first thing anyone runs against the edit form, so its evidence has
    # to reach the dashboard — it used to stay in logs/run.log on the posting
    # machine, which is exactly the machine you are not sitting at.
    steps: list[EditStep] = Field(default_factory=list)
    live_status: str | None = None       # "active" | "expired" | "deleted" | ...

    artifact_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 9. post_edit_attempt — every reconcile of a live posting, success or failure
#    (DESIGN_EDITS decision 35).
#
# Mirrors PostAttempt's shape deliberately: one structured row per attempt with
# a `failed_step` that drives routing. Adds `steps` because an edit acts on
# something already live, so "how far did it get" is the difference between a
# harmless retry and a post sitting there with no images (decision 32).
# ---------------------------------------------------------------------------

EditOutcome = Literal[
    "applied",
    "no_change",              # desired already matched live; nothing typed
    "dry_run",
    "skipped_not_eligible",
    "skipped_lease_held",     # another browser flow had the profile
    "failed_stale",           # decision 26 — live content moved under us
    "failed_gone",            # post expired/deleted on CL
    "failed_login",
    "failed_form",
    "failed_images",
    "failed_other",
    "degraded_live",          # decision 32 — mutated and could not recover
]


class PostEditAttempt(_EventBase):
    event_type: Literal["post_edit_attempt"] = "post_edit_attempt"
    machine: str
    account: str
    post_id: str

    outcome: EditOutcome
    duration_seconds: float | None = None

    # Which revision of the desired state this attempt was reconciling. Ingest
    # only advances live_rev when the attempt actually applied, so a stale or
    # failed attempt never marks the post as up to date.
    desired_rev: int | None = None
    applied_rev: int | None = None

    # Full-flow breadcrumb: every step entered, whether it completed, how long.
    steps: list[EditStep] = Field(default_factory=list)

    failed_step: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    # Set when outcome == "degraded_live": what state the live post was left in.
    images_live_count: int | None = None
    images_desired_count: int | None = None

    # Screenshots / HTML dumps spooled for upload (decision 35).
    artifact_ids: list[str] = Field(default_factory=list)


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
        FlowError,
        PostContent,
        PostEditAttempt,
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
    "FlowError",
    "PostContent",
    "PostEditAttempt",
    "PostImage",
    "EditStep",
    "StatsSyncHealth",
    "PostOutcome",
    "EditOutcome",
]
