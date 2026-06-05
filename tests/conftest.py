import os

# Set dummy environment variables for tests BEFORE any app imports
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "dummy")
os.environ.setdefault("GITHUB_APP_PRIVATE_KEY_B64", "ZHVtbXk=")
os.environ.setdefault("AI_API_KEY", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from app.config import get_settings
from app.main import app
from app.services.db.core import db_core
from app.services.db.queue import queue_repo


@pytest.fixture(scope="session")
def postgres_container():
    """Spins up a testcontainers PostgreSQL database for the test session."""
    try:
        with PostgresContainer("postgres:18-alpine", driver="psycopg") as postgres:
            yield postgres
    except Exception as exc:
        message = str(exc)
        docker_unavailable = (
            "Error while fetching server API version" in message
            or "FileNotFoundError" in message
            or "docker.sock" in message
        )
        if docker_unavailable:
            pytest.skip(f"Docker is not available for Postgres integration tests: {exc}")
        raise


@pytest.fixture(scope="session")
def test_db_url(postgres_container):
    """Returns the URL of the test database."""
    url = postgres_container.get_connection_url()
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest_asyncio.fixture(scope="session")
async def migrate_test_db(test_db_url):
    """Runs Alembic migrations on the test database."""
    alembic_cfg = Config("alembic.ini")
    # Alembic needs synchronous psycopg driver
    alembic_cfg.set_main_option(
        "sqlalchemy.url", test_db_url.replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(alembic_cfg, "head")
    yield


@pytest_asyncio.fixture
async def override_settings(test_db_url, migrate_test_db):
    """Overrides the global settings with the test DB URL and connects db_core."""
    settings = get_settings()
    original_url = settings.DATABASE_URL
    settings.DATABASE_URL = test_db_url

    # Connect directly to ensure DB pool is available before tests
    await db_core.connect()

    yield settings

    # Disconnect after test
    await db_core.disconnect()
    settings.DATABASE_URL = original_url


@pytest_asyncio.fixture
async def app_client(override_settings):
    """Returns an httpx.AsyncClient hooked up to the FastAPI app with lifespan events triggered."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def queue_service(override_settings):
    """Returns the connected queue repository and clears the DB before the test."""
    pool = db_core.get_pool()
    async with pool.connection() as conn:
        async with conn:
            async with conn.cursor() as cur:
                await cur.execute("TRUNCATE TABLE jobs CASCADE;")
    return queue_repo
