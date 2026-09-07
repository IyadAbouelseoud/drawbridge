# syntax=docker/dockerfile:1.7
# Extraction service. The only image carrying OCR binaries and Arabic language data —
# which is precisely why per-service Dockerfiles replaced the Week 1 shared image.
#
#   docker build -f services/extraction/Dockerfile .

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY packages/schemas/pyproject.toml packages/schemas/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-workspace --no-dev --extra ocr

COPY packages/ packages/
COPY services/ services/
COPY mcp_servers/ mcp_servers/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --extra ocr


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# tesseract-ocr-ara is the Arabic traineddata; without it the ZATCA Bayan OCR fallback
# silently returns garbage rather than failing. poppler-utils backs pdf2image rasterising.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara \
      poppler-utils \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 drawbridge

WORKDIR /app
COPY --from=builder --chown=drawbridge:drawbridge /app/.venv /app/.venv
COPY --chown=drawbridge:drawbridge packages/ packages/
COPY --chown=drawbridge:drawbridge services/ services/
USER drawbridge

# Fail the build rather than ship an image whose Arabic OCR would degrade silently.
RUN tesseract --list-langs 2>&1 | grep -q '^ara$' \
 && tesseract --list-langs 2>&1 | grep -q '^eng$'

CMD ["python", "-m", "services.extraction.src.worker"]
