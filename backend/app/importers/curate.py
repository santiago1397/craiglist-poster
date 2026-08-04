"""Approve, reject or delete imported images in bulk, by query.

    docker compose exec api python -m app.importers.curate list    --source companycam --limit 20
    docker compose exec api python -m app.importers.curate approve --source companycam --limit 50 --yes
    docker compose exec api python -m app.importers.curate reject  --ids 41,42,43 --yes

This exists because the dashboard cannot review an import of this size. The
Images grid implements "Load more" by growing `limit` against an `le=200` cap on
`GET /images`, so it fails once a bucket passes 200 rows, and `PATCH
/images/{id}` is one image at a time — a 3,000-photo review would be 3,000
clicks against a page that breaks on the sixth.

Nothing here bypasses the stack's own rules: `approve` sets `status='approved'`,
which is the same transition the dashboard's Approve button makes, and
`pick_for_draft` still applies the reuse cooldown and reservation on top.

`delete` is the only destructive verb, and it frees the bytes only for images
that never published — `delete_image` deliberately keeps the file for anything
Craigslist has already seen, so an audit of a live ad still resolves.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from ..config import get_settings
from ..services import images as images_svc

ACTIONS = ("list", "approve", "reject", "delete")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"dates want YYYY-MM-DD, got {value!r}")


def _where(args) -> tuple[str, dict]:
    """Build the row filter. Deliberately explicit — a curation tool that
    silently matches more than you asked for is worse than no tool."""
    clauses: list[str] = []
    params: dict = {}

    if args.ids:
        ids = [int(i) for i in args.ids.split(",") if i.strip()]
        if not ids:
            raise SystemExit("--ids was empty")
        clauses.append("i.id = ANY(%(ids)s)")
        params["ids"] = ids
    else:
        # Without explicit ids, a status filter is mandatory: `approve` with no
        # predicate would sweep up rejected and archived rows too.
        clauses.append("i.status = %(status)s")
        params["status"] = args.status

    if args.source:
        clauses.append("i.source = %(source)s")
        params["source"] = args.source
    if args.kind:
        clauses.append("i.kind = %(kind)s")
        params["kind"] = args.kind
    if args.project_ids:
        projects = [p.strip() for p in args.project_ids.split(",") if p.strip()]
        clauses.append(
            "EXISTS (SELECT 1 FROM image_sources s WHERE s.image_id = i.id "
            "AND s.project_id = ANY(%(projects)s))"
        )
        params["projects"] = projects
    if since := _parse_date(args.captured_since):
        clauses.append(
            "EXISTS (SELECT 1 FROM image_sources s WHERE s.image_id = i.id "
            "AND s.captured_at >= %(since)s)"
        )
        params["since"] = since
    if until := _parse_date(args.captured_until):
        clauses.append(
            "EXISTS (SELECT 1 FROM image_sources s WHERE s.image_id = i.id "
            "AND s.captured_at <= %(until)s)"
        )
        params["until"] = until

    return " AND ".join(clauses), params


def _select(db, args) -> list[dict]:
    where, params = _where(args)
    params["lim"] = args.limit
    rows = db.execute(
        # DISTINCT ON (i.id) because image_sources is deliberately N→1: two
        # CompanyCam photos that normalise to identical bytes share one image
        # row, and a plain LEFT JOIN would then return that image once per
        # ledger entry. That is not cosmetic — the duplicates would eat the
        # LIMIT, so `--limit 50` would act on fewer than 50 distinct images and
        # report a count and a byte total that double-count.
        #
        # The inner ORDER BY picks which ledger row represents the image (the
        # most recently captured); the outer one is the display order.
        f"""
        SELECT * FROM (
            SELECT DISTINCT ON (i.id)
                   i.id, i.status, i.kind, i.source, i.bytes_size, i.used_at,
                   i.owner_account,
                   s.external_id, s.project_id, s.captured_at, s.description
            FROM images i
            LEFT JOIN image_sources s ON s.image_id = i.id
            WHERE {where}
            ORDER BY i.id, s.captured_at DESC NULLS LAST
        ) x
        ORDER BY x.captured_at DESC NULLS LAST, x.id DESC
        LIMIT %(lim)s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _print(rows: list[dict]) -> None:
    if not rows:
        print("No matching images.")
        return
    total_mb = sum(r["bytes_size"] or 0 for r in rows) / (1024 * 1024)
    print(f"{len(rows)} image(s), {total_mb:.1f}MB")
    print(f"  {'id':>7}  {'status':9}  {'kind':6}  {'size':>7}  {'captured':10}  description")
    for r in rows:
        when = r["captured_at"].strftime("%Y-%m-%d") if r["captured_at"] else "-"
        size = f"{(r['bytes_size'] or 0) / 1024:.0f}K"
        desc = (r["description"] or "")[:44]
        print(
            f"  {r['id']:>7}  {r['status']:9}  {r['kind']:6}  {size:>7}  "
            f"{when:10}  {desc}"
        )


def run(args) -> int:
    if args.action != "list" and not args.ids and not args.source and not args.all:
        print(
            "Refusing an unscoped bulk change. Give --source, --ids, or --all.",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    db = psycopg.connect(settings.dsn, row_factory=dict_row)
    try:
        rows = _select(db, args)
        _print(rows)
        if args.action == "list" or not rows:
            return 0

        if not args.yes:
            print(
                f"\nWould {args.action} {len(rows)} image(s). "
                f"Re-run with --yes to do it."
            )
            return 0

        changed = 0
        for r in rows:
            if args.action == "delete":
                changed += 1 if images_svc.delete_image(db, r["id"]) else 0
            else:
                target = "approved" if args.action == "approve" else "rejected"
                changed += 1 if images_svc.set_status(db, r["id"], target) else 0
        db.commit()
        print(f"\n{args.action}d {changed} image(s).")
        if args.action == "approve":
            print("They are now selectable by autofill on the next draft.")
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.importers.curate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("--source", help="e.g. companycam, generated, uploaded")
    p.add_argument("--status", default="pending",
                   help="Status to match when --ids is not given (default pending)")
    p.add_argument("--kind", choices=("photo", "cover"))
    p.add_argument("--ids", help="Comma-separated image ids; overrides --status")
    p.add_argument("--project-ids", help="Comma-separated CompanyCam project ids")
    p.add_argument("--captured-since", help="YYYY-MM-DD, on the CompanyCam capture date")
    p.add_argument("--captured-until", help="YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=50,
                   help="Max rows to act on in one run (default 50)")
    p.add_argument("--all", action="store_true",
                   help="Allow a change scoped only by --status")
    p.add_argument("--yes", action="store_true", help="Actually make the change")
    return p


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
