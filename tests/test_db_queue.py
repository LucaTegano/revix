import pytest
import json
import uuid
from unittest.mock import AsyncMock, patch, MagicMock, ANY
from app.services.db.queue import JobQueueRepository
from app.services.db import DatabaseService

@pytest.fixture
def queue_repo():
    return JobQueueRepository()

@pytest.fixture
def db_service():
    return DatabaseService()

@pytest.fixture
def mock_db():
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cur = AsyncMock() # Use AsyncMock for the cursor as it's the one we call methods on
    
    # Connection Pool CM
    conn_cm = MagicMock()
    conn_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    conn_cm.__aexit__ = AsyncMock()
    mock_pool.connection.return_value = conn_cm
    
    # Connection (Transaction) CM
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock()
    
    # Cursor CM
    cur_cm = MagicMock()
    cur_cm.__aenter__ = AsyncMock(return_value=mock_cur)
    cur_cm.__aexit__ = AsyncMock()
    mock_conn.cursor.return_value = cur_cm
    
    return mock_pool, mock_conn, mock_cur

@pytest.mark.asyncio
async def test_enqueue_if_new_success(queue_repo, mock_db):

    mock_pool, mock_conn, mock_cur = mock_db
    mock_cur.fetchone.return_value = None
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        enqueued = await queue_repo.enqueue_if_new(
            sha="sha123",
            repo="owner/repo",
            pull_number=1,
            installation_id=123
        )
        assert enqueued is True
        assert mock_cur.execute.called

@pytest.mark.asyncio
async def test_enqueue_if_new_duplicate(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    mock_cur.fetchone.return_value = (1,)
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        enqueued = await queue_repo.enqueue_if_new("sha123", "owner/repo", 1, 123)
        assert enqueued is False

@pytest.mark.asyncio
async def test_claim_job_success(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    job_id = uuid.uuid4()
    mock_cur.fetchone.return_value = {"id": job_id, "commit_sha": "sha123"}
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        job = await queue_repo.claim_job("worker1")
        assert job is not None
        assert job["id"] == job_id

@pytest.mark.asyncio
async def test_finalize_job_success(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    job_id = uuid.uuid4()
    mock_cur.fetchone.return_value = {
        "id": job_id, "commit_sha": "sha123", 
        "pr_number": 1, "repo_full_name": "owner/repo"
    }
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        await queue_repo.finalize_job(job_id, 1, "SUCCESS")
        assert mock_cur.execute.called

@pytest.mark.asyncio
async def test_update_heartbeat(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    mock_cur.fetchone.return_value = ("processing",)
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        res = await queue_repo.update_heartbeat(uuid.uuid4(), "worker1")
        assert res is True

@pytest.mark.asyncio
async def test_set_check_run_id(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        await queue_repo.set_check_run_id(uuid.uuid4(), 123)
        assert mock_cur.execute.called

@pytest.mark.asyncio
async def test_release_job(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        await queue_repo.release_job(uuid.uuid4(), 1)
        assert mock_cur.execute.called

@pytest.mark.asyncio
async def test_database_service_shim(db_service, mock_db):
    # Test that the shim correctly uses the global pool
    mock_pool, _, _ = mock_db
    with patch("app.services.db.db_core.pool", mock_pool):
        assert db_service.pool == mock_pool
        db_service.pool = "new_pool"
        from app.services.db import db_core
        assert db_core.pool == "new_pool"

@pytest.mark.asyncio
async def test_get_latest_check_run_id(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    mock_cur.fetchone.return_value = (456,)
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        check_id = await queue_repo.get_latest_check_run_id("owner/repo", 1)
        assert check_id == 456
        assert mock_cur.execute.called

@pytest.mark.asyncio
async def test_reconcile_stale_jobs(queue_repo, mock_db):
    mock_pool, mock_conn, mock_cur = mock_db
    mock_cur.fetchall.return_value = [{"id": uuid.uuid4(), "status": "pending", "repo_full_name": "r", "payload": "{}"}]
    
    with patch("app.services.db.queue.db_core.get_pool", return_value=mock_pool):
        rows = await queue_repo.reconcile_stale_jobs()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"


