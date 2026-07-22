from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from api.config import Settings
from api.main import create_app
from day2_shared.models import Base

TEST_DATABASE_URL = os.environ.get(
    "DAY2_TEST_DATABASE_URL",
    "postgresql+asyncpg://day2:day2@localhost:5432/day2_test",
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        service_name="api-test",
        create_schema_on_start=True,
    )


@pytest_asyncio.fixture
async def app_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """App with a live schema; tables are truncated per test for isolation."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        async with app.state.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield client
