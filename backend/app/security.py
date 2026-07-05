from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings


def require_ingest_token(authorization: str | None = Header(default=None)) -> None:
    """Guard for /events endpoints. Windows sends `Authorization: Bearer <token>`."""
    settings = get_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, settings.ingest_bearer_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingest token")
