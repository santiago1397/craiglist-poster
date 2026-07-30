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
from ..db import conn, tx
from ..security import issue_machine_token, revoke_machine_token
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


class TokenCreate(BaseModel):
    machine: str
    label: str = ""


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
        return queue_svc.update_guardrails(c, patch)


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
