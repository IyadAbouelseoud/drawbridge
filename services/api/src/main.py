"""Drawbridge API — tenants, claims, review queue, webhooks.

The closed loop, in order: `/documents/batch` and `/extraction/run` take the file in,
`/classification/run` corroborates its codes, `/matching/run` allocates, `/triage/evaluate`
decides whether a human is needed, `/review/*` is that human, `/claims/persist` and
`/claims/transition` hold the state, and `/packaging/build` renders what a licensed filer
submits. n8n drives the sequence and holds none of the state (`docs/ARCHITECTURE.md` §4).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text

from services.api.src.auth import (
    AuthMiddleware,
    check_auth_configuration,
    check_identity_posture,
)
from services.api.src.config import Settings, get_settings
from services.api.src.gates import GateRefusedError
from services.api.src.guard import EdgeMiddleware, PrincipalGuard
from services.api.src.killswitch import KillSwitchEngagedError
from services.api.src.routes import (
    claims,
    classification,
    control,
    documents,
    identity,
    matching,
    packaging,
    pipeline,
    review,
    triage,
)
from services.api.src.secrets import check_secret_posture
from services.api.src.sync_db import reset_engine
from services.api.src.telemetry import configure_tracing, instrument_fastapi, shutdown_tracing

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    # Before any connection is opened. A deployment that authenticates nobody should fail
    # here rather than serve every tenant's rows to whoever asks.
    check_auth_configuration(settings)
    # And before it, whether the keys involved are real. Outside development this raises
    # on a placeholder; inside it, it logs the field names and lets the stack come up.
    check_secret_posture(settings)
    # And whether every registered agent has a person answering for it.
    check_identity_posture(settings)
    app.state.engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info("api.startup", environment=settings.environment)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()
        # The synchronous pool is process-wide and outlives any single request, so it has
        # to be released here or the connections linger until Postgres times them out.
        reset_engine()
        # Flush the batch processor; the last seconds of spans are the ones containing
        # whatever made someone restart the container.
        shutdown_tracing()
        log.info("api.shutdown")


_docs = get_settings().docs_enabled

app = FastAPI(
    title="Drawbridge API",
    version="1.1.0",
    summary="Autonomous customs duty recovery",
    lifespan=lifespan,
    # A map of every route, for someone who has not yet found one. Development only
    # unless DRAWBRIDGE_EXPOSE_API_DOCS says otherwise.
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)


@app.exception_handler(GateRefusedError)
async def _gate_refused(_request: Request, exc: GateRefusedError) -> JSONResponse:
    """A decision refused because of who was making it. 403, with the rule named."""
    return JSONResponse(
        status_code=403, content={"detail": {"error": "gate_refused", "message": str(exc)}}
    )


@app.exception_handler(KillSwitchEngagedError)
async def _halted(_request: Request, exc: KillSwitchEngagedError) -> JSONResponse:
    """A mutation stopped by the kill switch below the middleware — a tenant-scoped halt,
    found when the transaction was scoped. 503: the request was fine, the system is not
    accepting it."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "error": "kill_switch_engaged",
                "scope_kind": exc.scope[0],
                "scope_value": exc.scope[1],
                "message": str(exc),
            }
        },
    )


# Added innermost first. Starlette wraps each new middleware around the previous ones, so
# the request meets them in the reverse of this order: the edge (body cap, access log),
# then identity, then the per-principal guard (rate limit, kill switch), then the routes.
# `services/api/src/guard.py` draws the stack.
app.add_middleware(PrincipalGuard, settings=get_settings())

# Every request carries a verified tenant before it reaches a router, and that tenant is
# what the row-level policies compare against — `services/api/src/auth.py` explains why the
# `SET LOCAL` itself stays down in the session helpers rather than happening here.
app.add_middleware(AuthMiddleware, settings=get_settings())
app.add_middleware(EdgeMiddleware, settings=get_settings())

# Instrumented here rather than in the lifespan, and that placement is the whole of it:
# Starlette builds its middleware stack once, so a middleware added at startup is added to
# a list nothing reads again and the request spans never appear. Added last, which makes
# the OpenTelemetry middleware outermost — so a request rejected by the auth middleware
# still produces a span, and a burst of 401s is visible as one.
configure_tracing("drawbridge-api", endpoint=get_settings().otel_exporter_endpoint)
instrument_fastapi(app)


# Registered in pipeline order rather than alphabetically: the list is the closed loop
# — ingest, extract, classify, match, triage, suspend, persist, package — and reading it
# in that order is how someone new finds out what the pipeline actually does.
app.include_router(identity.router)
app.include_router(pipeline.router)
app.include_router(documents.router)
app.include_router(classification.router)
app.include_router(matching.router)
app.include_router(triage.router)
app.include_router(review.router)
app.include_router(claims.router)
app.include_router(packaging.router)
app.include_router(control.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Deliberately dependency-free so the container reports up before deps are."""
    return {"status": "ok"}


async def _check_postgres(app: FastAPI) -> str:
    async with app.state.sessionmaker() as session:
        await session.execute(text("SELECT 1"))
    return "ok"


async def _check_redis(app: FastAPI) -> str:
    await app.state.redis.ping()
    return "ok"


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness. Probes every backing service and reports each independently."""
    checks: dict[str, str] = {}
    for name, probe in (("postgres", _check_postgres), ("redis", _check_redis)):
        try:
            checks[name] = await asyncio.wait_for(probe(app), timeout=3.0)
        except Exception as exc:
            checks[name] = f"error: {type(exc).__name__}"
    settings: Settings = app.state.settings
    return {
        "status": "ok" if all(v == "ok" for v in checks.values()) else "degraded",
        "environment": settings.environment,
        "checks": checks,
    }
