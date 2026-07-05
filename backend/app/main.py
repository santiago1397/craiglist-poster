from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import get_settings
from .db import close_pool, init_pool


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting up — initialising DB pool")
    init_pool()
    try:
        yield
    finally:
        logger.info("Shutting down — closing DB pool")
        close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Craigslist Automation API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Login-endpoint rate limit lives here so 429s go to the client cleanly.
    limiter = Limiter(key_func=get_remote_address, default_limits=[])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    # Routers registered lazily so tests can import create_app cheaply
    from .routers import accounts, auth, dashboard, events, posts

    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(events.router, prefix="/events", tags=["ingest"])
    app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
    app.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
    app.include_router(posts.router, prefix="/posts", tags=["posts"])

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
