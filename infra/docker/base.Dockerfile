# syntax=docker/dockerfile:1.7
#
# Shared builder + runtime foundation for every Drawbridge Python service.
#
# Week 1 shipped one fat image running all ten containers. That was fine for a bring-up
# and wrong to keep: the extraction service needs Tesseract with Arabic language data
# (~40MB of traineddata plus the OCR engine) that an MCP server has no use for, and a
# shared image means every container carries every dependency.
#
# Per-service Dockerfiles derive from these stages. Build with the repo root as context.

# ---------------------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer, cached independently of source. README.md comes along because the
# root project declares it as its readme and hatchling reads it during metadata build.
COPY pyproject.toml uv.lock* README.md ./
COPY packages/schemas/pyproject.toml packages/schemas/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --no-dev 2>/dev/null \
    || uv sync --no-install-workspace --no-dev

# Workspace sources, then install the members themselves.
COPY packages/ packages/
COPY services/ services/
COPY mcp_servers/ mcp_servers/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# ---------------------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 drawbridge

WORKDIR /app

COPY --from=builder --chown=drawbridge:drawbridge /app/.venv /app/.venv
COPY --chown=drawbridge:drawbridge packages/ packages/
COPY --chown=drawbridge:drawbridge services/ services/
COPY --chown=drawbridge:drawbridge mcp_servers/ mcp_servers/

USER drawbridge
