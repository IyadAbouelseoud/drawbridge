# syntax=docker/dockerfile:1.7
# Drawbridge API. Build with the repo root as context:
#   docker build -f services/api/Dockerfile .

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY packages/schemas/pyproject.toml packages/schemas/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-install-workspace --no-dev

COPY packages/ packages/
COPY services/ services/
COPY mcp_servers/ mcp_servers/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"

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

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=30s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "services.api.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
