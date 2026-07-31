"""Image stack endpoints.

Most routes are admin-only. `/images/{id}/raw` is the exception: the Windows
poster must fetch bytes too, so it accepts either the admin cookie or a machine
token. It is the only place those two audiences meet.
"""
from __future__ import annotations

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import storage
from ..auth import require_admin
from ..db import conn, tx
from ..security import require_machine_token
from ..services import images as images_svc

router = APIRouter()
admin = Depends(require_admin)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


class GenerateImagesBody(BaseModel):
    count: int = Field(default=4, ge=1, le=20)
    city: str | None = None
    kind: str = Field(default="photo", pattern="^(photo|cover)$")


class StatusBody(BaseModel):
    # Either or both. `kind` moves an image between the two stacks.
    status: str | None = Field(default=None, pattern="^(pending|approved|rejected)$")
    kind: str | None = Field(default=None, pattern="^(photo|cover)$")


class AttachBody(BaseModel):
    image_id: int
    slot: int = Field(ge=1, le=images_svc.MAX_SLOTS)
    # Set only after the UI has told you which draft already holds this image.
    allow_double_book: bool = False


class AutofillBody(BaseModel):
    # Defaults to filling every photo slot Craigslist allows.
    count: int = Field(default=images_svc.MAX_SLOTS - 1, ge=1, le=images_svc.MAX_SLOTS - 1)


@router.get("", dependencies=[admin])
def list_images(
    status_filter: str | None = Query(default=None, alias="status"),
    bucket: str | None = Query(default=None),
    account: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if bucket and bucket not in images_svc.BUCKETS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"bucket must be one of {', '.join(images_svc.BUCKETS)}",
        )
    with conn() as c:
        return images_svc.list_images(
            c, status=status_filter, bucket=bucket, account=account, kind=kind,
            limit=limit, offset=offset,
        )


@router.get("/stats", dependencies=[admin])
def image_stats() -> dict:
    with conn() as c:
        out = images_svc.stats(c)
    out["storage"] = storage.usage()
    return out


@router.post("/generate", dependencies=[admin])
def generate(body: GenerateImagesBody) -> dict:
    """Generate into the pending shelf. Reports spend for this batch.

    Returns 200 even when the provider fails — the error travels in the body so
    the UI can show it without treating a bad night at the API as a crash.
    """
    with tx() as c:
        return images_svc.generate_images(
            c, count=body.count, city=body.city, kind=body.kind
        )


@router.post("/upload", dependencies=[admin], status_code=status.HTTP_201_CREATED)
async def upload(file: UploadFile = File(...), kind: str = Query(default="photo")) -> dict:
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"expected one of {sorted(ALLOWED_MIME)}, got {file.content_type}",
        )
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file is {len(data)} bytes, limit is {MAX_UPLOAD_BYTES}",
        )
    with tx() as c:
        row = images_svc.store_upload(c, data, mime=file.content_type, kind=kind)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this exact image is already in the stack",
        )
    return row


class OverlayBody(BaseModel):
    # Lines may contain {phone}, {license}, {city}. Omit to use the default
    # call-to-action template.
    lines: list[str] | None = None
    position: str = Field(default="bottom", pattern="^(top|bottom)$")
    phone: str = ""
    license: str = ""
    city: str = ""
    # Replace the source image rather than adding a second row.
    replace: bool = False


@router.post("/{image_id}/overlay", dependencies=[admin])
def apply_overlay(image_id: int, body: OverlayBody) -> dict:
    """Composite call-to-action text onto an image and store the result.

    The model draws the picture; Pillow draws the words, so the phone number is
    always exactly right and re-wording costs nothing (decision 11).
    """
    from ..services import overlay as overlay_svc

    with tx() as c:
        row = c.execute("SELECT * FROM images WHERE id = %s", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
        src = storage.open_path(row["storage_path"])
        if not src.exists():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Image bytes are missing")

        tpl = (
            overlay_svc.OverlayTemplate(lines=body.lines, position=body.position)
            if body.lines
            else overlay_svc.DEFAULT_TEMPLATE
        )
        try:
            rendered = overlay_svc.render(
                src.read_bytes(), tpl,
                {"phone": body.phone, "license": body.license, "city": body.city},
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"could not render overlay: {e}",
            )

        out = images_svc._store(
            c, rendered, source=row["source"], kind="cover",
            prompt=row["prompt"], provider=row["provider"], model=row["model"],
            cost=0, mime="image/jpeg", status="approved",
        )
        if out is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="that exact overlay already exists in the stack",
            )
        if body.replace and row["used_at"] is None:
            images_svc.delete_image(c, image_id)
    return out


@router.patch("/{image_id}", dependencies=[admin])
def patch_image(image_id: int, body: StatusBody) -> dict:
    """Approve/reject, and/or move the image between the cover and photo stacks."""
    if body.status is None and body.kind is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide status, kind, or both",
        )
    with tx() as c:
        row = None
        if body.status is not None:
            row = images_svc.set_status(c, image_id, body.status)
        if body.kind is not None:
            try:
                row = images_svc.set_kind(c, image_id, body.kind)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return row


