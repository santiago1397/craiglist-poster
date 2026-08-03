"""Admin-facing draft management — the Review module's backend.

Cookie-authenticated. Everything here is an operator action: edit, reorder,
mark reviewed, delete, retry a parked draft.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import conn, tx
from ..services import drafts as drafts_svc
from ..services import queue as queue_svc

router = APIRouter(dependencies=[Depends(require_admin)])


class DraftCreate(BaseModel):
    account: str
    title: str
    body: str
    body_head: str | None = None
    county: str = ""
    city: str = ""
    service_offered: str = ""
    postal_code: str = ""
    geographic_area: str | None = None
    license_number: str = ""
    phone_number: str = ""
    not_before: datetime | None = None
    expires_at: datetime | None = None
    source: str = "manual"


class DraftUpdate(BaseModel):
    account: str | None = None
    title: str | None = None
    body: str | None = None
    body_head: str | None = None
    county: str | None = None
    city: str | None = None
    service_offered: str | None = None
    postal_code: str | None = None
    geographic_area: str | None = None
    license_number: str | None = None
    phone_number: str | None = None
    not_before: datetime | None = None
    expires_at: datetime | None = None
    reviewed: bool | None = None
    status: str | None = None


class ReorderBody(BaseModel):
    # Draft to sit immediately after. None moves it to the head of its account.
    after_id: int | None = Field(default=None)


@router.get("")
def list_drafts(
    status_filter: str | None = Query(default=None, alias="status"),
    account: str | None = Query(default=None),
    reviewed: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with conn() as c:
        return drafts_svc.list_drafts(
            c,
            status=status_filter,
            account=account,
            reviewed=reviewed,
            limit=limit,
            offset=offset,
        )


@router.get("/health")
def queue_health(accounts: str = Query(description="comma-separated")) -> dict:
    """Queue depth and per-account block reasons — the Review module's header."""
    names = [a.strip() for a in accounts.split(",") if a.strip()]
    with conn() as c:
        report = queue_svc.evaluate_eligibility(c, names)
        unreviewed = c.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status = 'queued' AND NOT reviewed"
        ).fetchone()["n"]
        attention = c.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status = 'needs_attention'"
        ).fetchone()["n"]
        # Claims held longer than a posting run can take. The reaper parks these
        # at the next claim, but that may be hours away — and a draft sitting in
        # 'claimed' counts toward no depth, so the account reads as "queue
        # empty" without anything explaining why.
        stuck = [
            d for d in queue_svc.stuck_claims(c)
            if (d["held_minutes"] or 0) >= queue_svc.STALE_CLAIM_MINUTES
        ]
    report["unreviewed"] = unreviewed
    report["needs_attention"] = attention
    report["stuck_claims"] = len(stuck)
    return report


class GenerateBody(BaseModel):
    # Fill to target even if the queue is above the floor.
    force: bool = False
    # Safety cap so a mistyped target cannot mint hundreds of drafts.
    limit: int = Field(default=20, ge=1, le=100)


@router.get("/schedule")
def schedule(
    accounts: str = Query(description="comma-separated"),
    horizon_days: int = Query(default=21, ge=1, le=60),
) -> dict:
    """Forecast when each queued draft will publish.

    Replays the claim decision against the real scheduled fire times and every
    guardrail. Approximate by nature — pausing, a failed post or an edit shifts
    it — so it answers "roughly when", not "definitely then".
    """
    names = [a.strip() for a in accounts.split(",") if a.strip()]
    with conn() as c:
        return {"schedule": queue_svc.project_schedule(
            c, accounts=names, horizon_days=horizon_days
        )}


@router.post("/generate")
def generate(body: GenerateBody) -> dict:
    """Run the top-up now instead of waiting for the background loop.

    Never fails because the model is down — unusable output falls back to the
    workbook copy in seed_ads, and the response says which was used.
    """
    from ..services import generator

    with tx() as c:
        return generator.topup(c, force=body.force, limit=body.limit)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_draft(body: DraftCreate) -> dict:
    with tx() as c:
        try:
            return drafts_svc.create_draft(c, body.model_dump())
        except ValueError as e:
            # An over-length body is the common one, and a 500 would hide the
            # character count the operator needs to act on.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            )


@router.get("/{draft_id}")
def get_draft(draft_id: int) -> dict:
    with conn() as c:
        draft = drafts_svc.get_draft(c, draft_id)
        if draft is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        head = draft.get("body_head") or draft.get("body") or ""
        draft["similarity"] = drafts_svc.similarity_report(
            c, head=head, exclude_draft_id=draft_id
        )
    return draft


@router.patch("/{draft_id}")
def update_draft(draft_id: int, body: DraftUpdate) -> dict:
    payload = body.model_dump(exclude_unset=True)
    with tx() as c:
        existing = drafts_svc.get_draft(c, draft_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        if existing["status"] == "claimed":
            # Claim is atomic and immediately followed by posting; editing a
            # draft mid-publish would silently diverge from what went live.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Draft is being posted right now and cannot be edited",
            )
        try:
            updated = drafts_svc.update_draft(c, draft_id, payload)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return updated


@router.post("/{draft_id}/reorder")
def reorder(draft_id: int, body: ReorderBody) -> dict:
    with tx() as c:
        try:
            moved = drafts_svc.reorder_draft(c, draft_id, after_id=body.after_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if moved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    return moved


@router.post("/{draft_id}/requeue")
def requeue(draft_id: int) -> dict:
    """Send a parked draft back to the queue after you've dealt with it."""
    with tx() as c:
        draft = drafts_svc.get_draft(c, draft_id)
        if draft is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        if draft["status"] not in ("needs_attention", "expired"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Draft is {draft['status']}, not parked",
            )
        return drafts_svc.update_draft(c, draft_id, {"status": "queued"})


@router.post("/{draft_id}/post-now", status_code=status.HTTP_202_ACCEPTED)
def post_now(draft_id: int, admin: str = Depends(require_admin)) -> dict:
    """Ask the desktop to post this draft on its next poll (~15s).

    202, not 200: nothing has published yet. The desktop still has to pick the
    request up, take the browser lease and drive the form.

    Refuses synchronously on the guardrails rather than queueing a request that
    might fire much later — same rules, same wording, same server-side
    evaluation a scheduled run gets. Nothing here can post something a 9am fire
    would not have been allowed to post.
    """
    with tx() as c:
        draft = drafts_svc.get_draft(c, draft_id)
        if draft is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found"
            )
        try:
            result = drafts_svc.request_post(c, draft_id, requested_by=admin)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        except drafts_svc.NotEligible as e:
            # 409 with the reasons intact — the dashboard shows the operator the
            # same strings `cl status` prints, rather than a generic refusal.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": f"{draft['account']} cannot post right now",
                    "reasons": e.reasons,
                },
            )
    return result


@router.delete("/{draft_id}/post-now", status_code=status.HTTP_204_NO_CONTENT)
def cancel_post_now(draft_id: int) -> None:
    """Withdraw a request the desktop has not picked up yet.

    Best-effort by nature: once the draft is `claimed` the browser is already
    driving the form and there is nothing left to cancel.
    """
    with tx() as c:
        drafts_svc.clear_post_request(c, draft_id)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_draft(draft_id: int) -> None:
    with tx() as c:
        if not drafts_svc.delete_draft(c, draft_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Draft not found, or is being posted right now",
            )
