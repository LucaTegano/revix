import base64
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # --- Project Settings ---
    PROJECT_NAME: str = "Revix"
    DEBUG: bool = False

    # --- AI Configuration (Provider-Agnostic, LiteLLM Standard) ---
    AI_MODEL_MAP: str = "openrouter/google/gemini-3.1-flash-lite"
    AI_MODEL_REDUCE: str = "openrouter/google/gemini-3.1-flash-lite"

    AI_API_KEY: str | None = None
    OPENROUTER_API_KEY: str | None = None
    AI_API_BASE: str | None = None
    AI_MAX_TOKENS: int = 2048
    AI_TEMPERATURE: float = 0.0
    AI_CONCURRENCY: int = 5

    # --- Review Budget & Noise Controls ---
    REVIEW_PROFILE: str = "chill"
    REVIEW_MAX_CHUNKS: int = 12
    REVIEW_MAX_AGENTS_PER_CHUNK: int = 2
    REVIEW_MAX_INLINE_COMMENTS: int = 3
    REVIEW_MIN_INLINE_SEVERITY: str = "WARNING"

    # --- GitHub App Authentication ---
    GITHUB_APP_ID: int = 0
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_APP_PRIVATE_KEY_PATH: str | None = None
    GITHUB_APP_PRIVATE_KEY_B64: str | None = None
    GITHUB_APP_PRIVATE_KEY: str | None = None
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
    WORKER_SHUTDOWN_TIMEOUT: int = 30

    # --- Recovery Tuning ---
    WORKER_HEARTBEAT_INTERVAL_SECONDS: int = 30
    WORKER_HEARTBEAT_STALE_SECONDS: int = 90
    WORKER_RECONCILE_INTERVAL_SECONDS: int = 60
    WORKER_RETRY_BACKOFF_SECONDS: int = 120

    @staticmethod
    def _normalize_pem(key_text: str) -> str:
        return key_text.replace("\\n", "\n").strip('"').strip("'").strip()

    @property
    def github_app_private_key(self) -> str:
        """Resolves the RSA private key from PEM string, file path, or Base64."""
        if self.GITHUB_APP_PRIVATE_KEY:
            return self._normalize_pem(self.GITHUB_APP_PRIVATE_KEY)

        if self.GITHUB_APP_PRIVATE_KEY_PATH and self.GITHUB_APP_PRIVATE_KEY_PATH.strip():
            path = Path(self.GITHUB_APP_PRIVATE_KEY_PATH.strip())
            if path.is_file():
                return self._normalize_pem(path.read_text(encoding="utf-8"))
            raise ValueError(
                f"GitHub private key file not found at: {self.GITHUB_APP_PRIVATE_KEY_PATH}"
            )

        if self.GITHUB_APP_PRIVATE_KEY_B64:
            try:
                decoded = base64.b64decode(self.GITHUB_APP_PRIVATE_KEY_B64).decode("utf-8")
                return self._normalize_pem(decoded)
            except Exception as e:
                raise ValueError(f"Invalid Base64 in GITHUB_APP_PRIVATE_KEY_B64: {e}") from e

        default_pem = Path("github_private_key.pem")
        if default_pem.is_file():
            return self._normalize_pem(default_pem.read_text(encoding="utf-8"))

        raise ValueError(
            "GitHub App Private Key is missing. Set GITHUB_APP_PRIVATE_KEY_PATH, "
            "GITHUB_APP_PRIVATE_KEY_B64, GITHUB_APP_PRIVATE_KEY, or place github_private_key.pem in the project root."
        )

    @model_validator(mode="after")
    def validate_setup(self) -> "Settings":
        """Ensures all required fields for production are present."""
        errors = []

        if not self.AI_API_KEY and self.OPENROUTER_API_KEY:
            self.AI_API_KEY = self.OPENROUTER_API_KEY

        # LiteLLM requires openrouter/ prefix to route to OpenRouter gateway
        if self.AI_MODEL_MAP and self.AI_MODEL_MAP.startswith("minimax/"):
            self.AI_MODEL_MAP = f"openrouter/{self.AI_MODEL_MAP}"
        if self.AI_MODEL_REDUCE and self.AI_MODEL_REDUCE.startswith("minimax/"):
            self.AI_MODEL_REDUCE = f"openrouter/{self.AI_MODEL_REDUCE}"

        # OpenRouter transitioned minimax-m3 from :free to paid slug minimax/minimax-m3
        if self.AI_MODEL_MAP and "minimax-m3:free" in self.AI_MODEL_MAP:
            self.AI_MODEL_MAP = self.AI_MODEL_MAP.replace(":free", "")
        if self.AI_MODEL_REDUCE and "minimax-m3:free" in self.AI_MODEL_REDUCE:
            self.AI_MODEL_REDUCE = self.AI_MODEL_REDUCE.replace(":free", "")

        if not self.AI_API_KEY:
            errors.append("❌ AI_API_KEY is missing.")

        if not self.GITHUB_APP_ID:
            errors.append("❌ GITHUB_APP_ID is missing.")

        if not self.GITHUB_WEBHOOK_SECRET:
            errors.append("❌ GITHUB_WEBHOOK_SECRET is missing.")

        try:
            _ = self.github_app_private_key
        except ValueError as e:
            errors.append(f"❌ {e}")

        if errors:
            raise ValueError("Configuration Errors Found:\n" + "\n".join(errors))

        return self

    @property
    def db_pool_max_size(self) -> int:
        calculated_max = (self.WORKER_CONCURRENCY * 2) + 5
        return max(calculated_max, self.DB_POOL_MIN_SIZE)


@lru_cache
def get_settings() -> Settings:
    return Settings()


try:
    settings = get_settings()
except Exception as e:
    logger.warning("Settings loaded with partial or invalid configuration: %s", e)
    # Provide a non-validating fallback for module-level imports in test/tool environments
    settings = Settings.model_construct()

if __name__ == "__main__":
    validated = get_settings()
    print(f"✅ Configuration Validated for project: {validated.PROJECT_NAME}")
    print(f"🤖 Active Map Model:    {validated.AI_MODEL_MAP}")
    print(f"🤖 Active Reduce Model: {validated.AI_MODEL_REDUCE}")
    print(f"📦 Database:            {validated.DATABASE_URL.split('@')[-1]}")
    print("🔑 GitHub Private Key:  Successfully loaded and validated.")
