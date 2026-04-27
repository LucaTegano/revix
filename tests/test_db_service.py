from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.db import DatabaseService


@pytest.fixture
def db_service() -> DatabaseService:
    return DatabaseService()


@pytest.mark.asyncio
async def test_enqueue_if_new_success(db_service: DatabaseService) -> None:
    # Mock psycopg pool, connection and cursor
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.execute = AsyncMock()
    mock_cur.fetchone = AsyncMock()
    mock_cur.fetchall = AsyncMock()

    mock_pool.connection.return_value = mock_conn
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    mock_conn.cursor.return_value = mock_cur
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=None)

    db_service.pool = mock_pool

    # Mock fetchone to return None (SHA doesn't exist)
    mock_cur.fetchone.return_value = None

    result = await db_service.enqueue_if_new("sha123", "owner/repo", 1, 123)

    assert result is True
    # Verify advisory lock was called via cur.execute
    mock_cur.execute.assert_any_call("SELECT pg_advisory_xact_lock(%s)", ANY_ARG_MOCK)


class AnyArg:
    def __eq__(self, other):
        return True


ANY_ARG_MOCK = AnyArg()


@pytest.mark.asyncio
async def test_enqueue_if_new_duplicate(db_service: DatabaseService) -> None:
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.execute = AsyncMock()
    mock_cur.fetchone = AsyncMock()
    mock_cur.fetchall = AsyncMock()

    mock_pool.connection.return_value = mock_conn
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    mock_conn.cursor.return_value = mock_cur
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=None)

    db_service.pool = mock_pool

    # Mock fetchone to return (1,) (SHA already exists)
    mock_cur.fetchone.return_value = (1,)

    result = await db_service.enqueue_if_new("sha123", "owner/repo", 1, 123)

    assert result is False


@pytest.mark.asyncio
async def test_claim_job(db_service: DatabaseService) -> None:
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.execute = AsyncMock()
    mock_cur.fetchone = AsyncMock()
    mock_cur.fetchall = AsyncMock()

    mock_pool.connection.return_value = mock_conn
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    mock_conn.cursor.return_value = mock_cur
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=None)

    db_service.pool = mock_pool

    mock_job: dict[str, Any] = {"id": 1, "commit_sha": "sha123", "payload": "{}"}
    mock_cur.fetchone.return_value = mock_job

    job = await db_service.claim_job("test-worker")

    assert job == mock_job
    # The first call should be the claim query with SKIP LOCKED
    # The second call should be the heartbeat insert
    all_executes = [call[0][0] for call in mock_cur.execute.call_args_list]
    assert any("SKIP LOCKED" in q for q in all_executes)
