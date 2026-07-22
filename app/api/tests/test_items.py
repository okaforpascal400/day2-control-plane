from __future__ import annotations

from httpx import AsyncClient


async def test_create_item_returns_201_and_body(app_client: AsyncClient) -> None:
    response = await app_client.post(
        "/items", json={"name": "control-plane", "description": "demo item"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["name"] == "control-plane"
    assert body["description"] == "demo item"
    assert body["created_at"] and body["updated_at"]


async def test_create_item_enqueues_a_pending_job(app_client: AsyncClient) -> None:
    item_id = (await app_client.post("/items", json={"name": "with-job"})).json()["id"]

    jobs = (await app_client.get("/jobs")).json()
    assert [j["item_id"] for j in jobs] == [item_id]
    assert jobs[0]["status"] == "pending"
    assert jobs[0]["kind"] == "index_item"


async def test_list_items_is_newest_first(app_client: AsyncClient) -> None:
    for name in ("first", "second", "third"):
        await app_client.post("/items", json={"name": name})

    response = await app_client.get("/items")
    assert response.status_code == 200
    assert [i["name"] for i in response.json()] == ["third", "second", "first"]


async def test_list_items_paginates(app_client: AsyncClient) -> None:
    for name in ("a", "b", "c"):
        await app_client.post("/items", json={"name": name})

    page = (await app_client.get("/items", params={"limit": 2, "offset": 1})).json()
    assert [i["name"] for i in page] == ["b", "a"]


async def test_get_item_roundtrip(app_client: AsyncClient) -> None:
    item_id = (await app_client.post("/items", json={"name": "readable"})).json()["id"]

    response = await app_client.get(f"/items/{item_id}")
    assert response.status_code == 200
    assert response.json()["name"] == "readable"


async def test_get_missing_item_is_404(app_client: AsyncClient) -> None:
    response = await app_client.get("/items/999999")
    assert response.status_code == 404
    assert response.json()["detail"] == "item not found"


async def test_patch_updates_only_supplied_fields(app_client: AsyncClient) -> None:
    created = (
        await app_client.post("/items", json={"name": "old", "description": "keep me"})
    ).json()

    response = await app_client.patch(f"/items/{created['id']}", json={"name": "new"})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "new"
    assert body["description"] == "keep me"


async def test_delete_item_removes_it(app_client: AsyncClient) -> None:
    item_id = (await app_client.post("/items", json={"name": "doomed"})).json()["id"]

    assert (await app_client.delete(f"/items/{item_id}")).status_code == 204
    assert (await app_client.get(f"/items/{item_id}")).status_code == 404


async def test_delete_missing_item_is_404(app_client: AsyncClient) -> None:
    assert (await app_client.delete("/items/999999")).status_code == 404


async def test_create_item_rejects_empty_name(app_client: AsyncClient) -> None:
    assert (await app_client.post("/items", json={"name": ""})).status_code == 422
