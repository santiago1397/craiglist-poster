"""Failure artifacts — screenshots and HTML dumps from the desktop.

DESIGN.md decision 17. Before this, `_dump_page()` wrote a screenshot and a page
dump into `logs/failures/` on the Windows box, which is exactly where you cannot
read them from. A broken Craigslist selector produced an error string and
nothing else.

Cookie-authenticated reads; the desktop uploads via `PUT /queue/artifacts/{id}`
with its machine token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..auth import require_admin
from ..db import conn, tx

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("")
def list_artifacts(
    post_id: str | None = Query(default=None),
    flow: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    where = ["expires_at > NOW()"]
    params: list = []
    if post_id:
        where.append("post_id = %s")
        params.append(post_id)
    if flow:
        where.append("flow = %s")
        params.append(flow)
    params.append(limit)
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT id, machine, kind, content_type, size_bytes, post_id, account,
                   flow, label, created_at, expires_at
            FROM artifacts
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params,
        ).fetchall()
    return {"artifacts": [dict(r) for r in rows]}


@router.get("/{artifact_id}")
def get_artifact(artifact_id: str) -> Response:
    """Serve the bytes inline so the dashboard can render them in an <img>."""
    with conn() as c:
        row = c.execute(
            "SELECT content_type, data, kind FROM artifacts "
            "WHERE id = %s AND expires_at > NOW()",
            (artifact_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found or expired"
        )
    # HTML dumps are a full copy of a Craigslist page, scripts and all. Serving
    # them as text/plain means you can read the markup without the browser
    # executing someone else's page under our origin.
    media_type = row["content_type"] if row["kind"] == "screenshot" else "text/plain"
    return Response(
        content=bytes(row["data"]),
        media_type=media_type,
        headers={"Content-Security-Policy": "default-src 'none'"},
    )


@router.post("/purge")
def purge_expired() -> dict:
    """Drop artifacts past their 30-day TTL. Safe to call any time."""
    with tx() as c:
        cur = c.execute("DELETE FROM artifacts WHERE expires_at <= NOW()")
    return {"deleted": cur.rowcount or 0}
