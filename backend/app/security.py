from __future__ import annotations

import hmac
import secrets

from fastapi import Header, HTTPException, Request, status

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


# ---------------------------------------------------------------------------
# Agent API keys
#
# Same `<id>.<secret>` + argon2 storage as machine tokens, deliberately in a
# different table — see migration 0018 for why sharing one would be unsafe.
#
# The asymmetry between read and post is the whole design:
#
#   read  — accepted from `?key=`, `X-API-Key`, or `Authorization: Bearer`.
#           Many agent fetch tools cannot set request headers at all, and an
#           API an agent cannot call is not an API. The cost is that the key
#           lands in access logs and shell history.
#   post  — headers only. The no-headers constraint never applied to writes:
#           anything able to issue a POST can set a header. So the leaky path
#           is open exactly where leaking is survivable, and closed on the one
#           verb that publishes to Craigslist.
# ---------------------------------------------------------------------------

_BAD_KEY = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key"
)


def issue_api_key(label: str, scope: str = "read") -> str:
    """Create an agent key. The plaintext is returned exactly once."""
    if scope not in ("read", "post"):
        raise ValueError(f"unknown scope {scope!r}")
    secret = secrets.token_urlsafe(32)
    with conn() as c:
        row = c.execute(
            "INSERT INTO api_keys (label, scope, token_hash) "
            "VALUES (%s, %s, %s) RETURNING id",
            (label, scope, hash_password(secret)),
        ).fetchone()
    return f"{row['id']}.{secret}"


def revoke_api_key(key_id: int) -> bool:
    with conn() as c:
        cur = c.execute(
            "UPDATE api_keys SET revoked_at = NOW() "
            "WHERE id = %s AND revoked_at IS NULL",
            (key_id,),
        )
    return (cur.rowcount or 0) > 0


def _header_key(request: Request) -> str | None:
    """The key as supplied by a caller that can set headers."""
    supplied = request.headers.get("x-api-key")
    if supplied:
        return supplied.strip()
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _resolve(supplied: str | None) -> dict:
    if not supplied:
        raise _BAD_KEY
    key_id, _, secret = supplied.partition(".")
    if not key_id.isdigit() or not secret:
        raise _BAD_KEY
    with conn() as c:
        row = c.execute(
            "SELECT id, label, scope, token_hash FROM api_keys "
            "WHERE id = %s AND revoked_at IS NULL",
            (int(key_id),),
        ).fetchone()
        if row is None or not verify_password(secret, row["token_hash"]):
            raise _BAD_KEY
        c.execute("UPDATE api_keys SET last_seen_at = NOW() WHERE id = %s", (row["id"],))
    return {"id": row["id"], "label": row["label"], "scope": row["scope"]}


def require_agent_read(request: Request) -> dict:
    """Any live key opens the read surface. Query string is allowed here."""
    supplied = _header_key(request) or request.query_params.get("key")
    return _resolve(supplied)


def require_agent_post(request: Request) -> dict:
    """Header-only, scope 'post'.

    A key in the query string is rejected outright rather than merely ignored.
    Falling back to the header on a request that also carried `?key=` would
    still have written the secret to the access log, which is the exact thing
    this rule exists to prevent — so the request fails and the caller is told
    to move it into a header.
    """
    if "key" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Posting requires the key in a header, not the URL. "
                "Send it as 'X-API-Key: <key>'. A key in the query string is "
                "recorded in server access logs."
            ),
        )
    identity = _resolve(_header_key(request))
    if identity["scope"] != "post":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This key is read-only. Posting needs a key created with the "
                "'post' scope in Settings -> API keys."
            ),
        )
    return identity
