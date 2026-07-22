"""Async engine/session helpers plus the Phase 1 schema bootstrap.

Phase 1 has no migration tool yet: both services call `create_schema()` on start,
which is idempotent and safe to race between the api and worker pods.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from day2_shared.models import Base

logger = logging.getLogger(__name__)


def make_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        dsn,
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def wait_for_database(
    engine: AsyncEngine,
    *,
    timeout_seconds: float = 120.0,
    interval_seconds: float = 2.0,
) -> None:
    """Block until Postgres accepts a connection.

    Pod start order is not guaranteed, so a service that dies when the database
    is not up yet turns an ordinary rollout into a CrashLoopBackOff. Waiting is
    both quieter and faster than restarting into exponential backoff.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            async with engine.connect() as conn:
                await conn.scalar(text("SELECT 1"))
            if attempt > 1:
                logger.info("database available", extra={"attempt": attempt})
            return
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"

        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"timed out waiting for database: {reason}")
        logger.info("waiting for database", extra={"attempt": attempt, "reason": reason})
        await asyncio.sleep(interval_seconds)


async def wait_for_schema(
    engine: AsyncEngine,
    table: str,
    *,
    timeout_seconds: float = 120.0,
    interval_seconds: float = 2.0,
) -> None:
    """Block until `table` exists.

    The api owns schema creation, so on a cold start the worker can come up first.
    Waiting here keeps a normal rollout quiet instead of emitting a stack trace on
    every poll until the api catches up.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            async with engine.connect() as conn:
                found = await conn.scalar(text("SELECT to_regclass(:t)"), {"t": table})
            if found is not None:
                if attempt > 1:
                    logger.info("schema available", extra={"table": table})
                return
            reason = f"table {table!r} not created yet"
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"

        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"timed out waiting for schema: {reason}")
        logger.info(
            "waiting for schema",
            extra={"table": table, "attempt": attempt, "reason": reason},
        )
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Commit-on-success, rollback-on-error session."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
