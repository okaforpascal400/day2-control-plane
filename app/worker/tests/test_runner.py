from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from day2_shared.db import session_scope
from day2_shared.models import Job, JobStatus
from worker import runner
from worker.config import Settings

Factory = async_sessionmaker[AsyncSession]


async def seed_jobs(factory: Factory, count: int) -> list[int]:
    async with session_scope(factory) as session:
        jobs = [Job(kind="index_item") for _ in range(count)]
        session.add_all(jobs)
        await session.flush()
        return [job.id for job in jobs]


async def load_job(factory: Factory, job_id: int) -> Job:
    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        return job


async def test_process_batch_completes_pending_jobs(
    factory: Factory, settings: Settings
) -> None:
    (job_id,) = await seed_jobs(factory, 1)

    assert await runner.process_batch(factory, settings) == 1

    job = await load_job(factory, job_id)
    assert job.status is JobStatus.completed
    assert job.attempts == 1
    assert job.last_error is None


async def test_completed_job_has_ordered_timestamps(
    factory: Factory, settings: Settings
) -> None:
    (job_id,) = await seed_jobs(factory, 1)

    await runner.process_batch(factory, settings)

    job = await load_job(factory, job_id)
    assert job.started_at is not None and job.finished_at is not None
    assert job.created_at <= job.started_at <= job.finished_at


async def test_process_batch_is_a_noop_when_queue_is_empty(
    factory: Factory, settings: Settings
) -> None:
    assert await runner.process_batch(factory, settings) == 0


async def test_batch_size_bounds_one_cycle(factory: Factory, settings: Settings) -> None:
    await seed_jobs(factory, 5)
    settings.batch_size = 2

    assert await runner.process_batch(factory, settings) == 2

    async with session_scope(factory) as session:
        remaining = await session.execute(
            select(Job).where(Job.status == JobStatus.pending)
        )
        assert len(list(remaining.scalars().all())) == 3


async def test_completed_jobs_are_not_picked_up_again(
    factory: Factory, settings: Settings
) -> None:
    await seed_jobs(factory, 2)

    assert await runner.process_batch(factory, settings) == 2
    assert await runner.process_batch(factory, settings) == 0


async def test_failed_work_retries_until_max_attempts(
    factory: Factory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    (job_id,) = await seed_jobs(factory, 1)
    settings.max_attempts = 2

    async def boom(job: Job, duration: float) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(runner, "perform_work", boom)

    await runner.process_batch(factory, settings)
    job = await load_job(factory, job_id)
    assert job.status is JobStatus.pending  # requeued for a second attempt
    assert job.attempts == 1
    assert "simulated failure" in job.last_error

    await runner.process_batch(factory, settings)
    job = await load_job(factory, job_id)
    assert job.status is JobStatus.failed
    assert job.attempts == 2
    assert job.finished_at is not None


async def test_concurrent_workers_never_double_process(
    factory: Factory, settings: Settings
) -> None:
    """SKIP LOCKED is what makes replicas > 1 safe; assert it rather than trust it."""
    await seed_jobs(factory, 6)
    settings.batch_size = 6

    handled = await asyncio.gather(
        runner.process_batch(factory, settings),
        runner.process_batch(factory, settings),
        runner.process_batch(factory, settings),
    )

    assert sum(handled) == 6
    async with session_scope(factory) as session:
        result = await session.execute(select(Job))
        jobs = list(result.scalars().all())
    assert all(job.status is JobStatus.completed for job in jobs)
    assert all(job.attempts == 1 for job in jobs)


async def test_run_forever_drains_queue_then_honours_stop(
    factory: Factory, settings: Settings
) -> None:
    await seed_jobs(factory, 3)
    stop = asyncio.Event()

    task = asyncio.create_task(runner.run_forever(factory, settings, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    async with session_scope(factory) as session:
        result = await session.execute(
            select(Job).where(Job.status != JobStatus.completed)
        )
        assert list(result.scalars().all()) == []


async def test_heartbeat_is_written_and_ages_out(
    factory: Factory, settings: Settings
) -> None:
    assert not runner.heartbeat_is_fresh(settings.heartbeat_path, 30)

    await runner.process_batch(factory, settings)
    runner.touch_heartbeat(settings.heartbeat_path)

    assert runner.heartbeat_is_fresh(settings.heartbeat_path, 30)
    assert not runner.heartbeat_is_fresh(settings.heartbeat_path, 0)
