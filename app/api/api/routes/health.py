"""Liveness and readiness.

/health deliberately touches nothing but the process itself: if Postgres is down the
api is still alive and must not be killed and restarted by the kubelet. Dependency
health belongs in /ready, which gates traffic instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from api.deps import SessionDep
from api.schemas import HealthRead, ReadyRead

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthRead)
async def health(request: Request) -> HealthRead:
    return HealthRead(status="ok", service=request.app.state.settings.service_name)


@router.get("/ready", response_model=ReadyRead)
async def ready(session: SessionDep, response: Response) -> ReadyRead:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyRead(status="degraded", database="unavailable")
    return ReadyRead(status="ok", database="ok")
