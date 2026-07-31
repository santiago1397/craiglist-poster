"""Admin-facing post editing — the Edits module's backend.

Cookie-authenticated. Everything here is an operator action: load a post's live
content, change what it should say, discard, requeue a parked edit, or read the
attempt log.

The machine-facing half (`/queue/edits/...`) lives in `routers/queue.py` so that
every route the desktop calls with a machine token stays under one prefix.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import conn, tx
from ..services import edits as edits_svc
from ..services import images as images_svc

router = APIRouter(dependencies=[Depends(require_admin)])


class DesiredUpdate(BaseModel):
    """What an operator may change about a live posting.

    `county` and `service_offered` are absent on purpose — Craigslist's edit
    form exposes no control for either, so accepting them would stage a change
    the desktop can never apply. See `_EDITABLE` in services/edits.py.
    """

    title: str | None = None
    body: str | None = None
    city: str | None = None
    postal_code: str | None = None
    license_number: str | None = None
    phone_number: str | None = None
    image_set_managed: bool | None = None


@router.get("")
def list_posts(
    account: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with conn() as c:
        return edits_svc.list_editable_posts(
            c, account=account, status=status_filter, limit=limit, offset=offset
        )


@router.get("/health")
def health(accounts: str = Query(description="comma-separated")) -> dict:
    names = [a.strip() for a in accounts.split(",") if a.strip()]
    with conn() as c:
        return edits_svc.edit_health(c, names)


@router.get("/{post_id}")
def get_post(post_id: str) -> dict:
    with conn() as c:
        found = c.execute(
            """
            SELECT p.*, d.status AS edit_status, d.desired_rev, d.live_rev,
                   d.title AS desired_title, d.body AS desired_body,
                   d.city AS desired_city,
                   d.postal_code AS desired_postal_code,
                   d.license_number AS desired_license_number,
                   d.phone_number AS desired_phone_number,
                   d.image_set_managed,
                   d.failed_step, d.failed_message, d.attempts AS edit_attempts,
                   d.last_attempt_at, d.base_hash,
                   -- The draft this posting came from, if the queue produced it.
                   -- `body_head` is what lets the editor split the ad copy from
                   -- the byte-exact keyword tail, exactly as Review does; a post
                   -- with no originating draft simply gets one plain textarea.
                   src.body_head
            FROM posts p
            LEFT JOIN post_desired_state d ON d.post_id = p.post_id
            LEFT JOIN LATERAL (
                SELECT body_head FROM drafts
                WHERE posted_post_id = p.post_id
                ORDER BY posted_at DESC NULLS LAST LIMIT 1
            ) src ON TRUE
            WHERE p.post_id = %s
            """,
            (post_id,),
        ).fetchone()
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        out = dict(found)
        out["attempts"] = edits_svc.attempts_for_post(c, post_id)
    return out


@router.post("/{post_id}/hydrate", status_code=status.HTTP_202_ACCEPTED)
def hydrate(post_id: str) -> dict:
    """Ask the desktop to read this post's live edit form (decision 24).

    202 because nothing has happened yet — the desktop picks this up on its next
    poll, opens Craigslist, and reports back as a `post_content` event.
    """
    with tx() as c:
        try:
            return edits_svc.request_hydration(c, post_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.put("/{post_id}/desired")
def put_desired(post_id: str, body: DesiredUpdate) -> dict:
    payload = body.model_dump(exclude_unset=True)
    with tx() as c:
        existing = edits_svc.get_desired(c, post_id)
        if existing is not None and existing["status"] == "applying":
            # The desktop is on the edit form right now; changing the target
            # mid-flight would apply a mix of two revisions.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This post is being edited right now — try again in a moment",
            )
        try:
            return edits_svc.upsert_desired(c, post_id, payload)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            )


@router.delete("/{post_id}/desired", status_code=status.HTTP_204_NO_CONTENT)
def discard_desired(post_id: str) -> None:
    with tx() as c:
        if not edits_svc.discard_desired(c, post_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Nothing pending, or the post is being edited right now",
            )


@router.post("/{post_id}/requeue")
def requeue(post_id: str) -> dict:
    """Send a parked edit back to pending after you've dealt with it."""
    with tx() as c:
        row = edits_svc.requeue_desired(c, post_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Edit is not parked",
            )
        return row


@router.get("/{post_id}/attempts")
def attempts(post_id: str, limit: int = Query(default=20, ge=1, le=100)) -> dict:
    with conn() as c:
        return {"attempts": edits_svc.attempts_for_post(c, post_id, limit=limit)}


# --------------------------------------------------------------------- images
# Attaching is what binds an image to an account permanently (decision 13), so
# these go through services.edits rather than letting the client PUT an id list.


class AttachBody(BaseModel):
    image_id: int
    # Same 24 slots a draft has. This was capped at 5 while editing was a
    # phase-3 spike; the bound belongs to the image model, not to this router.
    slot: int = Field(ge=1, le=images_svc.MAX_SLOTS)
    allow_double_book: bool = False


class AutofillBody(BaseModel):
    count: int = Field(default=images_svc.MAX_SLOTS - 1, ge=1, le=images_svc.MAX_SLOTS - 1)


@router.get("/{post_id}/images")
def list_images(post_id: str) -> dict:
    with conn() as c:
        return {"images": edits_svc.desired_images(c, post_id)}


@router.post("/{post_id}/images")
def attach_image(post_id: str, body: AttachBody) -> dict:
    with tx() as c:
        try:
            return edits_svc.attach_image(
                c, post_id=post_id, image_id=body.image_id, slot=body.slot,
                allow_double_book=body.allow_double_book,
            )
        except ValueError as e:
            # 409, matching the drafts attach route. One shared picker drives
            # both, so it must not have to branch on which one it is talking to.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/{post_id}/images/autofill")
def autofill_images(post_id: str, body: AutofillBody) -> dict:
    """Top up empty photo slots. Never replaces a choice, never touches slot 1."""
    with tx() as c:
        try:
            return edits_svc.autofill_images(c, post_id=post_id, want=body.count)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/{post_id}/images/cover")
def autofill_cover(post_id: str) -> dict:
    with tx() as c:
        try:
            chosen = edits_svc.autofill_cover(c, post_id=post_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return {"image": chosen}


@router.delete("/{post_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def detach_image(post_id: str, image_id: int) -> None:
    with tx() as c:
        if not edits_svc.detach_image(c, post_id=post_id, image_id=image_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Image is not attached"
            )
