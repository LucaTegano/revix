import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import ReviewResult
from app.worker import ReviewWorker


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
    mock_github.fetch_pull_files = AsyncMock(return_value=[])
    mock_github.post_review = AsyncMock()
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
        mock_analyze.assert_called_once_with("diff", "owner/repo", [])
        mock_github.post_review.assert_called_once()
        mock_finalize.assert_called_once_with(
            "550e8400-e29b-41d4-a716-446655440000", 1, "SUCCESS", mock_ai_res.model_dump()
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

        mock_finalize.assert_called_once_with("550e8400-e29b-41d4-a716-446655440000", 1, "FAILURE")
