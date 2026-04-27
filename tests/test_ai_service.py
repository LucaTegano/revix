import pytest

from app.services.ai import AIService


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
