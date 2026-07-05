from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import require_admin
from ..db import conn
from ..services.queries import post_detail, posts_page

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("")
def list_posts(
    account: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    ghost_filter: str | None = Query(default=None, alias="ghost"),
    since: str | None = Query(default=None, description="'all' | 'YYYY-MM-DD' | omit for 90d"),
    search: str | None = Query(default=None),
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
