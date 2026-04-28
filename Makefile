.PHONY: help setup install db migrations web worker lint format test test-fast test-ai prod setup-gvisor clean

help:
	@echo "🛠️  Revix Development CLI (Postgres-Native)"
	@echo "------------------------------------------"
	@echo "make setup      - Full interactive setup"
	@echo "make install    - Install dependencies with uv"
	@echo "make db         - Start Postgres container (v18)"
	@echo "make migrations - Run alembic migrations"
	@echo "make web        - Run FastAPI server (with reload)"
	@echo "make worker     - Run background worker"
	@echo "make lint       - Run ruff and mypy"
	@echo "make format     - Run ruff format"
	@echo "make test       - Run pytest with coverage gate (80%)"
	@echo "make test-fast  - Run pytest quickly (no coverage)"
	@echo "make test-ai    - Verify AI API connectivity"
	@echo "make prod       - Deploy locally with Docker Compose"
	@echo "make clean      - Remove caches and pycache"

setup:
	@./scripts/setup.sh

install:
	uv sync
	@if [ ! -f .env ]; then make setup; fi

db:
	docker run -d --name revix-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=revix -p 5432:5432 postgres:18-alpine || docker start revix-db

migrations:
	uv run alembic upgrade head

web:
	uv run uvicorn app.main:app --reload

worker:
	uv run python app/worker.py

lint:
	uv run ruff check .
	uv run mypy app

format:
	uv run ruff format .

test:
	uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80

test-fast:
	uv run pytest -n auto --dist loadscope

test-ai:
	uv run python3 scripts/verify_ai.py

prod:
	docker compose up -d --build

setup-gvisor:
	@echo "Installing gVisor (runsc)..."
	@curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
	@echo "deb [arch=$$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
	@sudo apt-get update && sudo apt-get install -y runsc
	@echo "Configuring Docker runtime..."
	@sudo runsc install
	@sudo systemctl reload docker
	@echo "gVisor setup complete."

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml

