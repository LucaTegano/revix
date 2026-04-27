#!/bin/bash
set -e

# Run migrations
echo "🚀 Running database migrations..."
alembic upgrade head

# Start App
echo "✅ Starting FastAPI..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
