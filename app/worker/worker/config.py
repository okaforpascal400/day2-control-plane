from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. Secrets arrive via env only (never baked into the image)."""

    model_config = SettingsConfigDict(env_prefix="DAY2_", extra="ignore")

    database_url: str = Field(
        default="postgresql+asyncpg://day2:day2@localhost:5432/day2"
    )
    log_level: str = Field(default="INFO")
    service_name: str = Field(default="worker")

    # How many pending jobs one poll claims, and how long to wait when there are none.
    batch_size: int = Field(default=10, ge=1, le=100)
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    # Stand-in for real work until Phase 3 gives us something worth measuring.
    work_duration_seconds: float = Field(default=0.25, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    # The api owns schema creation; on a cold start the worker waits it out.
    schema_wait_seconds: float = Field(default=120.0, gt=0)

    # Touched every poll; the container healthcheck reads its mtime.
    heartbeat_path: str = Field(default="/tmp/worker-heartbeat")
    heartbeat_max_age_seconds: int = Field(default=30, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
