# Stage 1: Builder
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Stage 2: Runtime
FROM python:3.12-slim-bookworm
WORKDIR /app

# Install runtime dependencies (like libpq for psycopg if using binary, but uv sync handled it in venv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY . .

# Ensure scripts are executable
RUN chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Use a non-root user for security
RUN groupadd -r lucai && useradd -r -g lucai lucai
RUN chown -R lucai:lucai /app
USER lucai

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
