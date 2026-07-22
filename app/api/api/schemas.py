from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from day2_shared.models import JobStatus


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)


class ItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)


class ItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int | None
    kind: str
    status: JobStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobCreate(BaseModel):
    item_id: int | None = None
    kind: str = Field(default="index_item", min_length=1, max_length=50)


class HealthRead(BaseModel):
    status: str
    service: str


class ReadyRead(BaseModel):
    status: str
    database: str
