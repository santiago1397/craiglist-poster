"""Post editing: desired state, eligibility, and the reconcile claim.

The dashboard owns *what a post should say*; the desktop reconciles the live
posting to match whenever it can take the browser lease (DESIGN_EDITS.md).

Two things make this deliberately unlike `services.queue`:

- A draft is posted once, so `drafts` can be claim-and-consume. A post is edited
  repeatedly, so this stores **desired state** (decision 25). Editing twice
  before the desktop runs bumps `desired_rev` rather than stacking a second job,
  which keeps reconciles idempotent and safe to retry after a crash.
- A failed post leaves nothing behind; a failed edit acts on something already
  live and earning (decision 32). `apply_attempt` therefore routes on how far
  the desktop got, and has a status — `degraded_live` — with no analogue in the
  posting flow.

Edits carry their own guardrails (decision 30) clamped desktop-side, and every
attempt that touched the browser consumes a slot (decision 31) so a broken
selector cannot retry-loop all day.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from ..config import get_settings
from . import drafts as drafts_svc
from . import images as images_svc
from .queue import get_guardrails

# Fields the operator may set on a desired state. Images are not here: they are
# rows in `post_desired_images`, managed through attach/detach so ownership and
# reuse cooldowns stay owned by `services.images` rather than duplicated.
#
# `county` and `service_offered` are deliberately absent. Craigslist's edit form
# does not expose them — `editor._read_form` reads back "" for both — so
# `editor.reconcile_post` has no control to type them into. While they were
# editable, changing one registered as a text change, started the mutation pass,
# and then reported `applied` with `applied_rev` advanced having typed nothing:
# a green badge for an edit that never happened. Restore them only alongside
# real selectors from the DESIGN_EDITS phase-0 spike.
_EDITABLE = (
    "title", "body", "city",
    "postal_code", "license_number", "phone_number",
    "image_set_managed",
)

# Outcomes that burned a real browser session on the account and therefore
# consume a guardrail slot (decision 31). Skips never opened a browser; a
# dry-run opened one but typed nothing, and rehearsing must stay free or you
# would be rationed out of the safe verification path.
CONSUMING_OUTCOMES = (
    "applied", "no_change", "failed_stale", "failed_gone", "failed_login",
    "failed_form", "failed_images", "failed_other", "degraded_live",
)

# Statuses that need a human before the desktop touches the post again.
PARKED_STATUSES = ("parked_stale", "parked_gone", "degraded_live", "failed")


def _local_now(now: datetime) -> datetime:
    return now.astimezone(get_settings().display_zoneinfo)


# ---------------------------------------------------------------------------
# Desired state
# ---------------------------------------------------------------------------

def get_desired(conn: psycopg.Connection, post_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM post_desired_state WHERE post_id = %s", (post_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_desired(conn: psycopg.Connection, post_id: str, payload: dict) -> dict:
    """Create or update what this post should say, bumping `desired_rev`.

    `base_hash` is captured from the post's last hydration, not from the client:
    it must describe the content the operator was actually shown (decision 26),
    and trusting the client with it would let a stale browser tab defeat the
    staleness check it exists to enforce.
    """
    post = conn.execute(
        "SELECT account, content_hash, hydrated_at FROM posts WHERE post_id = %s",
        (post_id,),
    ).fetchone()
    if post is None:
        raise ValueError(f"unknown post_id {post_id!r}")
    if post["hydrated_at"] is None:
        raise ValueError(
            "post has not been hydrated yet — load it from Craigslist before editing"
        )

    fields: dict[str, Any] = {k: v for k, v in payload.items() if k in _EDITABLE}
    if not fields:
        existing = get_desired(conn, post_id)
        if existing is None:
            raise ValueError("no editable fields supplied")
        return existing

    # The same cap the drafts path enforces. Without it a live posting could be
    # staged with a body Craigslist refuses, and the failure would only surface
    # on the desktop, mid-edit, against a real listing — the most expensive
    # possible place to discover it.
    if "body" in fields:
        drafts_svc.check_body_length(fields["body"])

    existing = get_desired(conn, post_id)
    if existing is None:
        # Seed unset fields from the live post so a partial edit doesn't blank
        # everything else out on apply.
        live = conn.execute(
            "SELECT title, body, county, city, service_offered, postal_code, "
            "license_number, phone_number FROM posts WHERE post_id = %s",
            (post_id,),
        ).fetchone()
        seeded = {k: (live.get(k) or "") for k in (
            "title", "body", "county", "city", "service_offered",
            "postal_code", "license_number", "phone_number",
        )}
        seeded.update(fields)
        seeded.update({
            "post_id": post_id,
            "account": post["account"],
            "base_hash": post["content_hash"],
        })
        cols = ", ".join(seeded)
        placeholders = ", ".join(f"%({k})s" for k in seeded)
        row = conn.execute(
            f"INSERT INTO post_desired_state ({cols}) VALUES ({placeholders}) RETURNING *",
            seeded,
        ).fetchone()
        return dict(row)

    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["post_id"] = post_id
    fields["base_hash"] = post["content_hash"]
    row = conn.execute(
        f"""
        UPDATE post_desired_state
        SET {sets},
            base_hash   = %(base_hash)s,
            desired_rev = desired_rev + 1,
            status      = 'pending',
            failed_step = NULL,
            failed_message = NULL,
            claimed_at  = NULL,
            claimed_by_machine = NULL,
            updated_at  = NOW()
        WHERE post_id = %(post_id)s
        RETURNING *
        """,
        fields,
    ).fetchone()
    return dict(row)


def discard_desired(conn: psycopg.Connection, post_id: str) -> bool:
    """Drop pending changes. Refuses while a machine holds the claim."""
    cur = conn.execute(
        "DELETE FROM post_desired_state WHERE post_id = %s AND status <> 'applying'",
        (post_id,),
    )
    return (cur.rowcount or 0) > 0


def _ensure_desired(conn: psycopg.Connection, post_id: str) -> dict:
    """The post's desired state, created from its live content if absent.

    Staging images used to require a text edit first — `attach_image` refused
    with "edit it first" when no row existed — which made "swap the photos on
    this ad" impossible without also touching the copy. Seeding through
    `upsert_desired` rather than inserting here keeps the two properties that
    matter: the eight live text fields are copied across (so a later reconcile
    does not blank them), and the hydration precondition still applies.
    """
    desired = get_desired(conn, post_id)
    if desired is not None:
        return desired
    return upsert_desired(conn, post_id, {"image_set_managed": True})


def attach_image(
    conn: psycopg.Connection,
    *,
    post_id: str,
    image_id: int,
    slot: int,
    allow_double_book: bool = False,
) -> dict:
    """Bind an image to a slot of this post's desired set.

    Delegates every rule to `images.attach_to_post` so the live-post path cannot
    drift from the draft path again — it previously capped at 5 slots, skipped
    the cover/photo partition and skipped the reservation check, all of which
    the drafts path had been hardened against.

    Taking control of the images is itself a decision, so the first attach flips
    `image_set_managed`. Without that the desktop ships `images: []` on the
    claim (see `claim_reconcile`) and applies only the text — the operator would
    stage a gallery, watch the edit report `applied`, and find the live post
    unchanged with no error anywhere.
    """
    desired = _ensure_desired(conn, post_id)

    out = images_svc.attach_to_post(
        conn,
        post_id=post_id,
        account=desired["account"],
        image_id=image_id,
        slot=slot,
        allow_double_book=allow_double_book,
    )
    if not desired["image_set_managed"]:
        conn.execute(
            "UPDATE post_desired_state SET image_set_managed = TRUE WHERE post_id = %s",
            (post_id,),
        )
    _touch_desired(conn, post_id)
    return out


def detach_image(conn: psycopg.Connection, *, post_id: str, image_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM post_desired_images WHERE post_id = %s AND image_id = %s",
        (post_id, image_id),
    )
    if not cur.rowcount:
        return False
    # Release the claim only if it never published and neither a draft nor
    # another post still holds it.
    conn.execute(
        """
        UPDATE images SET owner_account = NULL, updated_at = NOW()
        WHERE id = %s AND used_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM draft_images WHERE image_id = %s)
          AND NOT EXISTS (SELECT 1 FROM post_desired_images WHERE image_id = %s)
        """,
        (image_id, image_id, image_id),
    )
    _touch_desired(conn, post_id)
    return True


def desired_images(conn: psycopg.Connection, post_id: str) -> list[dict]:
    return images_svc.images_for_post(conn, post_id)


def autofill_images(
    conn: psycopg.Connection, *, post_id: str, want: int
) -> dict:
    """Top up this post's empty photo slots, mirroring the drafts autofill."""
    desired = _ensure_desired(conn, post_id)
    filled = images_svc.fill_post_photo_slots(
        conn, post_id=post_id, account=desired["account"], want=want
    )
    if filled and not desired["image_set_managed"]:
        conn.execute(
            "UPDATE post_desired_state SET image_set_managed = TRUE WHERE post_id = %s",
            (post_id,),
        )
    if filled:
        _touch_desired(conn, post_id)
    return {"filled": len(filled), "requested": want}


