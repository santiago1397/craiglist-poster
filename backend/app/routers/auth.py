from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..auth import (
    clear_session_cookie,
    require_admin,
    set_session_cookie,
    verify_password,
)
from ..config import get_settings

router = APIRouter()

# Independent limiter — attaching a limiter directly to a router keeps the
# rate-limit rule collocated with the endpoint it protects. The app also
# registers the global RateLimitExceeded handler.
_limiter = Limiter(key_func=get_remote_address)


class LoginBody(BaseModel):
    email: str
    password: str


class MeResponse(BaseModel):
    email: str


@router.post("/login")
@_limiter.limit("5/minute")
def login(request: Request, body: LoginBody, response: Response) -> MeResponse:
    settings = get_settings()
    if body.email.lower() != settings.admin_email.lower():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not verify_password(body.password, settings.admin_password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    set_session_cookie(response, settings.admin_email)
    return MeResponse(email=settings.admin_email)


@router.post("/logout")
def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(email: str = Depends(require_admin)) -> MeResponse:
    return MeResponse(email=email)
