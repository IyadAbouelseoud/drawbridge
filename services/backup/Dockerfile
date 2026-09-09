# syntax=docker/dockerfile:1.7
# Drawbridge retention job — pg_dump into object-locked storage, and ledger verification.
#   docker build -f services/backup/Dockerfile .
#
# Its own image rather than a command on the API's, for one reason: `pg_dump`. Putting the
# PostgreSQL client into the shared base would add it to the API, the agent and all five
# MCP servers — seven images carrying a tool that only this job runs, and seven images
# where a compromised process has a database dump utility already installed.
#
# The client must be major version 16 or newer. `pg_dump` refuses a server newer than
# itself, and Debian bookworm ships 15, so this comes from PGDG rather than from Debian.

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
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d -m 0755 /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 drawbridge

WORKDIR /app
COPY --from=builder --chown=drawbridge:drawbridge /app/.venv /app/.venv
COPY --chown=drawbridge:drawbridge packages/ packages/
COPY --chown=drawbridge:drawbridge services/ services/
COPY --chown=drawbridge:drawbridge scripts/ scripts/
USER drawbridge

# `schedule` rather than a one-shot, so `docker compose ps` saying "up" is the truth about
# whether backups are happening. See scripts/retention.py.
CMD ["python", "scripts/retention.py", "schedule"]
