from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import AIService, ReviewResult


@pytest.fixture
def ai_service() -> AIService:
    return AIService()


@pytest.mark.asyncio
async def test_coordinate_routing(ai_service: AIService) -> None:
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"agents": ["SecurityAgent", "ReviewAgent"]}'

    with patch("app.services.ai.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = mock_response

        agents = await ai_service._coordinate_routing("chunk content", "pr intent")

        assert set(agents) == {"SecurityAgent", "ReviewAgent"}
        mock_acompletion.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_diff_swarm_execution(ai_service: AIService) -> None:
    # Mock dependencies
    with (
        patch.object(ai_service, "_chunk_by_ast", return_value=["chunk1"]),
        patch.object(
            ai_service, "_coordinate_routing", new_callable=AsyncMock, return_value=["ReviewAgent"]
        ),
        patch.object(ai_service, "_execute_agent", new_callable=AsyncMock) as mock_execute,
        patch.object(ai_service, "_reduce_summaries", new_callable=AsyncMock) as mock_reduce,
    ):
        mock_execute.return_value = ReviewResult(summary="agent res", score=80, comments=[])
        mock_reduce.return_value = ReviewResult(summary="final", score=80, comments=[])

        pr_details = {"title": "feat: test", "body": "testing swarm"}
        result = await ai_service.analyze_diff(
            diff="diff",
            repo_full_name="owner/repo",
            pr_files=[{"filename": "test.py", "content": "print('hello')"}],
            pr_details=pr_details,
        )

        assert result.summary == "final"
        mock_execute.assert_called_once()
        mock_reduce.assert_called_once()


@pytest.mark.asyncio
async def test_run_in_sandbox_mock(ai_service: AIService) -> None:
    # Mock subprocess execution
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"output", b"error")
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await ai_service._run_in_sandbox("print('hello')")

        assert result["stdout"] == "output"
        assert result["exit_code"] == 0
        mock_exec.assert_called_once()
        # Verify docker run command includes gVisor runtime
        args = mock_exec.call_args[0]
        assert "--runtime=runsc" in args


@pytest.mark.asyncio
async def test_analyze_chunk_with_agent_sandbox_flow(ai_service: AIService) -> None:
    # Mock LiteLLM responses for VerificationAgent
    mock_response_1 = MagicMock()
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_1"
    mock_tool_call.function.name = "run_in_sandbox"
    mock_tool_call.function.arguments = '{"script": "print(1)"}'
    mock_response_1.choices = [MagicMock()]
    mock_response_1.choices[0].message.tool_calls = [mock_tool_call]

    mock_response_2 = MagicMock()
    mock_tool_call_2 = MagicMock()
    mock_tool_call_2.function.name = "submit_review"
    mock_tool_call_2.function.arguments = '{"summary": "verified", "score": 100, "comments": []}'
    mock_response_2.choices = [MagicMock()]
    mock_response_2.choices[0].message.tool_calls = [mock_tool_call_2]

    with (
        patch("app.services.ai.acompletion", new_callable=AsyncMock) as mock_acompletion,
        patch.object(ai_service, "_run_in_sandbox", new_callable=AsyncMock) as mock_sandbox,
    ):
        mock_acompletion.side_effect = [mock_response_1, mock_response_2]
        mock_sandbox.return_value = {"stdout": "1", "exit_code": 0}

        result = await ai_service._analyze_chunk_with_agent(
            "VerificationAgent", "system prompt", "chunk", "intent"
        )

        assert result.summary == "verified"
        assert mock_sandbox.called
        assert mock_acompletion.call_count == 2
