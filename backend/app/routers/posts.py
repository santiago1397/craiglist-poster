from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import require_admin
from ..db import conn, tx
from ..services import images as images_svc
from ..services.queries import post_detail, posts_page

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("")
def list_posts(
    account: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    ghost_filter: str | None = Query(default=None, alias="ghost"),
    since: str | None = Query(default=None, description="'all' | 'YYYY-MM-DD' | omit for 90d"),
    search: str | None = Query(default=None),
    edit_filter: str | None = Query(
        default=None, alias="edit",
        description="'degraded' | 'parked' | 'pending'",
    ),
    sort: str = Query(default="posted_ts"),
    sort_dir: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    with conn() as c:
        return posts_page(
            c,
            account=account,
            status_filter=status_filter,
            ghost_filter=ghost_filter,
            since=since,
            search=search,
            edit_filter=edit_filter,
            sort=sort,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )


@router.get("/{post_id}")
def get_post(post_id: str) -> dict:
    with conn() as c:
        result = post_detail(c, post_id)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return result


@router.post("/{post_id}/archive-images")
def archive_images(post_id: str) -> dict:
    """Keep our own copies of a posting's pictures.

    A recovered posting's manifest holds Craigslist's own URLs, which resolve
    today and carry no promise about tomorrow. This fetches them from the VPS
    and stores them content-addressed, at `status='archived'` so they can never
    be handed back out to a draft.
    """
    with tx() as c:
        try:
            return images_svc.archive_post_images(c, post_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/archive-images")
def archive_images_batch(
    limit: int = Query(default=25, ge=1, le=200),
) -> dict:
    """Archive the pictures of every posting still pointing only at Craigslist."""
    results = []
    with conn() as c:
        pending = images_svc.posts_needing_archive(c, limit=limit)
    for pid in pending:
        with tx() as c:
            try:
                results.append(images_svc.archive_post_images(c, pid))
            except Exception as e:  # pragma: no cover - one bad post must not stop the rest
                results.append({"post_id": pid, "error": str(e)})
    return {
        "considered": len(pending),
        "stored": sum(r.get("stored", 0) for r in results),
        "failed": sum(r.get("failed", 0) for r in results),
        "posts": results,
    }
