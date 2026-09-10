import pytest

from app.config import Settings


def test_custom_models_preserved_without_alteration() -> None:
    """Verifies any model string is preserved exactly as configured (no silent rewrites)."""
    settings = Settings(
        AI_API_KEY="dummy",
        GITHUB_APP_ID=123,
        GITHUB_WEBHOOK_SECRET="secret",
        GITHUB_APP_PRIVATE_KEY_B64="ZHVtbXk=",
        AI_MODEL_MAP="openrouter/anthropic/claude-3.7-sonnet",
        AI_MODEL_REDUCE="openai/gpt-4o",
    )

    assert settings.AI_MODEL_MAP == "openrouter/anthropic/claude-3.7-sonnet"
    assert settings.AI_MODEL_REDUCE == "openai/gpt-4o"


def test_private_key_loading_from_raw_pem() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEA1+gtPv0\n-----END RSA PRIVATE KEY-----"
    settings = Settings(
        AI_API_KEY="dummy",
        GITHUB_APP_ID=123,
        GITHUB_WEBHOOK_SECRET="secret",
        GITHUB_APP_PRIVATE_KEY=pem,
    )

    assert settings.github_app_private_key == pem


def test_private_key_loading_from_b64() -> None:
    # "hello-rsa-key" base64 is "aGVsbG8tcnNhLWtleQ=="
    settings = Settings(
        AI_API_KEY="dummy",
        GITHUB_APP_ID=123,
        GITHUB_WEBHOOK_SECRET="secret",
        GITHUB_APP_PRIVATE_KEY_PATH="",
        GITHUB_APP_PRIVATE_KEY_B64="aGVsbG8tcnNhLWtleQ==",
    )

    assert settings.github_app_private_key == "hello-rsa-key"


def test_private_key_loading_from_file(tmp_path) -> None:
    key_file = tmp_path / "test_key.pem"
    key_file.write_text("test-file-key")
    settings = Settings(
        AI_API_KEY="dummy",
        GITHUB_APP_ID=123,
        GITHUB_WEBHOOK_SECRET="secret",
        GITHUB_APP_PRIVATE_KEY_PATH=str(key_file),
    )
    assert settings.github_app_private_key == "test-file-key"


def test_validation_fails_on_missing_required_keys() -> None:
    with pytest.raises(ValueError, match="Configuration Errors Found"):
        Settings(
            AI_API_KEY="",
            GITHUB_APP_ID=0,
            GITHUB_WEBHOOK_SECRET="",
            GITHUB_APP_PRIVATE_KEY_B64="",
            GITHUB_APP_PRIVATE_KEY="",
            GITHUB_APP_PRIVATE_KEY_PATH="nonexistent.pem",
        )


def test_db_pool_max_size_scaling() -> None:
    settings = Settings(
        AI_API_KEY="dummy",
        GITHUB_APP_ID=123,
        GITHUB_WEBHOOK_SECRET="secret",
        GITHUB_APP_PRIVATE_KEY_B64="ZHVtbXk=",
        WORKER_CONCURRENCY=10,
        DB_POOL_MIN_SIZE=5,
    )
    # (10 * 2) + 5 = 25
    assert settings.db_pool_max_size == 25
