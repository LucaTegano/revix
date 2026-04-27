from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai import AIService, ReviewComment


@pytest.fixture
def ai_service() -> AIService:
    return AIService()


@pytest.mark.asyncio
async def test_reduce_summaries_success(ai_service: AIService) -> None:
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_tool_call = MagicMock()

    mock_tool_call.function.name = "synthesize_review"
    mock_tool_call.function.arguments = (
        '{"global_summary": "Synthesized summary", "global_risk_level": "HIGH"}'
    )
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_response.choices = [mock_choice]

    with patch("app.services.ai.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = mock_response

        summaries = ["Risk: LOW | Summary: s1", "Risk: HIGH | Summary: s2"]
        comments = [ReviewComment(path="a.py", line=1, body="b")]

        result = await ai_service._reduce_summaries(summaries, comments)

        assert result.summary == "Synthesized summary"
        assert result.risk_level == "HIGH"
        assert result.comments == comments

        # Verify the prompt includes the summaries
        call_kwargs = mock_acompletion.call_args.kwargs
        assert "Synthesize these summaries" in call_kwargs["messages"][0]["content"]
        assert "Risk: LOW | Summary: s1" in call_kwargs["messages"][1]["content"]
        assert "Risk: HIGH | Summary: s2" in call_kwargs["messages"][1]["content"]
