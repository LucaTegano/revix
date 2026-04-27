.PHONY: help install db web worker lint test clean migrations

help:
	@echo "🛠️  Revix Development CLI (Postgres-Native)"
	@echo "------------------------------------------"
	@echo "make setup      - Full interactive setup"
	@echo "make install    - Install dependencies"
	@echo "make db         - Start Postgres container"
	@echo "make migrations - Run alembic migrations"
	@echo "make web        - Run FastAPI server"
	@echo "make worker     - Run background worker"
	@echo "make lint       - Run ruff and mypy"
	@echo "make test       - Run pytest"

setup:
	@./scripts/setup.sh

install:
	pip install -r requirements.txt
	@if [ ! -f .env ]; then make setup; fi


db:
	docker run -d --name revix-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=revix -p 5432:5432 postgres:18-alpine || docker start revix-db

migrations:
	alembic upgrade head

web:
	uvicorn app.main:app --reload

worker:
	python app/worker.py

lint:
	ruff check .
	mypy .

test:
	export PYTHONPATH=. && pytest

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
