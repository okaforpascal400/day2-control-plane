from __future__ import annotations

import pytest
from sqlalchemy import text

from day2_shared.db import make_engine, wait_for_schema
from worker.config import Settings


async def test_returns_immediately_when_table_exists(factory, settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    try:
        await wait_for_schema(engine, "jobs", timeout_seconds=5, interval_seconds=0.1)
    finally:
        await engine.dispose()


async def test_times_out_when_table_never_appears(factory, settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    try:
        with pytest.raises(TimeoutError, match="not created yet"):
            await wait_for_schema(
                engine, "never_created", timeout_seconds=0.3, interval_seconds=0.1
            )
    finally:
        await engine.dispose()


async def test_times_out_when_database_is_unreachable() -> None:
    engine = make_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nope")
    try:
        with pytest.raises(TimeoutError) as excinfo:
            await wait_for_schema(
                engine, "jobs", timeout_seconds=0.3, interval_seconds=0.1
            )
        assert "not created yet" not in str(excinfo.value)
    finally:
        await engine.dispose()


async def test_sees_a_table_created_after_the_wait_started(
    factory, settings: Settings
) -> None:
    engine = make_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS late_arrival"))
            await conn.execute(text("CREATE TABLE late_arrival (id int)"))
        await wait_for_schema(
            engine, "late_arrival", timeout_seconds=5, interval_seconds=0.1
        )
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE late_arrival"))
    finally:
        await engine.dispose()
