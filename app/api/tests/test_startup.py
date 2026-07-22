from __future__ import annotations

import pytest

from api.config import Settings
from api.main import create_app
from day2_shared.db import make_engine, wait_for_database
from tests.conftest import TEST_DATABASE_URL


async def test_wait_for_database_returns_when_reachable() -> None:
    engine = make_engine(TEST_DATABASE_URL)
    try:
        await wait_for_database(engine, timeout_seconds=5, interval_seconds=0.1)
    finally:
        await engine.dispose()


async def test_wait_for_database_times_out_when_unreachable() -> None:
    engine = make_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope")
    try:
        with pytest.raises(TimeoutError, match="timed out waiting for database"):
            await wait_for_database(engine, timeout_seconds=0.3, interval_seconds=0.1)
    finally:
        await engine.dispose()


async def test_startup_fails_loudly_if_database_never_arrives() -> None:
    """Pods should wait, but not forever — an unreachable DB must fail the pod."""
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope",
        service_name="api-test",
        create_schema_on_start=True,
        database_wait_seconds=0.3,
    )
    app = create_app(settings)
    with pytest.raises(TimeoutError):
        async with app.router.lifespan_context(app):
            pass
