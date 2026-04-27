from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import AIService, ReviewResult


@pytest.fixture
def ai_service() -> AIService:
    return AIService()


@pytest.mark.asyncio
async def test_analyze_diff_single_chunk(ai_service: AIService) -> None:
    # Mock chunking to return one chunk
    with patch.object(ai_service, "_chunk_by_ast", return_value=["chunk1"]):
        # Mock _analyze_chunk to return a ReviewResult object
        mock_result = ReviewResult(summary="all good", risk_level="LOW", comments=[])
        with patch.object(ai_service, "_analyze_chunk", new_callable=AsyncMock) as mock_analyze:
            mock_analyze.return_value = mock_result

            result = await ai_service.analyze_diff(
                "some diff", "owner/repo", [{"filename": "f.py", "patch": "diff"}]
            )

            assert isinstance(result, ReviewResult)
            assert result.summary == "all good"
            assert result.risk_level == "LOW"
            mock_analyze.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_diff_multi_chunk(ai_service: AIService) -> None:
    # Mock chunking to return two chunks
    with patch.object(ai_service, "_chunk_by_ast", return_value=["chunk1", "chunk2"]):
        # Mock _analyze_chunk
        mock_res1 = ReviewResult(summary="s1", risk_level="LOW", comments=[])
        mock_res2 = ReviewResult(summary="s2", risk_level="HIGH", comments=[])

        with patch.object(ai_service, "_analyze_chunk", new_callable=AsyncMock) as mock_analyze:
            mock_analyze.side_effect = [mock_res1, mock_res2]

            # Mock _reduce_summaries
            mock_reduce_res = ReviewResult(summary="global summary", risk_level="HIGH", comments=[])
            with patch.object(
                ai_service, "_reduce_summaries", new_callable=AsyncMock
            ) as mock_reduce:
                mock_reduce.return_value = mock_reduce_res

                result = await ai_service.analyze_diff(
                    "large diff", "owner/repo", [{"filename": "f.py", "patch": "diff"}]
                )

                assert result.summary == "global summary"
                assert result.risk_level == "HIGH"
                assert mock_analyze.call_count == 2
                mock_reduce.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_chunk_tool_use(ai_service: AIService) -> None:
    # This tests the actual interaction with LiteLLM (mocked)
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_tool_call = MagicMock()

    mock_tool_call.function.name = "submit_review"
    mock_tool_call.function.arguments = '{"summary": "tool output", "risk_level": "MEDIUM", "comments": [{"path": "f.py", "line": 1, "body": "fix me"}]}'
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response.choices = [mock_choice]

    with patch("app.services.ai.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = mock_response

        result = await ai_service._analyze_chunk("diff", 0, "repo context")

        assert isinstance(result, ReviewResult)
        assert result.summary == "tool output"
        mock_acompletion.assert_called_once()
        # Check if tool choice was forced
        assert mock_acompletion.call_args.kwargs["tool_choice"] == "required"
