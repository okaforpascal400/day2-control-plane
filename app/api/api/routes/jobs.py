from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from api.deps import SessionDep
from api.schemas import JobCreate, JobRead
from day2_shared.models import Item, Job, JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobRead])
async def list_jobs(
    session: SessionDep,
    job_status: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Job]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit).offset(offset)
    if job_status is not None:
        stmt = stmt.where(Job.status == job_status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/stats")
async def job_stats(session: SessionDep) -> dict[str, int]:
    """Counts per status — what the dashboard polls."""
    result = await session.execute(select(Job.status, func.count()).group_by(Job.status))
    counts = {s.value: 0 for s in JobStatus}
    for job_status, count in result.all():
        counts[JobStatus(job_status).value] = count
    return counts


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreate, session: SessionDep) -> Job:
    if payload.item_id is not None and await session.get(Item, payload.item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="item not found")
    job = Job(item_id=payload.item_id, kind=payload.kind)
    session.add(job)
    await session.flush()
    return job
