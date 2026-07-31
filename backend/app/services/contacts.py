"""The call-tracking numbers that go on ads.

These used to be a list in `reference.py`, which made adding one a code change
and a redeploy — for something that changes when a campaign changes, not when
the software does.

Two consumers, and they want different things:

* The **composer** offers every active number, in order, and prefills the first.
* The **generator** rotates across the active numbers, because the phone is
  interpolated into the model prompt and asserted to appear in the body. That
  makes rotation a decision taken *before* the model call, not a field patched
  afterwards.

Retiring is `active = false`, not a delete. Drafts already carrying a number
keep it, and the record of what published under which number survives.
"""
from __future__ import annotations

import random
import re

import psycopg
from loguru import logger

# Ten digits is the real constraint — the poster types this into Craigslist's
# phone field. Formatting is left to the operator: "(954) 634-7370" and
# "954-634-7370" are both fine, and the ads have always carried the former.
_DIGITS = re.compile(r"\d")

MAX_LABEL = 60


class InvalidNumber(ValueError):
    """The number could not be a US phone number."""


def normalise(number: str) -> str:
    """Trim and validate. Returns the number as typed, minus outer whitespace.

    Deliberately does not reformat: a number is also a tracking identity, and
    rewriting "(954) 634-7370" into "+19546347370" would make it look like a
    different number to anyone comparing it against a call log.
    """
    cleaned = " ".join((number or "").split())
    digits = _DIGITS.findall(cleaned)
    if len(digits) < 10:
        raise InvalidNumber(
            f"{cleaned!r} has {len(digits)} digits; a US number needs at least 10"
        )
    if len(digits) > 15:  # E.164 ceiling
        raise InvalidNumber(f"{cleaned!r} has {len(digits)} digits, which is too many")
    return cleaned


def list_numbers(conn: psycopg.Connection, *, active_only: bool = False) -> list[dict]:
    where = "WHERE active" if active_only else ""
    rows = conn.execute(
        f"SELECT * FROM contact_numbers {where} ORDER BY position, id"
    ).fetchall()
    return [dict(r) for r in rows]


def active_numbers(conn: psycopg.Connection) -> list[str]:
    return [r["number"] for r in list_numbers(conn, active_only=True)]


def create(conn: psycopg.Connection, *, number: str, label: str = "") -> dict:
    """Add a number. Reviving a retired one rather than duplicating it."""
    value = normalise(number)
    existing = conn.execute(
        "SELECT * FROM contact_numbers WHERE number = %s", (value,)
    ).fetchone()
    if existing is not None:
        if existing["active"]:
            raise InvalidNumber(f"{value} is already in the list")
        row = conn.execute(
            "UPDATE contact_numbers SET active = TRUE, label = %s, updated_at = NOW() "
            "WHERE id = %s RETURNING *",
            (label.strip()[:MAX_LABEL], existing["id"]),
        ).fetchone()
        logger.info(f"reactivated contact number {value}")
        return dict(row)

    nxt = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM contact_numbers"
    ).fetchone()["n"]
    row = conn.execute(
        "INSERT INTO contact_numbers (number, label, position) VALUES (%s,%s,%s) "
        "RETURNING *",
        (value, label.strip()[:MAX_LABEL], nxt),
    ).fetchone()
    logger.info(f"added contact number {value}")
    return dict(row)


def update(conn: psycopg.Connection, number_id: int, patch: dict) -> dict | None:
    """Edit one. Changing the digits does not touch drafts already using it."""
    fields: dict = {}
    if "number" in patch and patch["number"] is not None:
        fields["number"] = normalise(patch["number"])
    if "label" in patch and patch["label"] is not None:
        fields["label"] = str(patch["label"]).strip()[:MAX_LABEL]
    for key in ("active", "position"):
        if patch.get(key) is not None:
            fields[key] = patch[key]
    if not fields:
        row = conn.execute(
            "SELECT * FROM contact_numbers WHERE id = %s", (number_id,)
        ).fetchone()
        return dict(row) if row else None

    if "number" in fields:
        clash = conn.execute(
            "SELECT id FROM contact_numbers WHERE number = %s AND id <> %s",
            (fields["number"], number_id),
        ).fetchone()
        if clash is not None:
            raise InvalidNumber(f"{fields['number']} is already in the list")

    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["id"] = number_id
    row = conn.execute(
        f"UPDATE contact_numbers SET {sets}, updated_at = NOW() "
        f"WHERE id = %(id)s RETURNING *",
        fields,
    ).fetchone()
    return dict(row) if row else None


def delete(conn: psycopg.Connection, number_id: int) -> bool:
    """Remove a number outright.

    Retiring (`active = false`) is usually what you want; this is for one added
    by mistake. Drafts keep whatever number they were written with either way —
    `phone_number` is copied onto the draft, not referenced.
    """
    cur = conn.execute("DELETE FROM contact_numbers WHERE id = %s", (number_id,))
    return (cur.rowcount or 0) > 0


def pick(conn: psycopg.Connection, rng: random.Random | None = None) -> str | None:
    """Choose a number for a new draft, or None if none are active.

    Least-used-first across drafts, so adding a number puts it into circulation
    immediately instead of waiting for chance to even it out. Ties broken
    randomly, which keeps consecutive drafts from marching down the list in a
    pattern.
    """
    rng = rng or random.Random()
    rows = conn.execute(
        """
        SELECT c.number, COUNT(d.id) AS uses
        FROM contact_numbers c
        LEFT JOIN drafts d ON d.phone_number = c.number
        WHERE c.active
        GROUP BY c.number
        ORDER BY uses
        """
    ).fetchall()
    if not rows:
        return None
    fewest = rows[0]["uses"]
    return rng.choice([r["number"] for r in rows if r["uses"] == fewest])