def autofill_cover(conn: psycopg.Connection, *, post_id: str) -> dict | None:
    """Pick a cover for slot 1 if it is empty."""
    desired = _ensure_desired(conn, post_id)
    chosen = images_svc.attach_post_cover(
        conn, post_id=post_id, account=desired["account"]
    )
    if chosen is not None:
        if not desired["image_set_managed"]:
            conn.execute(
                "UPDATE post_desired_state SET image_set_managed = TRUE "
                "WHERE post_id = %s",
                (post_id,),
            )
        _touch_desired(conn, post_id)
    return chosen


def _touch_desired(conn: psycopg.Connection, post_id: str) -> None:
    """An image change is a content change: bump the revision so it reconciles."""
    conn.execute(
        """
        UPDATE post_desired_state
        SET desired_rev = desired_rev + 1,
            status = CASE WHEN status = 'applying' THEN status ELSE 'pending' END,
            updated_at = NOW()
        WHERE post_id = %s
        """,
        (post_id,),
    )


def requeue_desired(conn: psycopg.Connection, post_id: str) -> dict | None:
    """Send a parked edit back to pending after you've dealt with it."""
    row = conn.execute(
        """
        UPDATE post_desired_state
        SET status = 'pending',
            failed_step = NULL,
            failed_message = NULL,
            claimed_at = NULL,
            claimed_by_machine = NULL,
            updated_at = NOW()
        WHERE post_id = %s AND status = ANY(%s)
        RETURNING *
        """,
        (post_id, list(PARKED_STATUSES)),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Hydration
# ---------------------------------------------------------------------------

def request_hydration(conn: psycopg.Connection, post_id: str) -> dict:
    """Mark a post as wanting a fresh read of its live edit form (decision 24)."""
    row = conn.execute(
        """
        UPDATE posts
        SET hydrate_requested_at = NOW(), hydrate_error = NULL, updated_at = NOW()
        WHERE post_id = %s
        RETURNING post_id, account, hydrate_requested_at
        """,
        (post_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown post_id {post_id!r}")
    return dict(row)


def record_content(conn: psycopg.Connection, ev) -> bool:
    """Store a hydration result and clear the pending request.

    Only this path writes the content columns on `posts` — a `post_attempt`
    must never overwrite text we read off the live posting.

    Returns False when the event was ignored as stale. The outbox can deliver
    out of order after a retry, and an older read overwriting a newer one would
    silently resurrect content the operator has already seen replaced — and
    would move `content_hash` backwards, breaking decision 26's staleness check.
    """
    current = conn.execute(
        "SELECT hydrated_at FROM posts WHERE post_id = %s", (ev.post_id,)
    ).fetchone()
    if current is None:
        return False
    if current["hydrated_at"] is not None and ev.ts <= current["hydrated_at"]:
        return False

    # The step trail and the artifacts are kept whether the read succeeded or
    # not — a failed hydration is precisely when you need the selector census
    # and the screenshot of the page Craigslist actually served.
    steps = json.dumps([s.model_dump(mode="json") for s in ev.steps])
    artifacts = json.dumps(list(ev.artifact_ids or []))

    if not ev.ok:
        conn.execute(
            """
            UPDATE posts
            SET hydrate_requested_at = NULL,
                hydrate_error = %s,
                hydrate_steps = %s::jsonb,
                hydrate_artifact_ids = %s::jsonb,
                updated_at = NOW()
            WHERE post_id = %s
            """,
            (
                f"{ev.error_type}: {(ev.error_message or '')[:400]}",
                steps, artifacts, ev.post_id,
            ),
        )
        return True

    conn.execute(
        """
        UPDATE posts
        SET title           = COALESCE(%s, title),
            body            = %s,
            -- Craigslist's edit form does not expose the subarea or the service
            -- type, so hydration reads back "" for both. Writing that through
            -- would erase what the posting flow recorded at publish time, which
            -- is the only place these values are ever known.
            county          = COALESCE(NULLIF(%s, ''), county),
            city            = %s,
            service_offered = COALESCE(NULLIF(%s, ''), service_offered),
            postal_code     = %s,
            license_number  = %s,
            phone_number    = %s,
            images          = %s::jsonb,
            content_hash    = %s,
            live_status     = %s,
            editable        = %s,
            hydrated_at     = %s,
            hydrate_requested_at = NULL,
            hydrate_error   = NULL,
            hydrate_steps   = %s::jsonb,
            hydrate_artifact_ids = %s::jsonb,
            updated_at      = NOW()
        WHERE post_id = %s
        """,
        (
            ev.title, ev.body, ev.county, ev.city, ev.service_offered,
            ev.postal_code, ev.license_number, ev.phone_number,
            json.dumps([i.model_dump(mode="json") for i in ev.images]),
            ev.content_hash, ev.live_status, ev.editable, ev.ts,
            steps, artifacts, ev.post_id,
        ),
    )
    return True


# ---------------------------------------------------------------------------
# Eligibility (decision 30)
# ---------------------------------------------------------------------------

def _edits_last_24h_by_account(conn: psycopg.Connection, now: datetime) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT account, COUNT(*) AS n FROM post_edit_attempts
        WHERE ts >= %s AND outcome = ANY(%s)
        GROUP BY account
        """,
        (now - timedelta(hours=24), list(CONSUMING_OUTCOMES)),
    ).fetchall()
    return {r["account"]: r["n"] for r in rows}


def _last_edit_by_post(conn: psycopg.Connection) -> dict[str, datetime]:
    rows = conn.execute(
        """
        SELECT post_id, MAX(ts) AS last_at FROM post_edit_attempts
        WHERE outcome = ANY(%s) GROUP BY post_id
        """,
        (list(CONSUMING_OUTCOMES),),
    ).fetchall()
    return {r["post_id"]: r["last_at"] for r in rows}


def _applied_count_by_post(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT post_id, COUNT(*) AS n FROM post_edit_attempts "
        "WHERE outcome = 'applied' GROUP BY post_id"
    ).fetchall()
    return {r["post_id"]: r["n"] for r in rows}


def evaluate_edit_eligibility(
    conn: psycopg.Connection,
    accounts: list[str],
    now: datetime | None = None,
) -> dict:
    """Why each account may or may not run an edit right now.

    Shaped like `queue.evaluate_eligibility` on purpose — same mental model, and
    the desktop surfaces `global_blocks` verbatim.
    """
    now = now or datetime.now(timezone.utc)
    g = get_guardrails(conn)
    local = _local_now(now)

    global_blocks: list[str] = []
    # Posting's kill switch is the master switch (decision 30): pausing the
    # system pauses everything that drives a browser, not just posting.
    if not g.get("posting_enabled", True):
        reason = (g.get("paused_reason") or "").strip()
        global_blocks.append(
            f"posting paused from the dashboard{': ' + reason if reason else ''}"
        )
    if not g.get("edits_enabled", False):
        reason = (g.get("edits_paused_reason") or "").strip()
        global_blocks.append(
            "editing is disabled"
            + (f": {reason}" if reason else " — enable it under Settings once the "
               "Craigslist edit-form spike is done")
        )
    if not (g["edit_window_start_hour"] <= local.hour < g["edit_window_end_hour"]):
        global_blocks.append(
            f"outside edit window "
            f"({g['edit_window_start_hour']:02d}-{g['edit_window_end_hour']:02d} "
            f"{local.tzname()})"
        )

    per_account = _edits_last_24h_by_account(conn, now)
    depths = pending_depths(conn)

    out: dict[str, dict] = {}
    for name in accounts:
        reasons = list(global_blocks)
        used = per_account.get(name, 0)
        if used >= g["max_edits_per_account_per_day"]:
            reasons.append(
                f"daily edit cap: {used}/{g['max_edits_per_account_per_day']}"
            )
        depth = depths.get(name, 0)
        if depth == 0:
            reasons.append("nothing pending: no edits queued for this account")
        out[name] = {
            "eligible": not reasons,
            "reasons": reasons,
            "edits_last_24h": used,
            "pending_depth": depth,
        }

    return {
        "now": now,
        "global_blocks": global_blocks,
        "accounts": out,
    }


def pending_depths(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT account, COUNT(*) AS n FROM post_desired_state
        WHERE status = 'pending' AND desired_rev > live_rev
        GROUP BY account
        """
    ).fetchall()
    return {r["account"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Work handout
# ---------------------------------------------------------------------------

def pending_work(
    conn: psycopg.Connection,
    *,
    accounts: list[str],
    now: datetime | None = None,
    limit: int = 10,
) -> dict:
    """What this machine could usefully do next.

    Returns hydration requests (always allowed — reading a form is not an edit,
    and blocking it would make the UI unusable while edits are disabled) and
    reconcile candidates (gated on the full guardrail set).
    """
    now = now or datetime.now(timezone.utc)

    hydrate = [
        dict(r)
        for r in conn.execute(
            """
            SELECT post_id, account, url FROM posts
            WHERE hydrate_requested_at IS NOT NULL
              AND account = ANY(%s)
            ORDER BY hydrate_requested_at
            LIMIT %s
            """,
            (accounts, limit),
        ).fetchall()
    ]

    report = evaluate_edit_eligibility(conn, accounts, now=now)
    eligible = [n for n, i in report["accounts"].items() if i["eligible"]]

    reconcile: list[dict] = []
    if eligible:
        g = get_guardrails(conn)
        last_edit = _last_edit_by_post(conn)
        applied = _applied_count_by_post(conn)
        cooldown = timedelta(hours=g["min_hours_between_edits_same_post"])
        lifetime_cap = g["max_edits_per_post_lifetime"]

        rows = conn.execute(
            """
            SELECT d.post_id, d.account, d.desired_rev, p.url
            FROM post_desired_state d
            JOIN posts p ON p.post_id = d.post_id
            WHERE d.status = 'pending'
              AND d.desired_rev > d.live_rev
              AND d.account = ANY(%s)
              AND p.editable
            ORDER BY d.updated_at
            LIMIT %s
            """,
            (eligible, limit),
        ).fetchall()
        for r in rows:
            pid = r["post_id"]
            last = last_edit.get(pid)
            if last is not None and (now - last) < cooldown:
                continue
            if applied.get(pid, 0) >= lifetime_cap:
                continue
            reconcile.append(dict(r))

    return {
        "hydrate": hydrate,
        "reconcile": reconcile,
        "eligibility": report,
    }


def claim_reconcile(
    conn: psycopg.Connection, *, machine: str, post_id: str
) -> dict | None:
    """Atomically take one post's desired state for reconciliation.

    `FOR UPDATE SKIP LOCKED` mirrors the draft claim: two machines racing the
    same post cannot both walk away with it.
    """
    row = conn.execute(
        """
        SELECT post_id FROM post_desired_state
        WHERE post_id = %s AND status = 'pending' AND desired_rev > live_rev
        FOR UPDATE SKIP LOCKED
        """,
        (post_id,),
    ).fetchone()
    if row is None:
        return None

    claimed = conn.execute(
        """
        UPDATE post_desired_state
        SET status = 'applying',
            claimed_at = NOW(),
            claimed_by_machine = %s,
            attempts = attempts + 1,
            last_attempt_at = NOW(),
            updated_at = NOW()
        WHERE post_id = %s
        RETURNING *
        """,
        (machine, post_id),
    ).fetchone()

    live = conn.execute(
        "SELECT url, content_hash, images, live_status FROM posts WHERE post_id = %s",
        (post_id,),
    ).fetchone()

    out = dict(claimed)
    out["url"] = live["url"] if live else None
    out["live_content_hash"] = live["content_hash"] if live else None
    # Only ship images when the operator has taken control of them; otherwise
    # the desktop must leave the live image set untouched.
    out["images"] = (
        [
            {"id": i["id"], "slot": i["slot"], "sha256": i["sha256"], "mime": i["mime"]}
            for i in desired_images(conn, post_id)
        ]
        if claimed["image_set_managed"]
        else []
    )
    return out


def release_stale_claims(conn: psycopg.Connection, older_than_minutes: int = 30) -> int:
    """Return claims whose machine died mid-edit.

    They go back to `pending`, not to a parked status: a claim that never
    produced an attempt event never touched the browser, so retrying is safe.
    """
    cur = conn.execute(
        """
        UPDATE post_desired_state
        SET status = 'pending', claimed_at = NULL, claimed_by_machine = NULL,
            updated_at = NOW()
        WHERE status = 'applying'
          AND claimed_at < NOW() - make_interval(mins => %s)
        """,
        (older_than_minutes,),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Attempt routing (decision 32)
# ---------------------------------------------------------------------------

_OUTCOME_TO_STATUS = {
    "applied": "applied",
    "no_change": "applied",
    "failed_stale": "parked_stale",
    "failed_gone": "parked_gone",
    "degraded_live": "degraded_live",
}

# A failure at or before these steps mutated nothing on Craigslist, so the edit
# can go straight back to pending and retry. Anything later either typed into a
# live form or touched its images, and needs the routing table above.
# `submit_copy`, `open_images_step` and `publish` are deliberately absent.
# Craigslist stages an edit as a draft and only commits it when the hub's
# publish is pressed, so a failure at those steps leaves the live posting
# untouched — but it also leaves a draft session open, and retrying blind is not
# obviously safe until that has been observed. Park them for a human.
PRE_MUTATION_STEPS = frozenset({
    "launch", "lease", "login_check", "open_account_page", "find_post_row",
    "open_edit_form", "open_edit_step", "read_images", "hydrate", "verify_hash",
    "diff",
})


def apply_attempt(conn: psycopg.Connection, ev) -> None:
    """Advance a post's desired state from a reported attempt.

    Called from event ingest on first receipt only, so a replayed event cannot
    park an edit that has since been applied or re-queued.
    """
    desired = get_desired(conn, ev.post_id)
    if desired is None:
        return
    if ev.outcome in ("dry_run", "skipped_not_eligible", "skipped_lease_held"):
        # Rehearsals and skips hold no claim and change nothing.
        return

    if ev.outcome in ("applied", "no_change"):
        conn.execute(
            """
            UPDATE post_desired_state
            SET status = 'applied',
                live_rev = COALESCE(%s, desired_rev),
                failed_step = NULL,
                failed_message = NULL,
                claimed_at = NULL,
                claimed_by_machine = NULL,
                updated_at = NOW()
            WHERE post_id = %s
            """,
            (ev.applied_rev, ev.post_id),
        )
        return

    status = _OUTCOME_TO_STATUS.get(ev.outcome)
    if status is None:
        # A generic failure: safe to retry only if nothing was mutated.
        status = "pending" if ev.failed_step in PRE_MUTATION_STEPS else "failed"

    conn.execute(
        """
        UPDATE post_desired_state
        SET status = %s,
            failed_step = %s,
            failed_message = %s,
            claimed_at = NULL,
            claimed_by_machine = NULL,
            updated_at = NOW()
        WHERE post_id = %s
        """,
        (status, ev.failed_step, (ev.error_message or "")[:1000], ev.post_id),
    )


# ---------------------------------------------------------------------------
# Dashboard queries
# ---------------------------------------------------------------------------

def list_editable_posts(
    conn: psycopg.Connection,
    *,
    account: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if account:
        where.append("p.account = %(account)s")
        params["account"] = account
    if status == "degraded":
        where.append("d.status = 'degraded_live'")
    elif status == "parked":
        where.append("d.status = ANY(ARRAY['parked_stale','parked_gone','failed'])")
    elif status == "pending":
        where.append("d.status = 'pending'")
    elif status == "hydrating":
        where.append("p.hydrate_requested_at IS NOT NULL")
    clause = " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT
            p.post_id, p.account, p.title, p.url, p.posted_ts,
            p.body, p.city, p.county, p.postal_code, p.phone_number,
            p.license_number, p.service_offered,
            p.images, p.content_hash, p.live_status, p.editable,
            p.hydrated_at, p.hydrate_requested_at, p.hydrate_error,
            d.status        AS edit_status,
            d.desired_rev, d.live_rev, d.failed_step, d.failed_message,
            d.attempts      AS edit_attempts,
            d.last_attempt_at,
            (d.post_id IS NOT NULL AND d.desired_rev > d.live_rev) AS has_pending_edit
        FROM posts p
        LEFT JOIN post_desired_state d ON d.post_id = p.post_id
        WHERE {clause}
        ORDER BY
            CASE d.status WHEN 'degraded_live' THEN 0
                          WHEN 'parked_stale' THEN 1
                          WHEN 'parked_gone' THEN 1
                          WHEN 'failed' THEN 1
                          WHEN 'pending' THEN 2
                          ELSE 3 END,
            p.posted_ts DESC NULLS LAST
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM posts p "
        f"LEFT JOIN post_desired_state d ON d.post_id = p.post_id WHERE {clause}",
        params,
    ).fetchone()["n"]
    return {"posts": [dict(r) for r in rows], "total": total}


def attempts_for_post(conn: psycopg.Connection, post_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM post_edit_attempts
        WHERE post_id = %s ORDER BY ts DESC LIMIT %s
        """,
        (post_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def edit_health(conn: psycopg.Connection, accounts: list[str]) -> dict:
    report = evaluate_edit_eligibility(conn, accounts)
    counts = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'pending')       AS pending,
            COUNT(*) FILTER (WHERE status = 'applying')      AS applying,
            COUNT(*) FILTER (WHERE status = 'degraded_live') AS degraded,
            COUNT(*) FILTER (WHERE status IN ('parked_stale','parked_gone','failed')) AS parked
        FROM post_desired_state
        """
    ).fetchone()
    hydrating = conn.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE hydrate_requested_at IS NOT NULL"
    ).fetchone()["n"]
    report.update({k: counts[k] for k in ("pending", "applying", "degraded", "parked")})
    report["hydrating"] = hydrating
    return report
