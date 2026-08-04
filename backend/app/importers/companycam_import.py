"""Pull photos from CompanyCam into the image stack's pending shelf.

    docker compose exec api python -m app.importers.companycam_import \
        --token "$TOK" --count-only
    docker compose exec api python -m app.importers.companycam_import \
        --token "$TOK" --limit 150

Everything lands `status='pending'`, so nothing it imports can reach Craigslist
until somebody approves it — the pending shelf is decision 20 and this does not
get an exemption from it. Curate with `python -m app.importers.curate`.

Run it as often as you like. The `image_sources` ledger is keyed on CompanyCam's
own photo id, so a second run re-downloads nothing, and a photo you rejected or
deleted stays gone rather than being helpfully re-imported.

Failure policy: one bad photo is a logged line and a `state='failed'` ledger
row, never the end of the run. A bad *token* stops immediately, because every
remaining photo would fail the same way.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import httpx
import psycopg
from loguru import logger
from psycopg.rows import dict_row

from ..config import get_settings
from ..services import companycam, image_import

SOURCE = "companycam"

# Commit every N photos. Never one transaction for the whole run: the live queue
# loop is attaching and publishing against `images` the entire time, and a
# 40-minute open transaction holds a snapshot against all of it.
DEFAULT_BATCH = 25


def _parse_date(value: str | None) -> int | None:
    """Accept YYYY-MM-DD or a raw unix timestamp; the API wants unix seconds."""
    if not value:
        return None
    if value.isdigit():
        return int(value)
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"--start-date/--end-date want YYYY-MM-DD, got {value!r}")
    return int(dt.timestamp())


def _filters(args) -> dict:
    out: dict = {}
    if start := _parse_date(args.start_date):
        out["start_date"] = start
    if end := _parse_date(args.end_date):
        out["end_date"] = end
    # The `[]` suffix is the Rails array convention, and CompanyCam is a Rails
    # app — but this is the one thing in the importer that was not verified
    # against the live API, because it needs a token. The documented parameter
    # names are `project_ids` and `tag_ids`, typed as arrays; if a filtered run
    # comes back with everything or nothing, drop the brackets here first.
    # `--count-only` shows the mismatch without downloading anything.
    if args.project_ids:
        out["project_ids[]"] = [p.strip() for p in args.project_ids.split(",") if p.strip()]
    if args.tag_ids:
        out["tag_ids[]"] = [t.strip() for t in args.tag_ids.split(",") if t.strip()]
    return out


def _skip_reason(photo: dict) -> str | None:
    """Why this photo is not a candidate, or None if it is.

    `internal` is CompanyCam's own "not for outside eyes" flag, which is exactly
    the question being asked here, so it is honoured rather than second-guessed.
    """
    if photo.get("processing_status") not in (None, "processed"):
        return f"processing_status={photo.get('processing_status')}"
    if photo.get("status") not in (None, "active"):
        return f"status={photo.get('status')}"
    if photo.get("internal") is True:
        return "marked internal in CompanyCam"
    return None


def run(args) -> int:
    settings = get_settings()
    token = args.token or settings.companycam_api_token
    if not token:
        print(
            "No CompanyCam token. Pass --token, or set COMPANYCAM_API_TOKEN in\n"
            ".env.prod and restart the container (env_file is baked at container\n"
            "creation, so an already-running container cannot see a new value).",
            file=sys.stderr,
        )
        return 2

    api_base = args.api_base or settings.companycam_api_base
    filters = _filters(args)

    with httpx.Client(follow_redirects=True) as client:
        if args.count_only:
            n = companycam.count_photos(
                client, token=token, api_base=api_base, filters=filters
            )
            print(f"{n} photo(s) match. No bytes downloaded.")
            return 0

        db = psycopg.connect(settings.dsn, row_factory=dict_row)
        try:
            return _import_loop(client, db, args, token=token, api_base=api_base,
                                filters=filters)
        finally:
            db.close()


def _import_loop(client, db, args, *, token, api_base, filters) -> int:
    imported = duplicate = skipped = failed = considered = 0
    pending_commit = 0

    photos = companycam.list_photos(
        client, token=token, api_base=api_base, filters=filters
    )

    for photo in photos:
        if args.limit and considered >= args.limit:
            break
        external_id = str(photo.get("id") or "").strip()
        if not external_id:
            logger.warning("photo with no id in the response; skipped")
            continue
        considered += 1

        entry = image_import.ledger_entry(db, source=SOURCE, external_id=external_id)
        if not image_import.should_import(entry, retry_failed=args.retry_failed):
            skipped += 1
            continue

        common = {
            "source": SOURCE,
            "external_id": external_id,
            "remote_hash": photo.get("hash"),
            "captured_at": image_import.unix_to_dt(photo.get("captured_at")),
            "project_id": (
                str(photo["project_id"]) if photo.get("project_id") else None
            ),
            # Sometimes a rich-text object rather than the documented string.
            "description": image_import.as_text(photo.get("description")),
        }

        reason = _skip_reason(photo)
        if reason:
            skipped += 1
            if not args.dry_run:
                image_import.record(db, state="skipped", error=reason, **common)
                pending_commit += 1
            continue

        chosen = companycam.pick_uri(photo, preferred=args.variant)
        if chosen is None:
            skipped += 1
            logger.warning(f"photo {external_id} has no usable uri; skipped")
            if not args.dry_run:
                image_import.record(db, state="skipped", error="no usable uri",
                                    **common)
                pending_commit += 1
            continue
        _variant, url = chosen

        if args.dry_run:
            imported += 1
            print(f"  would import {external_id}  {url[:90]}")
            continue

        try:
            raw = companycam.download(client, url)
            data = image_import.normalise(
                raw, max_edge=args.max_edge, quality=args.quality
            )
            image_id, was_new = image_import.store_external(
                db, data, source=SOURCE, kind=args.kind, status=args.status
            )
        except Exception as e:
            failed += 1
            logger.warning(f"photo {external_id} failed: {e}")
            image_import.record(db, state="failed", error=str(e),
                                remote_url=url, **common)
            pending_commit += 1
        else:
            if was_new:
                imported += 1
            else:
                duplicate += 1
            image_import.record(
                db,
                state="imported" if was_new else "duplicate",
                image_id=image_id,
                remote_url=url,
                **common,
            )
            pending_commit += 1

        if pending_commit >= args.batch:
            db.commit()
            pending_commit = 0
            logger.info(
                f"…{imported} imported, {duplicate} duplicate, "
                f"{skipped} skipped, {failed} failed"
            )

    if pending_commit:
        db.commit()

    print("")
    print(f"considered: {considered}")
    print(f"  imported: {imported}   (new rows, status={args.status})")
    print(f" duplicate: {duplicate}   (identical bytes already in the stack)")
    print(f"   skipped: {skipped}")
    print(f"    failed: {failed}")
    if args.dry_run:
        print("\nDRY RUN — nothing was downloaded or written.")
    elif imported:
        print(
            f"\nReview them:  python -m app.importers.curate list "
            f"--source {SOURCE} --limit 20"
        )
    return 1 if failed and not imported else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.importers.companycam_import",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--token", help="CompanyCam API token (else COMPANYCAM_API_TOKEN)")
    p.add_argument("--api-base", help="Override the API base URL")
    p.add_argument("--start-date", help="Only photos captured on/after YYYY-MM-DD")
    p.add_argument("--end-date", help="Only photos captured on/before YYYY-MM-DD")
    p.add_argument("--project-ids", help="Comma-separated CompanyCam project ids")
    p.add_argument("--tag-ids", help="Comma-separated CompanyCam tag ids")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after considering this many photos (0 = all)")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                   help=f"Commit every N photos (default {DEFAULT_BATCH})")
    p.add_argument("--variant", default="original", choices=("original", "web"),
                   help="Which CompanyCam size to fetch before re-encoding")
    p.add_argument("--max-edge", type=int, default=image_import.DEFAULT_MAX_EDGE,
                   help="Longest edge in px after re-encode (0 = no resize)")
    p.add_argument("--quality", type=int, default=image_import.DEFAULT_QUALITY,
                   help="JPEG quality after re-encode")
    p.add_argument("--kind", default="photo", choices=("photo", "cover"),
                   help="Which stack these land in (default photo)")
    p.add_argument("--status", default="pending", choices=("pending", "approved"),
                   help="Import straight to approved only if you trust the source")
    p.add_argument("--retry-failed", action="store_true",
                   help="Retry photos past the attempt cap")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be imported; download and write nothing")
    p.add_argument("--count-only", action="store_true",
                   help="Report how many photos match and exit")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except companycam.CompanyCamError as e:
        print(f"CompanyCam API error: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrupted. Committed batches are kept; re-run to resume.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
