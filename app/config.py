import base64
import logging
from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class AIProvider(str, Enum):
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # --- Project Settings ---
    PROJECT_NAME: str = "Revix"
    DEBUG: bool = False  # Secure default

    # --- AI Configuration ---
    AI_PROVIDER: AIProvider = AIProvider.GEMINI
    AI_MODEL_MAP: str = "openrouter/google/gemini-2.0-flash-lite:free"
    AI_MODEL_REDUCE: str = "openrouter/anthropic/claude-3.5-sonnet"

    # --- API Keys ---
    GEMINI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    OPENROUTER_API_KEY: str | None = None
    AI_API_KEY: str | None = None
    AI_API_BASE: str | None = None

    AI_MAX_TOKENS: int = 4000
    AI_TEMPERATURE: float = 0.0

    # --- GitHub App ---
    GITHUB_APP_ID: int
    GITHUB_WEBHOOK_SECRET: str
    GITHUB_APP_PRIVATE_KEY_B64: str
    GITHUB_BOT_NAME: str = "revix"

    # --- LLM Fallbacks ---
    AI_FALLBACK_MODELS: list[str] = Field(default_factory=list)

    # --- Database & Concurrency ---
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/revix",
        description="PostgreSQL connection string",
    )
    DB_POOL_MIN_SIZE: int = 10
    WORKER_CONCURRENCY: int = 5
    WORKER_SHUTDOWN_TIMEOUT: int = 30  # Matches K8s default grace period

    @model_validator(mode="after")
    def validate_setup(self) -> "Settings":
        """Ensures all required fields for the active provider are present."""
        errors = []

        # 1. AI Provider Validation
        required_keys = {
            AIProvider.GEMINI: self.GEMINI_API_KEY,
            AIProvider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            AIProvider.OPENAI: self.OPENAI_API_KEY,
            AIProvider.OPENROUTER: self.OPENROUTER_API_KEY,
            AIProvider.CUSTOM: self.AI_API_KEY,
        }

        if not required_keys.get(self.AI_PROVIDER):
            errors.append(
                f"❌ AI_PROVIDER is set to '{self.AI_PROVIDER.value}', but {self.AI_PROVIDER.name}_API_KEY is missing."
            )

        # 2. GitHub Validation
        if not self.GITHUB_APP_ID:
            errors.append("❌ GITHUB_APP_ID is missing.")
        if not self.GITHUB_WEBHOOK_SECRET:
            errors.append("❌ GITHUB_WEBHOOK_SECRET is missing.")

        # 3. Private Key Validation
        if not self.GITHUB_APP_PRIVATE_KEY_B64:
            errors.append("❌ GITHUB_APP_PRIVATE_KEY_B64 is missing.")
        else:
            try:
                base64.b64decode(self.GITHUB_APP_PRIVATE_KEY_B64).decode("utf-8")
            except Exception:
                errors.append("❌ GITHUB_APP_PRIVATE_KEY_B64 is not a valid base64 encoded string.")

        # 4. Auto-detect Docker environment for Database
        import os

        if os.path.exists("/.dockerenv") and "localhost" in self.DATABASE_URL:
            self.DATABASE_URL = self.DATABASE_URL.replace("localhost", "db")

        if errors:
            error_msg = "\n".join(errors)
            raise ValueError(f"Configuration Errors Found:\n{error_msg}")

        return self

    @property
    def db_pool_max_size(self) -> int:
        return (self.WORKER_CONCURRENCY * 2) + 5

    @property
    def github_app_private_key(self) -> str:
        """Reads the key from the B64 env var."""
        return base64.b64decode(self.GITHUB_APP_PRIVATE_KEY_B64).decode("utf-8")

    @property
    def active_api_key(self) -> str | None:
        keys = {
            AIProvider.GEMINI: self.GEMINI_API_KEY,
            AIProvider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            AIProvider.OPENAI: self.OPENAI_API_KEY,
            AIProvider.OPENROUTER: self.OPENROUTER_API_KEY,
        }
        return keys.get(self.AI_PROVIDER) or self.AI_API_KEY


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except Exception as e:
        # For CLI usage, we want a clean exit with a helpful message
        print(f"\n🛑 CONFIGURATION ERROR:\n{e}\n")
        print("👉 Run 'make setup' or check your .env file.\n")
        raise SystemExit(1) from e


settings = get_settings()

if __name__ == "__main__":
    # If run directly, validate and print status
    print(f"✅ Configuration Validated for project: {settings.PROJECT_NAME}")
    print(f"🤖 AI Provider: {settings.AI_PROVIDER.value}")
    print(f"📦 Database: {settings.DATABASE_URL.split('@')[-1]}")  # Hide credentials
    print("🔑 GitHub Key: Base64 key loaded successfully")
