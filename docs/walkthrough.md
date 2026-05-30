# Walkthrough: Setting up Revix

This guide will take you from a fresh clone to a running, Postgres-native code review system.

## 1. Prerequisites
- Docker & Docker Compose
- Python 3.12+
- An AI Provider API Key (e.g., OpenRouter, OpenAI, or Anthropic)

## 2. Infrastructure Setup
Revix rejects Redis and SQLite. We use PostgreSQL for everything.

```bash
# Start the database
make db
```

## 3. Application Configuration
Copy the template and fill in your secrets.

```bash
cp .env.example .env
```

Key variables:
- `AI_API_KEY`: Required API key for your AI provider/router (e.g., OpenRouter, OpenAI, Google).
- `AI_MODEL_MAP`: The model used for coordinator routing and sub-agent analysis (defaults to `openrouter/google/gemini-2.0-flash-lite:free`).
- `AI_MODEL_REDUCE`: The model used for final review synthesis/reduction (defaults to `openrouter/anthropic/claude-3.5-sonnet`).
- `GITHUB_APP_ID`: Your GitHub App ID.
- `GITHUB_WEBHOOK_SECRET`: Used for webhook signature HMAC verification.
- `GITHUB_APP_PRIVATE_KEY_B64`: Base64 encoded private key of your GitHub App.
- `DATABASE_URL`: PostgreSQL connection string.

## 4. Database Migrations
We use Alembic for zero-downtime schema management.

```bash
# Run migrations
alembic upgrade head
```

## 5. Running the Application
In a production-like environment, you need two processes running concurrently.

### The Ingestion Layer (FastAPI)
This process handles GitHub webhooks, verifies signatures, and enqueues jobs using Postgres advisory locks.
```bash
make web
```

### The Worker Layer
This process polls the Postgres queue using `SKIP LOCKED` and performs the AI Map-Reduce analysis.
```bash
make worker
```

## 6. Verification
To verify the system without GitHub, you can use the provided benchmark script:
```bash
python scripts/benchmark_queue.py
```
This will simulate 100 concurrent webhooks and measure the p99 latency of the Postgres queue.
