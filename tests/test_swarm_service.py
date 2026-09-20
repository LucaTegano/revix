from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import AIService, ReviewComment, ReviewResult


@pytest.fixture
def ai_service() -> AIService:
    return AIService()


@pytest.mark.asyncio
async def test_analyze_diff_uses_single_agent_per_chunk(ai_service: AIService) -> None:
    with (
        patch.object(ai_service, "_build_review_chunks", return_value=["chunk1", "chunk2"]),
        patch.object(ai_service, "_execute_review_agent", new_callable=AsyncMock) as execute,
    ):
        execute.side_effect = [
            ReviewResult(summary="one", score=90, comments=[]),
            ReviewResult(summary="two", score=80, comments=[]),
        ]
        result = await ai_service.analyze_diff(
            diff="diff",
            repo_full_name="owner/repo",
            pr_files=[{"filename": "test.py", "content": "print('hello')"}],
            pr_details={"title": "feat: test", "body": "testing"},
        )

    assert execute.call_count == 2
    assert result.score == 80
    assert result.summary == "No actionable issues found."


def test_merge_results_deduplicates_and_sorts(ai_service: AIService) -> None:
    duplicate = ReviewComment(
        path="a.py", line=4, body="same issue", severity="WARNING", agent_id="CodeReviewAgent"
    )
    critical = ReviewComment(
        path="b.py", line=2, body="critical issue", severity="CRITICAL", agent_id="CodeReviewAgent"
    )
    result = ai_service._merge_results(
        [
            ReviewResult(summary="a", score=85, comments=[duplicate]),
            ReviewResult(summary="b", score=70, comments=[duplicate, critical]),
        ]
    )
    assert result.score == 70
    assert len(result.comments) == 2
    assert result.comments[0].severity == "CRITICAL"
    assert "2 actionable issue" in result.summary


@pytest.mark.asyncio
async def test_run_in_sandbox_mock(ai_service: AIService) -> None:
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"output", b"error")
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await ai_service._run_in_sandbox("print('hello')")

    assert result["stdout"] == "output"
    assert result["exit_code"] == 0
    assert "--runtime=runsc" in mock_exec.call_args[0]


@pytest.mark.asyncio
async def test_unified_agent_can_use_sandbox(ai_service: AIService) -> None:
    first = MagicMock()
    sandbox_call = MagicMock()
    sandbox_call.id = "call_1"
    sandbox_call.function.name = "run_in_sandbox"
    sandbox_call.function.arguments = '{"script": "print(1)"}'
    first.choices = [MagicMock()]
    first.choices[0].message.tool_calls = [sandbox_call]

    second = MagicMock()
    submit_call = MagicMock()
    submit_call.function.name = "submit_review"
    submit_call.function.arguments = '{"summary":"verified","score":100,"comments":[]}'
    second.choices = [MagicMock()]
    second.choices[0].message.tool_calls = [submit_call]

    with (
        patch("app.services.ai.acompletion", new_callable=AsyncMock) as completion,
        patch.object(ai_service, "_run_in_sandbox", new_callable=AsyncMock) as sandbox,
    ):
        completion.side_effect = [first, second]
        sandbox.return_value = {"stdout": "1", "exit_code": 0}
        result = await ai_service._execute_review_agent("chunk", "intent")

    assert result is not None
    assert result.summary == "verified"
    assert sandbox.called
    assert completion.call_count == 2
