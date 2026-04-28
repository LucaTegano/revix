from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import AIService, ReviewComment, ReviewResult


@pytest.fixture
def ai_service() -> AIService:
    return AIService()


def test_chunk_by_ast_large_file(ai_service: AIService) -> None:
    # Create a large file content
    lines = [f"def func{i}():\n    pass" for i in range(100)]
    large_file_content = "\n".join(lines)

    # Set limit to small value to force splitting
    ai_service.MAX_CHUNK_TOKENS = 50
    chunks = ai_service._chunk_by_ast("large.py", large_file_content)

    # Should have multiple chunks
    assert len(chunks) > 1


@pytest.mark.asyncio
async def test_reduce_summaries_success(ai_service: AIService) -> None:
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_tool_call = MagicMock()

    mock_tool_call.function.name = "synthesize_review"
    mock_tool_call.function.arguments = (
        '{"global_summary": "Synthesized summary", "global_score": 85}'
    )
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response.choices = [mock_choice]

    with patch("app.services.ai.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = mock_response

        summaries = ["Score: 80 | Summary: s1", "Score: 90 | Summary: s2"]
        comments = [ReviewComment(path="a.py", line=1, body="b", side="RIGHT", severity="INFO")]

        result = await ai_service._reduce_summaries(summaries, comments)

        assert result.summary == "Synthesized summary"
        assert result.score == 85
        assert result.comments == comments

        # Verify the prompt includes the summaries
        call_kwargs = mock_acompletion.call_args.kwargs
        assert "Synthesize these summaries" in call_kwargs["messages"][0]["content"]
        assert "Score: 80 | Summary: s1" in call_kwargs["messages"][1]["content"]
        assert "Score: 90 | Summary: s2" in call_kwargs["messages"][1]["content"]


@pytest.mark.asyncio
async def test_analyze_diff_swarm_flow(ai_service: AIService) -> None:
    # Mock chunking
    with patch.object(ai_service, "_chunk_by_ast", return_value=["chunk1"]):
        # Mock coordinator
        with patch.object(ai_service, "_coordinate_routing", new_callable=AsyncMock) as mock_route:
            mock_route.return_value = ["ReviewAgent"]

            # Mock agent execution
            mock_result = ReviewResult(summary="swarm logic ok", score=90, comments=[])
            with patch.object(ai_service, "_execute_agent", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = mock_result

                # Mock reduction
                with patch.object(
                    ai_service, "_reduce_summaries", new_callable=AsyncMock
                ) as mock_reduce:
                    mock_reduce.return_value = mock_result

                    pr_details = {"title": "fix: bug", "body": "description"}
                    result = await ai_service.analyze_diff(
                        diff="diff",
                        repo_full_name="owner/repo",
                        pr_files=[{"filename": "a.py", "patch": "@@ -1 +1 @@"}],
                        pr_details=pr_details,
                    )

                    assert result.summary == "swarm logic ok"
                    assert result.score == 90
                    mock_route.assert_called_once()
                    mock_exec.assert_called_once()
                    mock_reduce.assert_called_once()

