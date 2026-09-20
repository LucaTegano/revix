import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.ai import AIService
from app.services.github import GitHubService
from app.worker import ReviewWorker


@pytest.mark.asyncio
async def test_worker_finalizes_invalid_job_payload() -> None:
    worker = ReviewWorker()
    job_id = uuid.uuid4()
    job = {
        "id": str(job_id),
        "commit_sha": "sha123",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "fence_token": 7,
        "payload": json.dumps({}),
    }

    with patch("app.worker.db_service.finalize_job", new_callable=AsyncMock) as mock_finalize:
        await worker.process_job(job)

    mock_finalize.assert_called_once()
    assert mock_finalize.call_args.args[:3] == (job_id, 7, "FAILURE")
    assert "installation_id" in mock_finalize.call_args.args[3]["error"]


@pytest.mark.asyncio
async def test_worker_uses_retry_after_for_rate_limit() -> None:
    worker = ReviewWorker()
    job_id = uuid.uuid4()
    job = {
        "id": job_id,
        "commit_sha": "sha123",
        "repo_full_name": "owner/repo",
        "pr_number": 1,
        "fence_token": 3,
        "attempt_count": 4,
        "payload": json.dumps({"installation_id": 123}),
    }
    response = httpx.Response(
        429,
        headers={"retry-after": "17"},
        request=httpx.Request("POST", "https://api.github.com/token"),
    )
    error = httpx.HTTPStatusError("rate limit", request=response.request, response=response)

    mock_github = MagicMock()
    mock_github.get_token = AsyncMock(side_effect=error)
    mock_github.close = AsyncMock()

    with (
        patch("app.worker.GitHubService", return_value=mock_github),
        patch("app.worker.db_service.release_job", new_callable=AsyncMock) as mock_release,
    ):
        await worker.process_job(job)

    mock_release.assert_called_once_with(job_id, 3, delay_seconds=17)


@pytest.mark.asyncio
async def test_github_review_filters_invalid_comments() -> None:
    github = GitHubService()
    existing_reviews = MagicMock()
    existing_reviews.json.return_value = []
    existing_reviews.raise_for_status = MagicMock()

    with (
        patch.object(github.client, "get", AsyncMock(return_value=existing_reviews)),
        patch.object(github, "_send_review_payload", new_callable=AsyncMock) as mock_send,
    ):
        await github.post_review(
            repo="owner/repo",
            pull_number=1,
            commit_id="sha123",
            token="token",
            body="summary",
            comments=[
                {"path": "", "line": 1, "body": "missing path"},
                {"path": "a.py", "line": 0, "body": "bad line"},
                {"path": "a.py", "line": 2, "side": "MIDDLE", "body": "valid enough"},
            ],
        )

    assert mock_send.call_count == 2
    sent_comment = mock_send.call_args_list[1].args[4][0]
    assert sent_comment == {
        "path": "a.py",
        "line": 2,
        "side": "RIGHT",
        "body": "valid enough",
    }
    await github.close()


@pytest.mark.asyncio
async def test_ai_agent_accepts_direct_json_response() -> None:
    ai_service = AIService()
    response = MagicMock()
    choice = MagicMock()
    choice.message.tool_calls = None
    choice.message.content = '{"summary": "ok", "score": 91, "comments": []}'
    response.choices = [choice]

    with patch("app.services.ai.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = response
        result = await ai_service._analyze_chunk_with_agent(
            "CodeReviewAgent",
            ai_service.CODE_REVIEW_AGENT_PROMPT,
            "FILE: a.py\nprint('ok')",
            "fix",
        )

    assert result is not None
    assert result.summary == "ok"
    assert result.score == 91


def test_ai_review_chunk_budget_prioritizes_risky_files() -> None:
    ai_service = AIService()
    pr_files = [
        {"filename": "docs/readme.md", "patch": "documentation"},
        {"filename": "src/components/Card.tsx", "patch": "@@ render tweaks"},
        {"filename": "src/app/api/auth/route.ts", "patch": "@@ token localStorage firebase"},
        {"filename": "public/logo.svg", "patch": "<svg />"},
    ]

    with patch("app.services.ai.settings.REVIEW_MAX_CHUNKS", 1):
        chunks = ai_service._build_review_chunks(pr_files)

    assert len(chunks) == 1
    assert "src/app/api/auth/route.ts" in chunks[0]
