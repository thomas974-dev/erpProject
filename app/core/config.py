from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ErpFailureMode = Literal[
    "normal",
    "http_503",
    "http_429",
    "http_400",
    "timeout_before_create",
    "timeout_after_create",
]


class Settings(BaseSettings):
    """Application settings, read from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://orders:orders@127.0.0.1:5432/orders"
    celery_broker_url: str = "redis://127.0.0.1:6379/0"

    erp_base_url: str = "http://127.0.0.1:8001"
    erp_http_timeout_seconds: float = Field(default=2.0, gt=0)
    erp_max_attempts: int = Field(default=3, ge=1)
    # Exponential backoff with full jitter between automatic attempts.
    erp_retry_base_delay_seconds: float = Field(default=1.0, ge=0)
    erp_retry_max_delay_seconds: float = Field(default=30.0, ge=0)

    # Mock ERP only (development/testing).
    mock_erp_failure_mode: ErpFailureMode = "normal"
    mock_erp_timeout_delay_seconds: float = Field(default=5.0, ge=0)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide Settings instance (cached; usable as a FastAPI dependency).

    Milestone 1: configuration from `.env`, shared by the API, the worker and the mock ERP.
    """
    return Settings()
