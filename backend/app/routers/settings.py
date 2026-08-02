"""Guardrail settings and machine-token administration.

The server owns the throttles (decision 14), but the desktop clamps whatever it
receives to ceilings compiled into `craigslist_auto.config`. Bounds here are a
first line of defence, not the only one — a value that slips through still gets
clamped desktop-side and logged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..config import get_settings
from ..db import conn, tx
from ..security import (
    issue_api_key,
    issue_machine_token,
    revoke_api_key,
    revoke_machine_token,
)
from ..services import queue as queue_svc

router = APIRouter(dependencies=[Depends(require_admin)])


class GuardrailUpdate(BaseModel):
    min_hours_between_posts_same_account: int | None = Field(default=None, ge=1, le=168)
    max_posts_per_day_total: int | None = Field(default=None, ge=1, le=50)
    max_posts_per_account_per_week: int | None = Field(default=None, ge=1, le=100)
    post_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    post_window_end_hour: int | None = Field(default=None, ge=1, le=24)
    post_weekdays_only: bool | None = None
    queue_depth_floor: int | None = Field(default=None, ge=0, le=500)
    queue_depth_target: int | None = Field(default=None, ge=1, le=1000)

    # Editing (DESIGN_EDITS.md decision 30). Bounds here are a first line of
    # defence; the desktop clamps again to ceilings compiled into config.py.
    edits_enabled: bool | None = None
    min_hours_between_edits_same_post: int | None = Field(default=None, ge=1, le=720)
    max_edits_per_account_per_day: int | None = Field(default=None, ge=0, le=50)
    max_edits_per_post_lifetime: int | None = Field(default=None, ge=1, le=200)
    edit_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    edit_window_end_hour: int | None = Field(default=None, ge=1, le=24)
    edits_paused_reason: str | None = Field(default=None, max_length=200)


class TokenCreate(BaseModel):
    machine: str
    label: str = ""


class ApiKeyCreate(BaseModel):
    label: str = Field(default="", max_length=100)
    # 'read' opens the whole agent surface. 'post' additionally allows
    # publishing a draft a human has already marked reviewed.
    scope: str = Field(default="read", pattern="^(read|post)$")


class PostingSwitch(BaseModel):
    enabled: bool
    # Free text shown wherever the pause is surfaced, so a week later you know
    # why it was stopped.
    reason: str | None = Field(default=None, max_length=200)


@router.get("/posting")
def get_posting_state() -> dict:
    with conn() as c:
        g = queue_svc.get_guardrails(c)
    return {
        "enabled": g.get("posting_enabled", True),
        "paused_at": g.get("paused_at"),
        "paused_reason": g.get("paused_reason"),
    }


@router.put("/posting")
def set_posting_state(body: PostingSwitch) -> dict:
    """Stop or resume posting across every machine.

    The queue is left alone — drafts keep their order and resume where they
    left off. Takes effect on the next claim; a post already in flight runs to
    completion.
    """
    with tx() as c:
        g = queue_svc.set_posting_enabled(c, enabled=body.enabled, reason=body.reason)
    return {
        "enabled": g["posting_enabled"],
        "paused_at": g.get("paused_at"),
        "paused_reason": g.get("paused_reason"),
    }


@router.get("/guardrails")
def get_guardrails() -> dict:
    with conn() as c:
        return queue_svc.get_guardrails(c)


@router.put("/guardrails")
def put_guardrails(body: GuardrailUpdate) -> dict:
    patch = body.model_dump(exclude_unset=True)
    with tx() as c:
        current = queue_svc.get_guardrails(c)
        merged = {**current, **{k: v for k, v in patch.items() if v is not None}}
        if merged["post_window_start_hour"] >= merged["post_window_end_hour"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="post_window_start_hour must be before post_window_end_hour",
            )
        if merged["queue_depth_floor"] > merged["queue_depth_target"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="queue_depth_floor cannot exceed queue_depth_target",
            )
        if merged["edit_window_start_hour"] >= merged["edit_window_end_hour"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="edit_window_start_hour must be before edit_window_end_hour",
            )
        return queue_svc.update_guardrails(c, patch)


class GenerationUpdate(BaseModel):
    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=100)
    api_base: str | None = Field(default=None, max_length=300)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    system_prompt: str | None = None
    user_template: str | None = None
    tail_template: str | None = None
    photos_min: int | None = Field(default=None, ge=0, le=24)
    photos_max: int | None = Field(default=None, ge=0, le=24)
    imageless_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # Background photo-stack refill. Ships off; turn it on once the image
    # prompts are settled, since this is what spends money on them unattended.
    image_topup_enabled: bool | None = None
    image_stack_floor: int | None = Field(default=None, ge=0, le=100_000)
    image_stack_target: int | None = Field(default=None, ge=0, le=100_000)
    image_topup_batch: int | None = Field(default=None, ge=1, le=100)


@router.get("/generation")
def get_generation() -> dict:
    """Prompts, model and run stats. `seed_ads` count tells you whether the
    workbook fallback has anything to fall back to."""
    from ..services import generator

    with conn() as c:
        g = generator.get_generation_settings(c)
        g["api_key_configured"] = bool(get_settings().minimax_api_key)
        g["seed_ads"] = c.execute(
            "SELECT COUNT(*) AS n FROM seed_ads WHERE active"
        ).fetchone()["n"]
    return g


@router.put("/generation")
def put_generation(body: GenerationUpdate) -> dict:
    from ..services import generator

    patch = body.model_dump(exclude_unset=True)
    with tx() as c:
        # Check against the merged result, not the patch: sending only
        # photos_max could otherwise invert the range against the stored min.
        merged = {**generator.get_generation_settings(c),
                  **{k: v for k, v in patch.items() if v is not None}}
        if merged["photos_min"] > merged["photos_max"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"photos_min ({merged['photos_min']}) cannot exceed "
                    f"photos_max ({merged['photos_max']})"
                ),
            )
        if merged["image_stack_floor"] > merged["image_stack_target"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"image_stack_floor ({merged['image_stack_floor']}) cannot "
                    f"exceed image_stack_target ({merged['image_stack_target']})"
                ),
            )
        return generator.update_generation_settings(c, patch)


@router.get("/machine-tokens")
def list_machine_tokens() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT id, machine, label, created_at, last_seen_at, revoked_at "
            "FROM machine_tokens ORDER BY created_at DESC"
        ).fetchall()
    return {"tokens": [dict(r) for r in rows]}


@router.post("/machine-tokens", status_code=status.HTTP_201_CREATED)
def create_machine_token(body: TokenCreate) -> dict:
    """The plaintext token is shown once and never stored. Copy it now."""
    token = issue_machine_token(body.machine, body.label)
    return {"machine": body.machine, "token": token}


@router.delete("/machine-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_machine_token(token_id: int) -> None:
    if not revoke_machine_token(token_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Token not found or already revoked"
        )


# ---------------------------------------------------------------------------
# Agent API keys
#
# Separate table from machine tokens on purpose — see migration 0018. These
# open `/agent/*` and nothing else; they can never claim a draft or report an
# event.
# ---------------------------------------------------------------------------

@router.get("/api-keys")
def list_api_keys() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT id, label, scope, created_at, last_seen_at, revoked_at "
            "FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return {"keys": [dict(r) for r in rows]}


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(body: ApiKeyCreate) -> dict:
    """The plaintext key is shown once and never stored. Copy it now."""
    key = issue_api_key(body.label, body.scope)
    return {"label": body.label, "scope": body.scope, "key": key}


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(key_id: int) -> None:
    if not revoke_api_key(key_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Key not found or already revoked",
        )
