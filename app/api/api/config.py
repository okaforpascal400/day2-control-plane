from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. Secrets arrive via env only (never baked into the image)."""

    model_config = SettingsConfigDict(env_prefix="DAY2_", extra="ignore")

    # postgresql+asyncpg://user:password@host:5432/dbname
    database_url: str = Field(
        default="postgresql+asyncpg://day2:day2@localhost:5432/day2"
    )
    log_level: str = Field(default="INFO")
    service_name: str = Field(default="api")
    # Set false when a migration job owns the schema (Phase 2+).
    create_schema_on_start: bool = Field(default=True)
    # How long startup waits for Postgres before giving up and failing the pod.
    database_wait_seconds: float = Field(default=120.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
