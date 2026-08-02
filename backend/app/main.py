from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import get_settings
from .db import close_pool, conn, init_pool, tx

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


async def _image_topup_loop(interval_minutes: int) -> None:
    """Keep the photo stack deep enough to fill the queue.

    Gated on `image_topup_enabled`, which ships false — the flag is read every
    cycle rather than at startup, so turning it on in Settings takes effect
    without a redeploy, and turning it off stops the spend immediately.
    """
    from .services import images as images_svc

    interval = max(60, interval_minutes * 60)
    # Offset from the draft loop's stagger so a restart doesn't fire both at
    # once against the same model API.
    await asyncio.sleep(50)
    while True:
        try:
            def _run() -> dict:
                with tx() as conn:
                    return images_svc.topup_stack(conn)

            result = await asyncio.to_thread(_run)
            if result and result.get("created"):
                logger.info(f"image top-up created {result['created']} photo(s)")
        except Exception as e:  # never let the loop die
            logger.warning(f"image top-up failed: {e!r}")
        await asyncio.sleep(interval)


# Hourly is fast enough: Craigslist keeps a finished posting's images up for
# days, and the batch is small so a backlog drains over a few cycles rather than
# hammering their CDN in one burst.
_ARCHIVE_INTERVAL = 3600
_ARCHIVE_BATCH = 10


async def _image_archive_loop() -> None:
    """Keep our own copy of every picture a posting published.

    `posts.images` holds Craigslist's own CDN URLs — written by hydration for
    live postings and by `cl scan-ended` for finished ones. They resolve until
    Craigslist prunes the ad, and after that the record of what an ad looked
    like is gone. That makes this a race, so it runs on its own rather than
    waiting for somebody to press a button.

    Cheap when there is nothing to do: one indexed scan that finds no rows.
    Ungated on purpose — it spends no money and calls no model, it only fetches
    URLs we already recorded.
    """
    from .services import images as images_svc

    await asyncio.sleep(90)  # after both top-up loops have had their turn
    while True:
        try:
            def _run() -> dict:
                with conn() as c:
                    pending = images_svc.posts_needing_archive(c, limit=_ARCHIVE_BATCH)
                stored = 0
                for pid in pending:
                    # A post per transaction: one unreachable image must not
                    # roll back the copies we did manage to keep.
                    with tx() as c:
                        stored += images_svc.archive_post_images(c, pid).get("stored", 0)
                return {"posts": len(pending), "stored": stored}

            result = await asyncio.to_thread(_run)
            if result["stored"]:
                logger.info(
                    f"archived {result['stored']} published image(s) "
                    f"across {result['posts']} post(s)"
                )
        except Exception as e:  # never let the loop die
            logger.warning(f"image archive failed: {e!r}")
        await asyncio.sleep(_ARCHIVE_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting up — initialising DB pool")
    init_pool()
    settings = get_settings()
    tasks: list[asyncio.Task] = []
    if settings.generation_interval_minutes > 0:
        tasks.append(asyncio.create_task(_topup_loop(settings.generation_interval_minutes)))
        logger.info(
            f"draft top-up loop every {settings.generation_interval_minutes}m"
        )
        # Same cadence, separate task: the image loop no-ops cheaply while
        # image_topup_enabled is false, so it costs nothing to have running.
        tasks.append(
            asyncio.create_task(_image_topup_loop(settings.generation_interval_minutes))
        )
    # Independent of generation: archiving is about not losing what already went
    # out, so it must keep running even with draft generation switched off.
    tasks.append(asyncio.create_task(_image_archive_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        logger.info("Shutting down — closing DB pool")
        close_pool()


class _RedactApiKeyFilter(logging.Filter):
    """Keep agent API keys out of the access log.

    The agent surface accepts `?key=` because a large share of AI fetch tools
    cannot set request headers, and an API they cannot call is useless. That
    choice is only defensible if the key does not then get written to disk by
    us — a credential in an access log is a credential in every backup and
    every log shipper downstream of it.

    uvicorn formats access lines lazily: `record.args` is
    (client, method, full_path, http_version, status). Rewriting index 2 before
    formatting redacts the query string without touching anything else.
    """

    _KEY = re.compile(r"([?&]key=)[^&\s]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            if "key=" in args[2].lower():
                record.args = (
                    args[:2] + (self._KEY.sub(r"\1REDACTED", args[2]),) + args[3:]
                )
        return True


def _install_log_redaction() -> None:
    """Attach the redaction filter once, however often create_app is called.

    The logger is process-global while `create_app` is not — tests build
    several apps — and filters appended twice would stack on the same logger.
    """
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactApiKeyFilter) for f in access.filters):
        access.addFilter(_RedactApiKeyFilter())


def create_app() -> FastAPI:
    settings = get_settings()
    _install_log_redaction()
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
        accounts, agent, artifacts, auth, dashboard, diagnostics, drafts, edits,
        events, images, posts, prompts, queue, reference,
        settings as settings_router,
    )

    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    # Read-only surface for AI agents, on its own API-key auth. Deliberately
    # not behind require_admin: the whole point is that it is callable without
    # a browser session.
    app.include_router(agent.router, prefix="/agent", tags=["agent"])
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
    app.include_router(
        diagnostics.router, prefix="/diagnostics", tags=["diagnostics"]
    )

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
