"""Drawbridge API — tenants, claims, review queue, webhooks.

Week 1 scope: process wiring and dependency health only. Claim routes land in week 6.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text

from services.api.src.config import Settings, get_settings

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info("api.startup", environment=settings.environment)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()
        log.info("api.shutdown")


app = FastAPI(
    title="Drawbridge API",
    version="0.1.0",
    summary="Autonomous customs duty recovery",
    lifespan=lifespan,
)


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
