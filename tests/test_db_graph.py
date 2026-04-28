from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.db.graph import GraphRepository


@pytest.fixture
def graph_repo():
    return GraphRepository()


class MockAsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def write(self, data):
        pass


@pytest.mark.asyncio
async def test_graph_repo_persist(graph_repo):
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()

    mock_pool.connection.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

    # Mock connection as ACM
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock()

    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__aexit__ = AsyncMock()

    mock_cur.execute = AsyncMock()
    mock_cur.copy.return_value = MockAsyncCM()

    with patch("app.services.db.graph.db_core.get_pool", return_value=mock_pool):
        await graph_repo.persist_repo_graph(
            "owner/repo",
            [
                {
                    "name": "func",
                    "qualified_name": "q",
                    "kind": "f",
                    "file_path": "a.py",
                    "start_line": 1,
                    "end_line": 2,
                    "signature": "s",
                }
            ],
            [
                {
                    "symbol_name": "func",
                    "file_path": "b.py",
                    "line": 1,
                    "context_line": "c",
                    "ref_type": "r",
                }
            ],
        )
        assert mock_cur.execute.called


@pytest.mark.asyncio
async def test_get_external_callers(graph_repo):
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = AsyncMock()

    mock_pool.connection.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cur)

    mock_cur.fetchall.return_value = [{"symbol_name": "func"}]

    with patch("app.services.db.graph.db_core.get_pool", return_value=mock_pool):
        callers = await graph_repo.get_external_callers("owner/repo", ["func"])
        assert len(callers) == 1
        assert callers[0]["symbol_name"] == "func"
