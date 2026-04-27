import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.db import DatabaseService


@pytest.fixture
def db_service() -> DatabaseService:
    return DatabaseService()


@pytest.mark.asyncio
async def test_finalize_job_success(db_service: DatabaseService) -> None:
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

    # Mock the SELECT query for job info
    mock_cur.fetchone.return_value = {
        "id": "uuid123",
        "commit_sha": "sha123",
        "pr_number": 1,
        "repo_full_name": "owner/repo",
    }

    job_id = uuid.uuid4()
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        await db_service.finalize_job(job_id, 1, "SUCCESS", {"summary": "good"})

    # Verify: 1 SELECT, 1 INSERT (review_records), 1 UPDATE (jobs)
    # Total 3 executes
    assert mock_cur.execute.call_count == 3
    assert mock_cur.fetchone.call_count == 1


@pytest.mark.asyncio
async def test_reconcile_stale_jobs(db_service: DatabaseService) -> None:
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

    mock_cur.fetchall.return_value = []

    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        await db_service.reconcile_stale_jobs()

    # Check if the query uses heartbeat_at instead of locked_at
    query = mock_cur.execute.call_args_list[0][0][0]
    assert "heartbeat_at" in query
    assert "UPDATE jobs" in query
