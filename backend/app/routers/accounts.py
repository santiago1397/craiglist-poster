from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_admin
from ..db import conn

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("")
def list_accounts() -> dict:
    """Distinct account names observed via events. Used by the Posts filter dropdown."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT DISTINCT account FROM (
                SELECT account FROM posts
                UNION SELECT account FROM post_attempts
                UNION SELECT account FROM account_states
            ) sub
            ORDER BY account
            """
        ).fetchall()
    return {"accounts": [r["account"] for r in rows]}
