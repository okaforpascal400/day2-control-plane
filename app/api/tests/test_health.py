from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from api.config import Settings
from api.main import create_app


async def test_health_ok(app_client: AsyncClient) -> None:
    response = await app_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api-test"}


@pytest.mark.asyncio
async def test_health_does_not_depend_on_the_database() -> None:
    """Liveness must stay green while Postgres is unreachable.

    A liveness probe that fails on a DB outage turns a recoverable dependency blip
    into a restart loop across every api pod.
    """
    settings = Settings(
        database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nonexistent",
        service_name="api-test",
        create_schema_on_start=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://api.test") as client:
            assert (await client.get("/health")).status_code == 200


async def test_ready_reports_database(app_client: AsyncClient) -> None:
    response = await app_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
