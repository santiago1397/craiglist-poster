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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

# DESIGN.md decision 17 caps artifacts at ~2MB. The desktop downscales
# screenshots before spooling; this is the server-side backstop.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024

from ..db import conn, tx
from ..security import require_machine_token
from ..services import edits as edits_svc
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
    # Pin the claim to one draft an operator pressed "Post now" on. The draft
    # must still carry a live request — naming an id is not on its own
    # permission to post it.
    draft_id: int | None = None


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
            c,
            machine=machine,
            candidate_accounts=body.accounts,
            draft_id=body.draft_id,
        )


@router.get("/post-requests")
def post_requests(
    machine: str = Depends(require_machine_token),
    accounts: str = Query(description="comma-separated account names"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Drafts an operator pressed "Post now" on (DESIGN.md decision 32).

    Polled every ~15s by the reporter daemon, which spawns `cl post --draft-id`
    for whatever comes back. Mirrors `/edits/pending`: the reaper runs on the
    poll path rather than on a timer, because the poll is the one moment we know
    a machine is alive to ask.

    Filtered to accounts bound to the asking machine, so a request for craigs1
    is never even visible to a machine that cannot post it.
    """
    names = [a.strip() for a in accounts.split(",") if a.strip()]
    if not names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no accounts given"
        )
    with tx() as c:
        expired = queue_svc.expire_post_requests(c)
        requests = queue_svc.pending_post_requests(c, accounts=names, limit=limit)
        eligibility = queue_svc.evaluate_eligibility(c, names)
    return {
        "machine": machine,
        "requests": requests,
        "expired": expired,
        "eligibility": eligibility,
    }


# ---------------------------------------------------------------------------
# Post editing (DESIGN_EDITS.md). Same machine token, same prefix — the desktop
# still calls exactly one authenticated surface besides /events.
# ---------------------------------------------------------------------------


class EditClaimBody(BaseModel):
    post_id: str


@router.get("/edits/pending")
def edits_pending(
    machine: str = Depends(require_machine_token),
    accounts: str = Query(description="comma-separated account names"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """What this machine could usefully do next (decision 29).

    Polled every ~15s by the reporter daemon. Hydration requests are returned
    even when editing is disabled — reading a form is not an edit, and gating it
    would leave the dashboard unable to show you anything to edit.
    """
    names = [a.strip() for a in accounts.split(",") if a.strip()]
    if not names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no accounts given"
        )
    with tx() as c:
        # Reclaim anything a dead machine left mid-flight before answering.
        edits_svc.release_stale_claims(c)
        out = edits_svc.pending_work(c, accounts=names, limit=limit)
    out["machine"] = machine
    return out


@router.post("/edits/claim")
def edits_claim(body: EditClaimBody, machine: str = Depends(require_machine_token)) -> dict:
    """Atomically take one post's desired state for reconciliation.

    Always 200 — `desired is None` means someone else got there first, or the
    edit stopped being pending between the poll and this call.
    """
    with tx() as c:
        desired = edits_svc.claim_reconcile(c, machine=machine, post_id=body.post_id)
    return {"desired": desired}


@router.put("/artifacts/{artifact_id}", status_code=status.HTTP_201_CREATED)
async def put_artifact(
    artifact_id: str,
    request: Request,
    machine: str = Depends(require_machine_token),
    kind: str = Query(pattern="^(screenshot|html)$"),
    content_type: str = Query(default="application/octet-stream"),
    post_id: str | None = Query(default=None),
    account: str | None = Query(default=None),
    flow: str | None = Query(default=None),
    label: str | None = Query(default=None),
) -> dict:
    """Upload a failure artifact (DESIGN.md decision 17).

    Raw body rather than multipart: the desktop is spooling one blob at a time
    and this keeps the client a single httpx.put. Idempotent by artifact_id, so
    a retry after a timeout does not store the bytes twice.
    """
    data = await request.body()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="empty artifact"
        )
    if len(data) > MAX_ARTIFACT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"artifact is {len(data)//1024}KB; limit is {MAX_ARTIFACT_BYTES//1024}KB",
        )
    with tx() as c:
        c.execute(
            """
            INSERT INTO artifacts (id, machine, kind, content_type, size_bytes,
                                   post_id, account, flow, label, data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (artifact_id, machine, kind, content_type, len(data),
             post_id, account, flow, label, data),
        )
    return {"id": artifact_id, "size_bytes": len(data)}
