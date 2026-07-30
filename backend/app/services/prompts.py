"""The prompt library.

Four purposes, each with any number of named prompts and exactly one default.
The default is what automatic generation uses; the rest sit there so you can
try something without losing what currently works.

Editing overwrites in place — there is no version history, by choice. Every
generated image already stores the exact prompt text that produced it, which
answers "why does this one look good?" without a history browser.
"""
from __future__ import annotations

import psycopg

PURPOSES = ("cover_image", "photo_image", "ad_copy", "keyword_tail")

# Placeholders a prompt may use, per purpose. Surfaced in the editor so the
# available variables are discoverable rather than folklore.
VARIABLES: dict[str, list[str]] = {
    "cover_image": ["kind", "city"],
    "photo_image": ["kind", "city"],
    "ad_copy": ["city", "county", "zip_code", "service", "phone", "license", "angle"],
    "keyword_tail": [],
}


def list_prompts(conn: psycopg.Connection, purpose: str | None = None) -> list[dict]:
    if purpose:
        rows = conn.execute(
            "SELECT * FROM prompts WHERE purpose = %s ORDER BY is_default DESC, name",
            (purpose,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM prompts ORDER BY purpose, is_default DESC, name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_prompt(conn: psycopg.Connection, prompt_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM prompts WHERE id = %s", (prompt_id,)).fetchone()
    return dict(row) if row else None


def get_default_body(conn: psycopg.Connection, purpose: str) -> str | None:
    """The prompt automatic generation should use. None if none is set."""
    row = conn.execute(
        "SELECT body FROM prompts WHERE purpose = %s AND is_default LIMIT 1",
        (purpose,),
    ).fetchone()
    return row["body"] if row else None


def create_prompt(conn: psycopg.Connection, *, purpose: str, name: str, body: str,
                  make_default: bool = False) -> dict:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose: {purpose!r}")
    if not name.strip() or not body.strip():
        raise ValueError("name and body are both required")

    # First prompt for a purpose becomes the default automatically — a purpose
    # with prompts but no default would silently fall back to the built-in.
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM prompts WHERE purpose = %s", (purpose,)
    ).fetchone()["n"]
    default = make_default or existing == 0
    if default:
        conn.execute(
            "UPDATE prompts SET is_default = FALSE, updated_at = NOW() "
            "WHERE purpose = %s AND is_default",
            (purpose,),
        )
    row = conn.execute(
        "INSERT INTO prompts (purpose, name, body, is_default) "
        "VALUES (%s,%s,%s,%s) RETURNING *",
        (purpose, name.strip(), body, default),
    ).fetchone()
    return dict(row)


def update_prompt(conn: psycopg.Connection, prompt_id: int, *,
                  name: str | None = None, body: str | None = None) -> dict | None:
    patch: dict = {}
    if name is not None:
        if not name.strip():
            raise ValueError("name cannot be empty")
        patch["name"] = name.strip()
    if body is not None:
        if not body.strip():
            raise ValueError("body cannot be empty")
        patch["body"] = body
    if not patch:
        return get_prompt(conn, prompt_id)
    sets = ", ".join(f"{k} = %({k})s" for k in patch)
    patch["id"] = prompt_id
    row = conn.execute(
        f"UPDATE prompts SET {sets}, updated_at = NOW() WHERE id = %(id)s RETURNING *",
        patch,
    ).fetchone()
    return dict(row) if row else None


def set_default(conn: psycopg.Connection, prompt_id: int) -> dict | None:
    row = conn.execute("SELECT purpose FROM prompts WHERE id = %s", (prompt_id,)).fetchone()
    if row is None:
        return None
    # Clear first: the unique partial index would reject two defaults, and the
    # error would be a constraint violation rather than anything useful.
    conn.execute(
        "UPDATE prompts SET is_default = FALSE, updated_at = NOW() "
        "WHERE purpose = %s AND is_default",
        (row["purpose"],),
    )
    updated = conn.execute(
        "UPDATE prompts SET is_default = TRUE, updated_at = NOW() WHERE id = %s RETURNING *",
        (prompt_id,),
    ).fetchone()
    return dict(updated)


def delete_prompt(conn: psycopg.Connection, prompt_id: int) -> None:
    """Deleting the default promotes another prompt rather than leaving the
    purpose without one, which would silently fall back to the built-in."""
    row = conn.execute(
        "SELECT purpose, is_default FROM prompts WHERE id = %s", (prompt_id,)
    ).fetchone()
    if row is None:
        raise ValueError("prompt not found")
    conn.execute("DELETE FROM prompts WHERE id = %s", (prompt_id,))
    if row["is_default"]:
        nxt = conn.execute(
            "SELECT id FROM prompts WHERE purpose = %s ORDER BY name LIMIT 1",
            (row["purpose"],),
        ).fetchone()
        if nxt:
            set_default(conn, nxt["id"])


# ---------------------------------------------------------------------------
# The editable {kind} list
# ---------------------------------------------------------------------------

def get_image_kinds(conn: psycopg.Connection) -> list[str]:
    row = conn.execute("SELECT image_kinds FROM generation_settings LIMIT 1").fetchone()
    return list(row["image_kinds"] or [])


def set_image_kinds(conn: psycopg.Connection, kinds: list[str]) -> list[str]:
    import json

    cleaned = [k.strip() for k in kinds if k and k.strip()]
    if not cleaned:
        raise ValueError("at least one kind is required — it is substituted into {kind}")
    conn.execute(
        "UPDATE generation_settings SET image_kinds = %s::jsonb, updated_at = NOW() "
        "WHERE singleton",
        (json.dumps(cleaned),),
    )
    return cleaned
