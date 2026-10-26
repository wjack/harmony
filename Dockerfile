# syntax=docker/dockerfile:1.27
# check=skip=JSONArgsRecommended
#
# One image for the Render web service and the background worker (ARCHITECTURE.md §3,
# §13). The CMD runs the API; the worker overrides it with `harmony worker`, and the web
# service's pre-deploy command is `alembic upgrade head`. (The skipped check: the CMD is
# shell form on purpose, so $PORT expands, and it execs, so PID 1 still gets signals.)
#
#   docker build -t harmony .
#   docker run --rm -p 10000:10000 harmony

ARG NODE_IMAGE=node:26.12.1-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe
ARG PYTHON_IMAGE=python:3.13.17-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
# Matches the local uv and pyproject's uv_build range.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

FROM ${UV_IMAGE} AS uv

# --- Stage 1: the SPA ----------------------------------------------------------------
FROM ${NODE_IMAGE} AS web

# Node 25+ no longer ships corepack, so install the pnpm that web/package.json's
# packageManager names. CI=true keeps pnpm from ever prompting.
ARG PNPM_VERSION=12.15.0
ENV CI=true
RUN npm install --global --no-fund --no-audit "pnpm@${PNPM_VERSION}"

WORKDIR /build
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml web/
RUN --mount=type=cache,id=harmony-pnpm-store,target=/pnpm/store \
    pnpm -C web install --frozen-lockfile --store-dir /pnpm/store
COPY web/ web/
RUN pnpm -C web build

# --- Stage 2: the Python app ---------------------------------------------------------
FROM ${PYTHON_IMAGE} AS app

# uv is mounted only into the steps that use it, so it stays out of the final image.
# Bytecode is compiled at build time because the app user can't write /app at runtime.
# The venv lives in /app/.venv and harmony is installed editable, so config.REPO_ROOT
# resolves to /app and finds alembic.ini, scenarios/ and web/dist there.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 harmony \
 && useradd --system --uid 10001 --gid harmony --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin harmony

WORKDIR /app

# Third-party dependencies first, so a source edit reuses this layer.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,id=harmony-uv-cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev --no-install-project

COPY pyproject.toml uv.lock .python-version README.md alembic.ini ./
# --parents keeps each directory's name. scenarios/ arrives with the seed loader; the
# wildcard lets the image build before it exists. The cassettes are what
# MODEL_MODE=scripted (the hosted default) plays, at config's default CASSETTE_DIR.
COPY --parents ./src/ ./migrations/ ./scenario[s]/ ./tests/fixtures/cassettes/ ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,id=harmony-uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
 && .venv/bin/python -m compileall -q src migrations

COPY --from=web /build/web/dist web/dist

# The only writable path: OBJECT_STORE=localfs keeps objects under .data/objects.
RUN mkdir -p .data/objects && chown -R harmony:harmony .data

USER harmony
ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 10000

# Shell form so ${PORT} expands (Render sets it); exec makes uvicorn PID 1, so it gets
# SIGTERM directly and shuts down gracefully.
CMD exec uvicorn harmony.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'
