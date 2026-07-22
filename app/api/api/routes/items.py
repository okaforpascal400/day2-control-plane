from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from api.deps import SessionDep
from api.schemas import ItemCreate, ItemRead, ItemUpdate
from day2_shared.models import Item, Job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/items", tags=["items"])


async def _get_or_404(session: SessionDep, item_id: int) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="item not found")
    return item


@router.get("", response_model=list[ItemRead])
async def list_items(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Item]:
    result = await session.execute(
        select(Item).order_by(Item.id.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@router.post("", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def create_item(payload: ItemCreate, session: SessionDep) -> Item:
    item = Item(name=payload.name, description=payload.description)
    session.add(item)
    await session.flush()
    # Every new item enqueues work so the worker has something real to drain.
    session.add(Job(item_id=item.id, kind="index_item"))
    await session.flush()
    logger.info("item created", extra={"item_id": item.id})
    return item


@router.get("/{item_id}", response_model=ItemRead)
async def get_item(item_id: int, session: SessionDep) -> Item:
    return await _get_or_404(session, item_id)


@router.patch("/{item_id}", response_model=ItemRead)
async def update_item(item_id: int, payload: ItemUpdate, session: SessionDep) -> Item:
    item = await _get_or_404(session, item_id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(item, field, value)
    await session.flush()
    # `updated_at` is set by an onupdate server default, so it is expired after the
    # flush; refresh eagerly or serialisation triggers lazy IO outside the greenlet.
    await session.refresh(item)
    logger.info("item updated", extra={"item_id": item.id, "fields": list(data)})
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_item(item_id: int, session: SessionDep) -> None:
    item = await _get_or_404(session, item_id)
    await session.delete(item)
    logger.info("item deleted", extra={"item_id": item_id})
