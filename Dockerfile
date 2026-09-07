# Shared base image. Every service and MCP server runs from this until week 2, when
# each gets a purpose-built Dockerfile (extraction needs OCR binaries the others don't).
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer, cached independently of source.
COPY pyproject.toml uv.lock* ./
COPY packages/schemas/pyproject.toml packages/schemas/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --extra dev || uv venv && uv pip install -e ".[dev]"

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e . -e ./packages/schemas

RUN useradd --create-home --uid 10001 drawbridge \
 && chown -R drawbridge:drawbridge /app
USER drawbridge

EXPOSE 8000
CMD ["uvicorn", "services.api.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
