from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.db.core import DatabaseCore


@pytest.fixture
def db_core():
    return DatabaseCore()


@pytest.mark.asyncio
async def test_db_core_wait_for_tables(db_core):
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = AsyncMock()

    mock_pool.connection.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cur)

    mock_cur.fetchall.return_value = [("jobs",), ("review_records",), ("worker_heartbeats",)]
    db_core.pool = mock_pool
    await db_core.wait_for_tables(["jobs"])
