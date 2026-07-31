"""Reference data for the composer — counties, cities, zips, phones, license.

Counties and cities are compiled in (see app.reference for why). Phone numbers
are not: they live in `contact_numbers` and are managed here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import conn, tx
from ..reference import as_payload
from ..services import contacts

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/locations")
def locations() -> dict:
    """Service-area data plus the active call-tracking numbers. Constrains the
    composer to values the poster can actually route (see app.reference)."""
    with conn() as c:
        return as_payload(c)


class PhoneCreate(BaseModel):
    number: str = Field(min_length=1, max_length=40)
    label: str = Field(default="", max_length=contacts.MAX_LABEL)


class PhoneUpdate(BaseModel):
    number: str | None = Field(default=None, max_length=40)
    label: str | None = Field(default=None, max_length=contacts.MAX_LABEL)
    active: bool | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)


@router.get("/phones")
def list_phones() -> dict:
    """Every number, retired ones included, in rotation order."""
    with conn() as c:
        return {"phones": contacts.list_numbers(c)}


@router.post("/phones", status_code=status.HTTP_201_CREATED)
def add_phone(body: PhoneCreate) -> dict:
    with tx() as c:
        try:
            return contacts.create(c, number=body.number, label=body.label)
        except contacts.InvalidNumber as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            )


@router.patch("/phones/{phone_id}")
def edit_phone(phone_id: int, body: PhoneUpdate) -> dict:
    with tx() as c:
        try:
            row = contacts.update(c, phone_id, body.model_dump(exclude_unset=True))
        except contacts.InvalidNumber as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Number not found")
    return row


@router.delete("/phones/{phone_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_phone(phone_id: int) -> None:
    """Remove outright. Retiring (active=false) is usually what you want —
    drafts keep whatever number they were written with either way."""
    with tx() as c:
        if not contacts.delete(c, phone_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Number not found")
