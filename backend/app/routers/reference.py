"""Reference data for the composer — counties, cities, zips, phones, license."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_admin
from ..reference import as_payload

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/locations")
def locations() -> dict:
    """Static service-area data. Constrains the composer to values the poster
    can actually route (see app.reference for why that matters)."""
    return as_payload()
