"""Machine-facing queue endpoints.

Authenticated with a per-machine token (decision 19), not the admin cookie and
not the ingest token. These are the only routes the desktop calls besides
/events.

Outcomes are NOT reported here. The desktop emits a `post_attempt` event
carrying `draft_id` and `failed_step`, and event ingest updates the draft. That
keeps one durable path (the outbox) for everything the desktop tells the server,
so a network outage delays reporting instead of losing it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..db import conn, tx
from ..security import require_machine_token
from ..services import queue as queue_svc

router = APIRouter()


class ClaimBody(BaseModel):
    # Accounts this machine is bound to. Eligibility is decided server-side;
    # this is only the candidate set.
    accounts: list[str] = Field(min_length=1)
    # Unflushed events still sitting in the desktop's outbox. A backlog means
    # the server's post history may be stale, so claiming is refused until it
    # drains (see DESIGN.md, derived requirements).
    outbox_pending: int = 0


@router.get("")
def list_queue(
    machine: str = Depends(require_machine_token),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Prefetch window. Read-only and commits to nothing — the desktop mirrors
    these so image bytes are warm, but the draft it actually posts is decided
    at claim time (decision 4).

    Returns full draft bodies, not just headers: the desktop mirrors these so a
    claim can be executed immediately without a second round-trip, and so
    `--dry-run` can walk a real draft without consuming one."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM drafts
            WHERE status = 'queued'
              AND (not_before IS NULL OR not_before <= NOW())
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY account, position
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return {"machine": machine, "drafts": [dict(r) for r in rows]}


@router.get("/settings")
def machine_settings(machine: str = Depends(require_machine_token)) -> dict:
    """Guardrails the desktop applies locally, clamped to its compiled ceilings
    (decision 14)."""
    with conn() as c:
        return {"machine": machine, "guardrails": queue_svc.get_guardrails(c)}


@router.get("/eligibility")
def eligibility(
    machine: str = Depends(require_machine_token),
    accounts: str = Query(description="comma-separated account names"),
) -> dict:
    names = [a.strip() for a in accounts.split(",") if a.strip()]
    if not names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no accounts given"
        )
    with conn() as c:
        return queue_svc.evaluate_eligibility(c, names)


@router.post("/claim")
def claim(body: ClaimBody, machine: str = Depends(require_machine_token)) -> dict:
    """Atomically hand out the next draft, or explain why not.

    Always 200 — "nothing to post" is a normal answer, not an error. The desktop
    distinguishes on `draft is None`.
    """
    if body.outbox_pending > 0:
        return {
            "draft": None,
            "refused": "outbox_backlog",
            "detail": (
                f"{body.outbox_pending} unsent event(s) in the outbox; "
                "flush before claiming so post history is current"
            ),
        }
    with tx() as c:
        return queue_svc.claim_next(
            c, machine=machine, candidate_accounts=body.accounts
        )
