"""
FastAPI application entrypoint.

Serves the REST API (mounted under ``/api/v1``) and, in later phases, the
WebSocket fan-out and the Redis-backed task bridge between the API and the
agent runtime. This module exposes the ``app`` object that
``tests/test_campaigns_api.py`` and ``uvicorn src.api.main:app`` import.
"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import (
    accounts,
    agents,
    campaigns,
    me,
    outreach,
    targeting,
    warmup,
)

API_V1_PREFIX = "/api/v1"

logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan.

    Connects Redis and attaches the campaign<->agent task bridge
    (``MessageOrchestrator``) to ``app.state`` so campaign starts hand work to
    the interaction agents. If Redis is unreachable the app still boots with the
    bridge unset, and campaign starts degrade to the "orchestrator not
    configured" path rather than failing. (The httpx test transport does not run
    lifespan, so tests always take the degraded path.)
    """
    app.state.redis = None
    app.state.task_orchestrator = None

    # Local convenience only: build the schema straight from the models.
    #
    # Deployments do NOT use this — they run `scripts/migrate.py` before the
    # server starts (see the Dockerfile CMD). Enabling it in production would
    # let create_all invent tables no migration describes, and the next real
    # migration would then be written against a schema nobody can reproduce.
    # Off unless explicitly enabled.
    if os.getenv("AUTO_CREATE_TABLES", "").lower() == "true":
        try:
            from src.database.models import Base, import_all_models
            from src.database.session import engine

            import_all_models()
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("AUTO_CREATE_TABLES: schema ensured")
        except Exception as exc:  # pragma: no cover - depends on runtime DB
            logger.warning("AUTO_CREATE_TABLES failed: %s", exc)

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        from redis import asyncio as aioredis

        from src.infrastructure.task_bridge import MessageOrchestrator

        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
        app.state.redis = client
        app.state.task_orchestrator = MessageOrchestrator(client)
        logger.info("Connected to Redis; task bridge active")
    except Exception as exc:  # pragma: no cover - depends on runtime env
        logger.warning("Redis unavailable (%s); campaign execution degraded", exc)

    # Optional in-process scheduler.
    #
    # Production runs the scheduler as its own process (`python -m src.scheduler`)
    # so a stalled sweep cannot degrade request handling and web replicas stay
    # stateless. SCHEDULER_IN_PROCESS exists for local development, where running
    # two processes to watch a warm-up day happen is friction with no benefit.
    #
    # A refusal to start is recorded on app.state and surfaced by /healthz rather
    # than raised: the API is not the scheduler, so a misconfigured scheduler
    # should not take the API down with it — but it must not be invisible either.
    app.state.scheduler_task = None
    app.state.scheduler_status = "not enabled"
    await _maybe_start_scheduler(app)

    try:
        yield
    finally:
        task = getattr(app.state, "scheduler_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if app.state.redis is not None:
            await app.state.redis.aclose()


async def _maybe_start_scheduler(app: FastAPI) -> None:
    """Start the in-process scheduler when explicitly asked to."""
    from src.scheduler.config import SchedulerDisabled, check_startable, load_config

    try:
        config = load_config()
    except SchedulerDisabled as exc:
        app.state.scheduler_status = f"misconfigured: {exc}"
        logger.warning("Scheduler configuration rejected: %s", exc)
        return

    if not config.in_process:
        app.state.scheduler_status = (
            "enabled, running out-of-process"
            if config.enabled
            else "not enabled"
        )
        return

    try:
        check_startable(config, redis_available=app.state.redis is not None)
    except SchedulerDisabled as exc:
        app.state.scheduler_status = f"refused to start: {exc}"
        logger.warning("In-process scheduler refused to start: %s", exc)
        return

    from src.scheduler.runner import run_forever

    app.state.scheduler_task = asyncio.create_task(
        run_forever(config, redis=app.state.redis)
    )
    app.state.scheduler_status = f"running in-process ({'dry-run' if config.dry_run else 'live'})"
    logger.info("In-process scheduler started (%s)", app.state.scheduler_status)


app = FastAPI(
    title="Social Bot LinkedIn API",
    description="Multi-tenant LinkedIn engagement automation API.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: the frontend (Vite dev server / hosted SPA) calls this API cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened to the configured frontend origin in Phase 1
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers under the versioned API prefix.
app.include_router(campaigns.router, prefix=API_V1_PREFIX)
app.include_router(me.router, prefix=API_V1_PREFIX)
app.include_router(agents.router, prefix=API_V1_PREFIX)
app.include_router(accounts.router, prefix=API_V1_PREFIX)
app.include_router(targeting.router, prefix=API_V1_PREFIX)
app.include_router(outreach.router, prefix=API_V1_PREFIX)
app.include_router(warmup.router, prefix=API_V1_PREFIX)


@app.get("/healthz", tags=["system"])
async def healthz(request: Request) -> dict:
    """
    Liveness probe plus a readiness summary of the optional components.

    Always 200 so the platform healthcheck reflects "the process is up", while
    ``components`` tells an operator what the app can actually *do* right now.
    The distinction matters here: without Redis the per-account caps cannot be
    enforced globally, so sending is refused — and that is a very different
    state from "broken", which is why it needs to be visible rather than
    inferred from behavior.
    """
    components: dict = {"api": "ok"}

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        components["redis"] = "not configured"
    else:
        try:
            await redis.ping()
            components["redis"] = "ok"
        except Exception as exc:  # pragma: no cover - depends on runtime env
            components["redis"] = f"error: {exc}"

    try:
        from src.database.session import engine

        async with engine.connect():
            components["database"] = "ok"
    except Exception as exc:  # pragma: no cover - depends on runtime env
        components["database"] = f"error: {exc}"

    components["credentials_encryption"] = (
        "ok" if os.getenv("ENCRYPTION_KEY") else "no ENCRYPTION_KEY (cannot connect accounts)"
    )
    components["llm"] = (
        "openrouter" if os.getenv("OPENROUTER_API_KEY") else "templates (no OPENROUTER_API_KEY)"
    )
    components["sending"] = (
        "enabled"
        if components["redis"] == "ok" or os.getenv("ALLOW_UNCAPPED_SENDING", "").lower() == "true"
        else "disabled (caps cannot be enforced without Redis)"
    )

    # The scheduler needs its own line, and the reason is specific: the warm-up
    # programme has deliberately quiet days (one day in five plans no likes at
    # all), so "nothing happened today" is normal and can never be the signal
    # that something is wrong. Aliveness has to be reported separately from
    # output, otherwise a dead scheduler is indistinguishable from a quiet one.
    components["scheduler"] = getattr(request.app.state, "scheduler_status", "unknown")
    try:
        from src.scheduler import heartbeat

        last = await heartbeat.last_tick(redis)
        if last is not None:
            components["scheduler_last_tick"] = last
    except Exception as exc:  # pragma: no cover - depends on runtime env
        components["scheduler_last_tick"] = {"error": str(exc)}

    return {"status": "ok", "components": components}


def _mount_frontend() -> None:
    """
    Serve the built React SPA (frontend/dist) so the whole product is one URL.

    Mounted last so it never shadows /api or /healthz. Unknown non-API paths
    fall through to index.html for client-side routing. No-ops if the build is
    absent (e.g. running the API alone in tests).
    """
    import os

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
    dist = os.path.abspath(dist)
    index_file = os.path.join(dist, "index.html")
    if not os.path.isdir(dist) or not os.path.isfile(index_file):
        logger.info("Frontend build not found at %s; serving API only", dist)
        return

    assets_dir = os.path.join(dist, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:
        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catch_all(full_path: str) -> FileResponse:
        # API/system routes are matched earlier; everything else is the SPA.
        return FileResponse(index_file)

    logger.info("Serving frontend SPA from %s", dist)


_mount_frontend()
