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
# Three scopes, and where each may carry its key is the whole design:
#
#   read  — accepted from `?key=`, `X-API-Key`, or `Authorization: Bearer`.
#           Many agent fetch tools cannot set request headers at all, and an
#           API an agent cannot call is not an API. The cost is that the key
#           lands in access logs and shell history. That cost is affordable
#           precisely because a leaked read key can only read.
#   post  — headers only. The no-headers constraint never applied to writes:
#           anything able to issue a POST can set a header. So the leaky path
#           is open exactly where leaking is survivable, and closed on the one
#           verb that publishes to Craigslist.
#   agent — read + compose + publish in one key, and therefore **headers only
#           on every verb, reads included**. The `?key=` concession was bought
#           with "a leaked read key exposes information, nothing more"; a key
#           that can also publish does not qualify for it. An agent that truly
#           cannot set headers gets a `read` key instead.
# ---------------------------------------------------------------------------

SCOPES = ("read", "post", "agent")

# Scopes allowed to publish, and to reach the compose surface. Kept as data
# rather than inline comparisons so adding a scope later cannot silently miss
# one of the two checks.
_MAY_PUBLISH = frozenset({"post", "agent"})
_MAY_COMPOSE = frozenset({"agent"})

_BAD_KEY = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API key"
)


def _query_string_refused(what: str) -> HTTPException:
    """One wording for every route that will not take a key from the URL.

    Says what to do, not just what went wrong: a caller that gets "401" from a
    valid key retries it, and a caller told to move the key into a header fixes
    it once.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"{what} requires the key in a header, not the URL. "
            "Send it as 'X-API-Key: <key>'. A key in the query string is "
            "recorded in server access logs."
        ),
    )


def issue_api_key(label: str, scope: str = "read") -> str:
    """Create an agent key. The plaintext is returned exactly once."""
    if scope not in SCOPES:
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
    """Any live key opens the read surface. `?key=` is allowed — up to a point.

    A `read` or `post` key may arrive in the query string here, unchanged from
    0018. An `agent` key may not, on any verb: it can publish, so the trade that
    made the query string acceptable ("worst case, rotate it") does not hold for
    it. The scope is only knowable after the lookup, so the refusal happens
    after resolving rather than before — which costs one argon2 verification on
    a request that is about to fail, and buys a message that says what to fix.
    """
    identity = _resolve(_header_key(request) or request.query_params.get("key"))
    # Checked against the raw query params rather than against which source won:
    # if `?key=` is present at all, that value reached the access log, and
    # honouring the header instead would leave the secret written down anyway.
    if identity["scope"] in _MAY_COMPOSE and "key" in request.query_params:
        raise _query_string_refused("An 'agent'-scope key")
    return identity


def require_agent_post(request: Request) -> dict:
    """Header-only. Scope 'post' or 'agent'.

    A key in the query string is rejected outright rather than merely ignored.
    Falling back to the header on a request that also carried `?key=` would
    still have written the secret to the access log, which is the exact thing
    this rule exists to prevent — so the request fails and the caller is told
    to move it into a header.
    """
    if "key" in request.query_params:
        raise _query_string_refused("Posting")
    identity = _resolve(_header_key(request))
    if identity["scope"] not in _MAY_PUBLISH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This key is read-only. Posting needs a key created with the "
                "'post' or 'agent' scope in Settings -> API keys."
            ),
        )
    return identity


def require_agent_compose(request: Request) -> dict:
    """Header-only, scope 'agent'. Guards everything that writes or spends.

    Compose is a narrower privilege than it looks. It can generate images, write
    drafts and attach pictures — but every route behind it is forbidden from
    setting `reviewed`, and `post-now` still refuses an unreviewed draft. So the
    gate that decides whether words go out on a live listing under a real
    licence number remains a human in the dashboard, exactly where it was.
    """
    if "key" in request.query_params:
        raise _query_string_refused("Composing")
    identity = _resolve(_header_key(request))
    if identity["scope"] not in _MAY_COMPOSE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This key cannot compose. Creating drafts, generating images "
                "and attaching them needs a key created with the 'agent' scope "
                "in Settings -> API keys."
            ),
        )
    return identity
