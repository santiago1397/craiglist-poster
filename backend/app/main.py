from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import get_settings
from .db import close_pool, init_pool, tx

async def _topup_loop(interval_minutes: int) -> None:
    """Keep the draft queue full.

    Safe under `uvicorn --workers N` and alongside the manual endpoint: the
    lock lives inside `generator.topup`, so every entry point serialises
    through the same gate rather than only the loop guarding itself.
    """
    from .services import generator

    interval = max(60, interval_minutes * 60)
    # Stagger the first run so a restart storm doesn't stampede the model API.
    await asyncio.sleep(20)
    while True:
        try:
            def _run() -> dict:
                with tx() as conn:
                    return generator.topup(conn)

            result = await asyncio.to_thread(_run)
            if result and result.get("created"):
                logger.info(f"draft top-up created {result['created']} draft(s)")
        except Exception as e:  # never let the loop die
            logger.warning(f"draft top-up failed: {e!r}")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting up — initialising DB pool")
    init_pool()
    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.generation_interval_minutes > 0:
        task = asyncio.create_task(_topup_loop(settings.generation_interval_minutes))
        logger.info(
            f"draft top-up loop every {settings.generation_interval_minutes}m"
        )
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
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
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )

    # Routers registered lazily so tests can import create_app cheaply
    from .routers import (
        accounts, artifacts, auth, dashboard, drafts, edits, events, images,
        posts, prompts, queue, reference,
        settings as settings_router,
    )

    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(events.router, prefix="/events", tags=["ingest"])
    app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
    app.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
    app.include_router(posts.router, prefix="/posts", tags=["posts"])
    app.include_router(drafts.router, prefix="/drafts", tags=["drafts"])
    app.include_router(queue.router, prefix="/queue", tags=["queue"])
    app.include_router(settings_router.router, prefix="/settings", tags=["settings"])
    app.include_router(reference.router, prefix="/reference", tags=["reference"])
    app.include_router(images.router, prefix="/images", tags=["images"])
    app.include_router(prompts.router, prefix="/prompts", tags=["prompts"])
    app.include_router(edits.router, prefix="/edits", tags=["edits"])
    app.include_router(artifacts.router, prefix="/artifacts", tags=["artifacts"])

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
