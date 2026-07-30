"""Prompt library — the studio's backend.

Test renders are the only interesting part. They generate real images at real
cost, but land as status='test' so no picker or auto-attach can see them. You
keep the good ones (promoting them to the pending shelf) and the rest are
deleted, either when the studio closes or by the abandoned-render sweep.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import conn, tx
from ..services import images as images_svc
from ..services import prompts as prompts_svc

router = APIRouter(dependencies=[Depends(require_admin)])


class PromptCreate(BaseModel):
    purpose: str = Field(pattern="^(cover_image|photo_image|ad_copy|keyword_tail)$")
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    make_default: bool = False


class PromptUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    body: str | None = None


class KindsUpdate(BaseModel):
    kinds: list[str] = Field(min_length=1, max_length=40)


class TestRender(BaseModel):
    # The wording being tried, which need not be saved yet — the point is to see
    # it before committing.
    body: str = Field(min_length=1)
    kind: str = Field(default="photo", pattern="^(photo|cover)$")
    city: str | None = None
    count: int = Field(default=2, ge=1, le=8)


@router.get("")
def list_prompts(purpose: str | None = Query(default=None)) -> dict:
    with conn() as c:
        return {
            "prompts": prompts_svc.list_prompts(c, purpose),
            "variables": prompts_svc.VARIABLES,
            "image_kinds": prompts_svc.get_image_kinds(c),
        }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_prompt(body: PromptCreate) -> dict:
    with tx() as c:
        try:
            return prompts_svc.create_prompt(
                c, purpose=body.purpose, name=body.name, body=body.body,
                make_default=body.make_default,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@router.patch("/{prompt_id}")
def update_prompt(prompt_id: int, body: PromptUpdate) -> dict:
    with tx() as c:
        try:
            row = prompts_svc.update_prompt(c, prompt_id, name=body.name, body=body.body)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt not found")
    return row


@router.post("/{prompt_id}/default")
def make_default(prompt_id: int) -> dict:
    with tx() as c:
        row = prompts_svc.set_default(c, prompt_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt not found")
    return row


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prompt(prompt_id: int) -> None:
    with tx() as c:
        try:
            prompts_svc.delete_prompt(c, prompt_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prompt not found")


@router.put("/image-kinds")
def set_kinds(body: KindsUpdate) -> dict:
    """The list {kind} is drawn from. This is how one prompt yields varied
    images instead of fifty pictures of the same house."""
    with tx() as c:
        try:
            return {"image_kinds": prompts_svc.set_image_kinds(c, body.kinds)}
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@router.post("/test")
def test_render(body: TestRender) -> dict:
    """Render a prompt without committing it to the stack.

    Results are status='test': invisible to the pickers, to auto-attach and to
    the Images page, so tuning a prompt over a dozen attempts leaves nothing to
    clean up.
    """
    with tx() as c:
        # Sweep anything a previous session abandoned before adding more.
        images_svc.purge_test_renders(c)
        return images_svc.generate_images(
            c, count=body.count, city=body.city, kind=body.kind,
            prompt_override=body.body, status="test",
        )


@router.post("/test/{image_id}/keep")
def keep_render(image_id: int) -> dict:
    """Promote a test render onto the pending shelf."""
    with tx() as c:
        row = images_svc.set_status(c, image_id, "pending")
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return row


@router.post("/test/discard")
def discard_renders() -> dict:
    """Drop every test render. Called when the studio closes."""
    with tx() as c:
        n = images_svc.purge_test_renders(c, older_than_hours=0)
    return {"discarded": n}
