"""Diagnostics — the read side of every failure the system records.

`flow_errors` had been written to since migration 0002 and read by nothing. The
desktop dutifully reported queue outages, clamped guardrails and crashed
background jobs into a table with no endpoint and no UI, which made the durable
outbox an elaborate way of writing to /dev/null.

Cookie-authenticated: this is an operator surface, not a machine one. The
desktop still reports through /events and nothing here is writable by a machine
token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import conn, tx
from ..services import diagnostics as diag_svc

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("")
def list_problems(
    hours: int = Query(default=diag_svc.DEFAULT_WINDOW_HOURS, ge=1, le=720),
    include_acknowledged: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    """Everything wrong right now, most severe first.

    Merges background flow errors, failed posting runs, drafts stuck in a claim,
    and machines that have gone silent — each with a plain-English explanation
    and where to go to fix it.
    """
    with conn() as c:
        return diag_svc.problems(
            c, hours=hours, include_acknowledged=include_acknowledged, limit=limit
        )


@router.get("/summary")
def summary() -> dict:
    """Counts only. Cheap enough for the header pill to poll on a timer."""
    with conn() as c:
        return diag_svc.summary(c)


class AckBody(BaseModel):
    event_ids: list[str] = Field(min_length=1, max_length=500)


@router.post("/acknowledge")
def acknowledge(body: AckBody) -> dict:
    """Mark flow errors as seen so they leave the open list.

    Acknowledging is not fixing. Derived problems (stuck claims, silent
    machines) ignore this by design — they clear when the underlying condition
    clears, and letting you dismiss them would hide something still true.
    """
    with tx() as c:
        return {"acknowledged": diag_svc.acknowledge(c, body.event_ids)}


@router.get("/posts/{post_id}/attempts")
def post_attempts(post_id: str, limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """Every posting run that produced this post — the counterpart to
    /edits/{post_id}/attempts, which posting never had."""
    with conn() as c:
        return {"attempts": diag_svc.attempts_for_post(c, post_id, limit=limit)}


@router.get("/drafts/{draft_id}/attempts")
def draft_attempts(draft_id: int, limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """Every run against one draft, including the ones that requeued it."""
    with conn() as c:
        return {"attempts": diag_svc.attempts_for_draft(c, draft_id, limit=limit)}
