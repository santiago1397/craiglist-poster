from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt

from .config import get_settings

_hasher = PasswordHasher()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _make_token(email: str) -> tuple[str, datetime]:
    settings = get_settings()
    exp = datetime.now(timezone.utc) + timedelta(days=settings.jwt_ttl_days)
    payload = {"sub": email, "exp": int(exp.timestamp())}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, exp


def set_session_cookie(response: Response, email: str) -> None:
    settings = get_settings()
    token, exp = _make_token(email)
    max_age = int((exp - datetime.now(timezone.utc)).total_seconds())
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.cookie_name,
        domain=settings.cookie_domain or None,
        path="/",
    )


def require_admin(request: Request) -> str:
    """FastAPI dependency — returns admin email or raises 401.

    We read the cookie off the raw request (rather than using Cookie()) so
    the cookie name can come from settings without any decorator gymnastics.
    """
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    email = payload.get("sub")
    if not email or email != settings.admin_email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    return email
