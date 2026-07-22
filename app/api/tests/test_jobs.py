from __future__ import annotations

from httpx import AsyncClient


async def test_jobs_empty_by_default(app_client: AsyncClient) -> None:
    response = await app_client.get("/jobs")
    assert response.status_code == 200
    assert response.json() == []


async def test_create_job_without_item(app_client: AsyncClient) -> None:
    response = await app_client.post("/jobs", json={"kind": "reindex_all"})
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "reindex_all"
    assert body["status"] == "pending"
    assert body["item_id"] is None
    assert body["attempts"] == 0
    assert body["started_at"] is None and body["finished_at"] is None


async def test_create_job_for_missing_item_is_404(app_client: AsyncClient) -> None:
    response = await app_client.post("/jobs", json={"item_id": 999999})
    assert response.status_code == 404


async def test_jobs_filter_by_status(app_client: AsyncClient) -> None:
    await app_client.post("/items", json={"name": "queued"})

    assert len((await app_client.get("/jobs", params={"status": "pending"})).json()) == 1
    assert (await app_client.get("/jobs", params={"status": "completed"})).json() == []


async def test_job_stats_counts_every_status(app_client: AsyncClient) -> None:
    await app_client.post("/items", json={"name": "one"})
    await app_client.post("/items", json={"name": "two"})

    stats = (await app_client.get("/jobs/stats")).json()
    assert stats == {"pending": 2, "processing": 0, "completed": 0, "failed": 0}


async def test_deleting_an_item_cascades_to_its_jobs(app_client: AsyncClient) -> None:
    item_id = (await app_client.post("/items", json={"name": "cascade"})).json()["id"]

    await app_client.delete(f"/items/{item_id}")

    assert (await app_client.get("/jobs")).json() == []
