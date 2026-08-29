"""Prometheus metrics for the worker.

The worker has no HTTP surface of its own, so `prometheus_client` runs a tiny
server in a daemon thread. Because the deployment is single-replica, the
queue-depth gauge is authoritative — no two workers report overlapping counts.
The metric names are the contract the app-overview dashboard queries against.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from day2_shared.db import session_scope
from day2_shared.models import Job, JobStatus

# result: "completed" | "failed" (attempts exhausted) | "retried" (will run again).
JOBS_PROCESSED = Counter(
    "day2_jobs_processed_total",
    "Jobs the worker finished a run of, by outcome.",
    ["result"],
)

JOB_PROCESSING_SECONDS = Histogram(
    "day2_job_processing_seconds",
    "Wall-clock time to run one job's work.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Set from a GROUP BY status each poll cycle; drives queue depth and drain rate.
QUEUE_DEPTH = Gauge(
    "day2_job_queue_depth",
    "Jobs in the queue by status (single-replica worker, so unambiguous).",
    ["status"],
)


def serve_metrics(port: int) -> None:
    """Start the /metrics HTTP server in a background daemon thread."""
    start_http_server(port)


async def refresh_queue_depth(factory: async_sessionmaker[AsyncSession]) -> None:
    """Publish one gauge sample per job status from a single GROUP BY query."""
    counts = {s.value: 0 for s in JobStatus}
    async with session_scope(factory) as session:
        result = await session.execute(
            select(Job.status, func.count()).group_by(Job.status)
        )
        for job_status, count in result.all():
            counts[JobStatus(job_status).value] = count
    for status_value, count in counts.items():
        QUEUE_DEPTH.labels(status=status_value).set(count)
