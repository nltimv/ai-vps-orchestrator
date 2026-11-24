# syntax=docker/dockerfile:1.7
# aivo backend (API + controller). Build from the repository root:
#   docker build -f backend/Dockerfile -t aivo-backend .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/protocol packages/protocol
COPY worker worker
COPY backend backend
RUN uv sync --frozen --no-dev --no-editable --package aivo-backend

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tini && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 -d /app aivo && mkdir -p /var/lib/aivo/media && chown aivo /var/lib/aivo/media
WORKDIR /app
COPY --from=build /app /app
COPY catalog /app/catalog
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    AIVO_CATALOG_DIR=/app/catalog AIVO_MEDIA_DIR=/var/lib/aivo/media AIVO_SCRATCH_DIR=/dev/shm/aivo
# Release version (<VERSION>.<build number>), set by the release workflow. GPU machines get the worker
# image with this same tag.
ARG AIVO_VERSION=dev
ENV AIVO_VERSION=$AIVO_VERSION
USER aivo
EXPOSE 8000
ENTRYPOINT ["tini", "--"]
CMD ["aivo", "serve", "--role", "all"]
