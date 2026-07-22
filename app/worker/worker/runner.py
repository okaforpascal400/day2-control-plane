"""Poll the jobs table, claim pending work, run it, record the outcome.

Claiming uses `FOR UPDATE SKIP LOCKED` so the deployment can be scaled past one
replica without two workers ever picking up the same job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from day2_shared.db import session_scope
from day2_shared.models import Job, JobStatus
from worker.config import Settings

logger = logging.getLogger(__name__)


async def claim_jobs(session: AsyncSession, batch_size: int) -> list[Job]:
    """Atomically move up to `batch_size` pending jobs into `processing`."""
    result = await session.execute(
        select(Job)
        .where(Job.status == JobStatus.pending)
        .order_by(Job.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars().all())

    now = datetime.now(UTC)
    for job in jobs:
        job.status = JobStatus.processing
        job.started_at = now
        job.attempts += 1
        logger.info(
            "job claimed",
            extra={"job_id": job.id, "kind": job.kind, "attempt": job.attempts},
        )
    await session.flush()
    return jobs


async def perform_work(job: Job, duration_seconds: float) -> None:
    """Placeholder for real work; kept awaitable so tests can substitute failures."""
    await asyncio.sleep(duration_seconds)


async def run_job(session: AsyncSession, job: Job, settings: Settings) -> JobStatus:
    started = time.perf_counter()
    try:
        await perform_work(job, settings.work_duration_seconds)
    except Exception as exc:
        job.last_error = f"{type(exc).__name__}: {exc}"
        exhausted = job.attempts >= settings.max_attempts
        job.status = JobStatus.failed if exhausted else JobStatus.pending
        job.finished_at = datetime.now(UTC) if exhausted else None
        logger.warning(
            "job failed",
            extra={
                "job_id": job.id,
                "attempt": job.attempts,
                "will_retry": not exhausted,
                "error": job.last_error,
            },
        )
    else:
        job.status = JobStatus.completed
        job.finished_at = datetime.now(UTC)
        job.last_error = None
        logger.info(
            "job completed",
            extra={
                "job_id": job.id,
                "kind": job.kind,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
    await session.flush()
    return job.status


async def process_batch(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """One poll cycle. Returns the number of jobs handled."""
    async with session_scope(factory) as session:
        jobs = await claim_jobs(session, settings.batch_size)
        for job in jobs:
            await run_job(session, job, settings)
        return len(jobs)


def touch_heartbeat(path: str) -> None:
    with open(path, "w") as handle:
        handle.write(str(int(time.time())))


def heartbeat_is_fresh(path: str, max_age_seconds: int) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) <= max_age_seconds
    except OSError:
        return False


async def run_forever(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    logger.info("worker polling", extra={"interval_s": settings.poll_interval_seconds})
    while not stop.is_set():
        try:
            handled = await process_batch(factory, settings)
            touch_heartbeat(settings.heartbeat_path)
        except Exception:
            handled = 0
            logger.exception("poll cycle failed")

        if handled == 0:
            # Idle: wait out the interval, but wake immediately on shutdown.
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.poll_interval_seconds
                )
    logger.info("worker stopped")
