"""Centralized environment variable loading and validation."""

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a helpful, respectful, and concise personal AI assistant."
)
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_HISTORY_LIMIT = 20


def normalize_async_db_url(url: str) -> str:
    """Normalize PostgreSQL / Neon URL for asyncpg and SQLAlchemy."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    # asyncpg expects 'ssl=require' instead of 'sslmode=require'
    if "sslmode=require" in url:
        url = url.replace("sslmode=require", "ssl=require")
    return url


class ConfigError(Exception):
    """Raised when environment variables are missing or invalid."""
    pass


@dataclass(frozen=True)
class Config:
    """Application configuration container."""

    telegram_bot_token: str
    allowed_telegram_user_ids: frozenset[int]
    gemini_api_key: str
    gemini_model: str
    gemini_system_instruction: str
    gemini_timeout_seconds: float
    mongodb_uri: str
    mongodb_db_name: str
    conversation_history_limit: int
    log_level: str
    environment: str
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/aiagent"
    qdrant_url: str = ":memory:"
    qdrant_api_key: str | None = None
    qdrant_collection_name: str = "aiagent_memories"
    embedding_model: str = "text-embedding-004"
    embedding_dimension: int = 768
    memory_top_k: int = 5
    memory_similarity_threshold: float = 0.65
    model_tier_small: str = "gemini-2.5-flash"
    model_tier_medium: str = "gemini-2.5-flash"
    model_tier_large: str = "gemini-1.5-pro"

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Config":
        """Load and validate configuration from environment variables.

        Args:
            env_file: Optional path to a specific .env file.

        Raises:
            ConfigError: If required configuration is missing or invalid.
        """
        if env_file is not None:
            load_dotenv(dotenv_path=env_file)
        else:
            load_dotenv()

        errors: list[str] = []

        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required and cannot be empty.")

        raw_allowed_users = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
        allowed_user_ids: set[int] = set()
        if not raw_allowed_users:
            errors.append(
                "ALLOWED_TELEGRAM_USER_IDS is required and cannot be empty (e.g. '123456789' or '12345,67890')."
            )
        else:
            for part in raw_allowed_users.split(","):
                part_clean = part.strip()
                if not part_clean:
                    continue
                try:
                    uid = int(part_clean)
                    if uid <= 0:
                        errors.append(f"ALLOWED_TELEGRAM_USER_IDS contains non-positive ID: '{part_clean}'.")
                    else:
                        allowed_user_ids.add(uid)
                except ValueError:
                    errors.append(f"ALLOWED_TELEGRAM_USER_IDS contains non-integer ID: '{part_clean}'.")

            if not allowed_user_ids and not errors:
                errors.append("ALLOWED_TELEGRAM_USER_IDS must contain at least one valid Telegram user ID.")

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not gemini_api_key:
            errors.append("GEMINI_API_KEY is required and cannot be empty.")

        gemini_model = os.getenv("GEMINI_MODEL", "").strip()
        if not gemini_model:
            errors.append("GEMINI_MODEL is required and cannot be empty (e.g. 'gemini-2.5-flash').")

        gemini_system_instruction = os.getenv(
            "GEMINI_SYSTEM_INSTRUCTION",
            DEFAULT_SYSTEM_INSTRUCTION,
        ).strip()
        if not gemini_system_instruction:
            gemini_system_instruction = DEFAULT_SYSTEM_INSTRUCTION

        raw_timeout = os.getenv("GEMINI_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
        try:
            gemini_timeout_seconds = float(raw_timeout)
            if gemini_timeout_seconds <= 0:
                errors.append("GEMINI_TIMEOUT_SECONDS must be a positive number.")
        except ValueError:
            errors.append(f"GEMINI_TIMEOUT_SECONDS must be a valid number, got '{raw_timeout}'.")
            gemini_timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017").strip()
        if not mongodb_uri:
            errors.append("MONGODB_URI cannot be empty.")

        mongodb_db_name = os.getenv("MONGODB_DB_NAME", "telegram_ai_bot").strip()
        if not mongodb_db_name:
            errors.append("MONGODB_DB_NAME cannot be empty.")

        raw_history_limit = os.getenv("CONVERSATION_HISTORY_LIMIT", str(DEFAULT_HISTORY_LIMIT)).strip()
        try:
            conversation_history_limit = int(raw_history_limit)
            if conversation_history_limit <= 0:
                errors.append("CONVERSATION_HISTORY_LIMIT must be a positive integer.")
        except ValueError:
            errors.append(f"CONVERSATION_HISTORY_LIMIT must be a valid integer, got '{raw_history_limit}'.")
            conversation_history_limit = DEFAULT_HISTORY_LIMIT

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in VALID_LOG_LEVELS:
            errors.append(
                f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)}, got '{log_level}'."
            )

        environment = os.getenv("ENVIRONMENT", "production").strip().lower()

        raw_db_url = os.getenv(
            "DATABASE_URL",
            os.getenv("POSTGRES_URI", "postgresql+asyncpg://postgres:postgres@localhost:5432/aiagent"),
        ).strip()
        database_url = normalize_async_db_url(raw_db_url)

        qdrant_url = os.getenv("QDRANT_URL", ":memory:").strip()
        qdrant_api_key = os.getenv("QDRANT_API_KEY", "").strip() or None
        qdrant_collection_name = os.getenv("QDRANT_COLLECTION_NAME", "aiagent_memories").strip()
        embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-004").strip()

        try:
            embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "768"))
        except ValueError:
            embedding_dimension = 768

        try:
            memory_top_k = int(os.getenv("MEMORY_TOP_K", "5"))
        except ValueError:
            memory_top_k = 5

        try:
            memory_similarity_threshold = float(os.getenv("MEMORY_SIMILARITY_THRESHOLD", "0.65"))
        except ValueError:
            memory_similarity_threshold = 0.65

        model_tier_small = os.getenv("MODEL_TIER_SMALL", "gemini-2.5-flash").strip()
        model_tier_medium = os.getenv("MODEL_TIER_MEDIUM", gemini_model).strip()
        model_tier_large = os.getenv("MODEL_TIER_LARGE", "gemini-1.5-pro").strip()

        if errors:
            formatted_errors = "\n  - " + "\n  - ".join(errors)
            raise ConfigError(f"Configuration validation failed:{formatted_errors}")

        return cls(
            telegram_bot_token=telegram_bot_token,
            allowed_telegram_user_ids=frozenset(allowed_user_ids),
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            gemini_system_instruction=gemini_system_instruction,
            gemini_timeout_seconds=gemini_timeout_seconds,
            mongodb_uri=mongodb_uri,
            mongodb_db_name=mongodb_db_name,
            conversation_history_limit=conversation_history_limit,
            log_level=log_level,
            environment=environment,
            database_url=database_url,
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            qdrant_collection_name=qdrant_collection_name,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            memory_top_k=memory_top_k,
            memory_similarity_threshold=memory_similarity_threshold,
            model_tier_small=model_tier_small,
            model_tier_medium=model_tier_medium,
            model_tier_large=model_tier_large,
        )
