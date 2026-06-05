import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import ReviewResult
from app.worker import ReviewWorker

JOB_ID = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")


@pytest.fixture
def worker() -> ReviewWorker:
    return ReviewWorker()


@pytest.mark.asyncio
async def test_process_job_success(worker: ReviewWorker) -> None:
    job = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "commit_sha": "sha123",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "fence_token": 1,
        "payload": json.dumps({"installation_id": 123}),
    }

    mock_github = MagicMock()
    mock_github.get_token = AsyncMock(return_value="token")
    mock_github.fetch_diff = AsyncMock(return_value="diff")
    mock_github.fetch_pull_request = AsyncMock(return_value={"title": "test", "body": "test"})
    mock_github.fetch_pull_files = AsyncMock(return_value=[])
    mock_github.post_review = AsyncMock()
    mock_github.create_check_run = AsyncMock(return_value=123)
    mock_github.update_check_run = AsyncMock()
    mock_github.close = AsyncMock()

    mock_ai_res = ReviewResult(summary="ok", score=100, comments=[])

    with (
        patch("app.worker.GitHubService", return_value=mock_github),
        patch.object(worker.ai, "analyze_diff", new_callable=AsyncMock) as mock_analyze,
        patch("app.worker.db_service.finalize_job", new_callable=AsyncMock) as mock_finalize,
    ):
        mock_analyze.return_value = mock_ai_res

        await worker.process_job(job)

        mock_github.fetch_pull_files.assert_called_once()
        mock_analyze.assert_called_once_with(
            diff="diff",
            repo_full_name="owner/repo",
            pr_files=[],
            pr_details={"title": "test", "body": "test"},
        )
        mock_github.post_review.assert_called_once()
        mock_finalize.assert_called_once_with(
            JOB_ID, 1, "SUCCESS", mock_ai_res.model_dump()
        )


@pytest.mark.asyncio
async def test_process_job_failure(worker: ReviewWorker) -> None:
    job = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "commit_sha": "sha123",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "fence_token": 1,
        "payload": json.dumps({"installation_id": 123}),
    }

    with (
        patch("app.worker.GitHubService") as mock_github_cls,
        patch("app.worker.db_service.finalize_job", new_callable=AsyncMock) as mock_finalize,
    ):
        mock_github = mock_github_cls.return_value
        mock_github.get_token = AsyncMock(side_effect=Exception("API Error"))
        mock_github.close = AsyncMock()

        await worker.process_job(job)

        mock_finalize.assert_called_once()
        assert mock_finalize.call_args.args[:3] == (JOB_ID, 1, "FAILURE")


@pytest.mark.asyncio
async def test_worker_safe_process_job(worker):
    job = {"id": uuid.uuid4()}
    worker._active_jobs[str(job["id"])] = {"fence_token": 1}

    with patch.object(worker, "process_job", AsyncMock()) as mock_proc:
        await worker._safe_process_job(job)
        assert str(job["id"]) not in worker._active_jobs
        mock_proc.assert_called_once_with(job)


@pytest.mark.asyncio
async def test_worker_shutdown(worker):
    worker.running = True
    job_id = uuid.uuid4()
    worker._active_jobs[str(job_id)] = {"fence_token": 1}

    with (
        patch("app.worker.db_core.disconnect", AsyncMock()),
        patch("app.worker.queue_repo.release_job", AsyncMock()) as mock_release,
        patch("asyncio.gather", AsyncMock()),
    ):
        await worker.shutdown()
        assert mock_release.called


@pytest.mark.asyncio
async def test_reconciliation_loop_full_logic(worker):
    worker.running = True
    job = {
        "github_check_run_id": 123,
        "status": "dead",
        "repo_full_name": "owner/repo",
        "payload": json.dumps({"installation_id": 456}),
    }

    async def mock_sleep(n):
        worker.running = False

    mock_gh = MagicMock()
    mock_gh.get_token = AsyncMock(return_value="token")
    mock_gh.update_check_run = AsyncMock()
    mock_gh.close = AsyncMock()

    with (
        patch("app.worker.queue_repo.reconcile_stale_jobs", AsyncMock(return_value=[job])),
        patch("app.worker.GitHubService", return_value=mock_gh),
        patch("asyncio.sleep", mock_sleep),
    ):
        await worker.reconciliation_loop()
        assert mock_gh.update_check_run.called


@pytest.mark.asyncio
async def test_reconciliation_error_and_missing_id(worker):
    worker.running = True
    job_missing_id = {
        "github_check_run_id": 123,
        "status": "dead",
        "repo_full_name": "owner/repo",
        "payload": json.dumps({}),
    }

    sleep_calls = 0

    async def mock_sleep(n):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            worker.running = False

    with (
        patch("app.worker.queue_repo.reconcile_stale_jobs") as mock_recon,
        patch("asyncio.sleep", mock_sleep),
    ):
        # 1st call: missing ID, 2nd call: Exception
        mock_recon.side_effect = [[job_missing_id], Exception("Boom")]
        await worker.reconciliation_loop()
        assert mock_recon.call_count == 2


@pytest.mark.asyncio
async def test_run_forever_one_pass(worker):
    worker.running = True

    async def mock_acquire():
        worker.running = False

    worker._semaphore.acquire = AsyncMock(side_effect=mock_acquire)
    with (
        patch("app.worker.db_core.connect", AsyncMock()),
        patch("app.worker.db_core.wait_for_tables", AsyncMock()),
        patch("app.worker.queue_repo.claim_job", AsyncMock(return_value=None)),
        patch.object(worker, "reconciliation_loop", AsyncMock()),
    ):
        await worker.run_forever()
        assert worker.running is False


@pytest.mark.asyncio
async def test_worker_rate_limit_handling(worker):
    job = {
        "id": uuid.uuid4(),
        "commit_sha": "sha123",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "fence_token": 1,
        "payload": json.dumps({"installation_id": 123}),
    }

    mock_gh = MagicMock()
    # Mock a 429 error
    error = Exception("429 Too Many Requests")
    mock_gh.get_token = AsyncMock(side_effect=error)
    mock_gh.close = AsyncMock()

    mock_release = AsyncMock()

    with (
        patch("app.worker.GitHubService", return_value=mock_gh),
        patch("app.worker.queue_repo.release_job", mock_release),
    ):
        await worker.process_job(job)
        assert mock_release.called


@pytest.mark.asyncio
async def test_worker_run_heartbeat_exit(worker):
    # Test heartbeat exit when job is not alive
    worker.worker_id = "w1"
    with patch("app.worker.queue_repo.update_heartbeat", AsyncMock(return_value=False)):
        with patch("asyncio.sleep", AsyncMock()):
            await worker._run_heartbeat(uuid.uuid4(), "sha")
