from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from day2_shared.db import create_schema, make_engine, make_sessionmaker
from day2_shared.models import Base
from worker.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "DAY2_TEST_DATABASE_URL",
    "postgresql+asyncpg://day2:day2@localhost:5432/day2_test",
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        service_name="worker-test",
        work_duration_seconds=0.0,
        poll_interval_seconds=0.05,
        heartbeat_path=str(tmp_path / "heartbeat"),
    )


@pytest_asyncio.fixture
async def factory(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(settings.database_url)
    await create_schema(engine)
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()