@router.delete("/{image_id}", dependencies=[admin], status_code=status.HTTP_204_NO_CONTENT)
def delete_image(image_id: int) -> None:
    with tx() as c:
        if not images_svc.delete_image(c, image_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")


@router.get("/{image_id}/thumb")
def thumb(image_id: int, request: Request) -> FileResponse:
    """A downscaled copy, for grids.

    The Images page renders up to 120 tiles a few hundred pixels wide. Serving
    `/raw` into those meant ~94MB of full-resolution JPEG per page view, since
    the stored files average 772KB. This is the same picture at 480px, around
    25KB, rendered once and cached on the volume.

    Falls back to the original if Pillow cannot read the file — a grid with
    heavy images beats a grid with broken ones.
    """
    if not _authorised(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    with conn() as c:
        row = c.execute(
            "SELECT storage_path, mime, sha256 FROM images WHERE id = %s", (image_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    path = storage.get_or_make_thumb(row["storage_path"])
    media_type = "image/jpeg"
    if path is None:
        path = storage.open_path(row["storage_path"])
        media_type = row["mime"]
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Image row exists but its bytes are missing from storage",
            )
    return FileResponse(
        path,
        media_type=media_type,
        # Derived from content-addressed bytes, so this can never go stale.
        headers={"Cache-Control": "public, max-age=31536000, immutable",
                 "ETag": f"{row['sha256']}-t480"},
    )


@router.get("/{image_id}/raw")
def raw(image_id: int, request: Request) -> FileResponse:
    """Serve the bytes. Readable by the dashboard (cookie) or the poster
    (machine token) — the poster needs real files on disk to hand Craigslist."""
    if not _authorised(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    with conn() as c:
        row = c.execute(
            "SELECT storage_path, mime, sha256 FROM images WHERE id = %s", (image_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    path = storage.open_path(row["storage_path"])
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Image row exists but its bytes are missing from storage",
        )
    return FileResponse(
        path,
        media_type=row["mime"],
        # Content-addressed, so the bytes at this id can never change.
        headers={"Cache-Control": "public, max-age=31536000, immutable",
                 "ETag": row["sha256"]},
    )


def _authorised(request: Request) -> bool:
    try:
        require_admin(request)
        return True
    except HTTPException:
        pass
    try:
        require_machine_token(request.headers.get("authorization"))
        return True
    except HTTPException:
        return False


# --- attachment to drafts -------------------------------------------------

@router.get("/draft/{draft_id}", dependencies=[admin])
def for_draft(draft_id: int) -> dict:
    with conn() as c:
        return {"images": images_svc.images_for_draft(c, draft_id)}


@router.post("/draft/{draft_id}/attach", dependencies=[admin])
def attach(draft_id: int, body: AttachBody) -> dict:
    with tx() as c:
        try:
            return images_svc.attach(
                c, draft_id=draft_id, image_id=body.image_id, slot=body.slot,
                allow_double_book=body.allow_double_book,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.post("/draft/{draft_id}/autofill", dependencies=[admin])
def autofill(draft_id: int, body: AutofillBody) -> dict:
    """Fill the draft's empty photo slots from the photo stack.

    Tops up; never replaces what you already chose, and never touches slot 1 —
    the cover stays a manual decision. Returns fewer than asked when the stack
    is short, which with manual refill is the ordinary case rather than a
    failure, so `filled` and `requested` are both reported.
    """
    with tx() as c:
        draft = c.execute(
            "SELECT id, account FROM drafts WHERE id = %s", (draft_id,)
        ).fetchone()
        if draft is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        attached = images_svc.fill_photo_slots(
            c, draft_id=draft_id, account=draft["account"], want=body.count
        )
        health = images_svc.stack_health(c)
    return {
        "filled": len(attached),
        "requested": body.count,
        "images": attached,
        "stack_health": health,
    }


@router.post("/draft/{draft_id}/cover", dependencies=[admin])
def auto_cover(draft_id: int) -> dict:
    """Pick a cover from the stack for a draft that has none.

    The same call `claim_next` makes as a backstop, exposed so you can take it
    deliberately instead of letting the posting slot decide.
    """
    with tx() as c:
        draft = c.execute(
            "SELECT id, account FROM drafts WHERE id = %s", (draft_id,)
        ).fetchone()
        if draft is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        chosen = images_svc.attach_cover(
            c, draft_id=draft_id, account=draft["account"]
        )
    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no cover available — the draft already has one, or the cover stack is empty",
        )
    return chosen


@router.delete("/draft/{draft_id}/attach/{image_id}", dependencies=[admin],
               status_code=status.HTTP_204_NO_CONTENT)
def detach(draft_id: int, image_id: int) -> None:
    with tx() as c:
        if not images_svc.detach(c, draft_id=draft_id, image_id=image_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not attached")
