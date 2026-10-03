# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13.16
ARG UV_VERSION=0.12.13

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM python:${PYTHON_VERSION}-slim-trixie AS python-base


FROM python-base AS builder

ARG PYTHON_VERSION

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    UV_PYTHON_DOWNLOADS=0

# Copy dependency metadata first to preserve the expensive dependency layer.
COPY .python-version pyproject.toml uv.lock ./
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml
COPY libs/config/pyproject.toml libs/config/pyproject.toml
COPY libs/schema/pyproject.toml libs/schema/pyproject.toml
COPY libs/genai/pyproject.toml libs/genai/pyproject.toml
COPY libs/observability/pyproject.toml libs/observability/pyproject.toml
COPY libs/google-identity/pyproject.toml libs/google-identity/pyproject.toml
COPY services/migrations/pyproject.toml services/migrations/pyproject.toml
COPY services/chat/pyproject.toml services/chat/pyproject.toml

# Docker cannot populate a pre-FROM ARG from .python-version. Keep the literal
# pins aligned and make drift fail immediately.
RUN test "$(tr -d '\r\n' < .python-version)" = "${PYTHON_VERSION}"

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --locked \
        --no-dev \
        --no-install-workspace \
        --package horizon-migrations

# Copy only this service and the internal libraries in its dependency closure.
COPY services/migrations services/migrations
COPY libs/schema libs/schema

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --locked \
        --no-dev \
        --no-editable \
        --package horizon-migrations


FROM python-base AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

WORKDIR /app

ENV HOME=/home/appuser \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" appuser \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --home-dir /home/appuser \
        --shell /usr/sbin/nologin \
        --no-log-init \
        appuser

COPY --from=builder /app/.venv /app/.venv

USER ${APP_UID}:${APP_GID}

STOPSIGNAL SIGTERM
# The CLI is the entrypoint, so `docker run <image> check` or `sql` selects a subcommand.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/.venv/bin/python", "-m", "horizon_migrations.main"]
CMD ["upgrade"]
