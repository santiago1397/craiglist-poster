from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_admin
from ..db import conn
from ..services.queries import dashboard_accounts

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("")
def get_dashboard() -> dict:
    with conn() as c:
        accounts = dashboard_accounts(c)
    return {"accounts": accounts}
