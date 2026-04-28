# Stage 1: Builder
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

# Stage 2: Test
FROM builder AS tester
COPY . .
# Run tests (this will fail build if tests fail)
# Note: We don't run them here because we need a running Postgres
# But we have the environment ready.

# Stage 3: Runtime
FROM python:3.12-slim-bookworm
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker:26-cli /usr/local/bin/docker /usr/local/bin/docker

# Copy venv WITHOUT dev dependencies for production
COPY --from=builder /app/.venv /app/.venv
COPY . .

RUN chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN groupadd -r revix && useradd -r -g revix revix
RUN chown -R revix:revix /app
USER revix

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

