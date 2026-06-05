from app.config import Settings


def test_deprecated_openrouter_models_are_normalized() -> None:
    settings = Settings(
        AI_API_KEY="dummy",
        GITHUB_APP_ID=123,
        GITHUB_WEBHOOK_SECRET="secret",
        GITHUB_APP_PRIVATE_KEY_B64="ZHVtbXk=",
        AI_MODEL_MAP="openrouter/google/gemini-2.0-flash-lite:free",
        AI_MODEL_REDUCE="openrouter/anthropic/claude-3.5-sonnet",
    )

    assert settings.AI_MODEL_MAP == "openrouter/google/gemini-3.1-flash-lite"
    assert settings.AI_MODEL_REDUCE == "openrouter/google/gemini-3.1-flash-lite"
