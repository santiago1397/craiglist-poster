from __future__ import annotations

import hmac
import secrets

from fastapi import Header, HTTPException, status

from .auth import hash_password, verify_password
from .config import get_settings
from .db import conn


def require_ingest_token(authorization: str | None = Header(default=None)) -> None:
    """Guard for /events endpoints. Windows sends `Authorization: Bearer <token>`."""
    settings = get_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, settings.ingest_bearer_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingest token")


# ---------------------------------------------------------------------------
# Per-machine tokens (decision 19)
#
# Separate from INGEST_BEARER_TOKEN so a compromised desktop can be revoked
# without breaking event ingest for every other machine.
#
# Format: "<row id>.<secret>". The id makes verification an indexed lookup plus
# one argon2 check, rather than an argon2 check against every active token.
# ---------------------------------------------------------------------------

_UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid machine token"
)


def issue_machine_token(machine: str, label: str = "") -> str:
    """Create a token for `machine`. The plaintext is returned exactly once."""
    secret = secrets.token_urlsafe(32)
    with conn() as c:
        row = c.execute(
            "INSERT INTO machine_tokens (machine, label, token_hash) "
            "VALUES (%s, %s, %s) RETURNING id",
            (machine, label, hash_password(secret)),
        ).fetchone()
    return f"{row['id']}.{secret}"


def revoke_machine_token(token_id: int) -> bool:
    with conn() as c:
        cur = c.execute(
            "UPDATE machine_tokens SET revoked_at = NOW() "
            "WHERE id = %s AND revoked_at IS NULL",
            (token_id,),
        )
    return (cur.rowcount or 0) > 0


def require_machine_token(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency for the queue endpoints. Returns the machine name."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    token_id, _, secret = supplied.partition(".")
    if not token_id.isdigit() or not secret:
        raise _UNAUTHORISED

    with conn() as c:
        row = c.execute(
            "SELECT id, machine, token_hash FROM machine_tokens "
            "WHERE id = %s AND revoked_at IS NULL",
            (int(token_id),),
        ).fetchone()
        if row is None or not verify_password(secret, row["token_hash"]):
            raise _UNAUTHORISED
        c.execute(
            "UPDATE machine_tokens SET last_seen_at = NOW() WHERE id = %s", (row["id"],)
        )
    return row["machine"]
