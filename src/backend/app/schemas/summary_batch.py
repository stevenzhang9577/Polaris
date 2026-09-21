"""Selection is a filter snapshot, never a client-side loaded-page approximation."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SummarySettings(BaseModel):
    concurrency: int = Field(default=3, ge=1, le=20)


class SummarySelectionFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = "library"
    q: str | None = None
    sort: str = "relevance"
    my_tag: str | None = None
    reading_status: str | None = None
    starred: bool | None = None
    author: str | None = None
    affiliation: str | None = None
    published_from: datetime | None = None
    published_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    daily_only: bool = False
    last_sync_only: bool = False


class SummaryBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    paper_ids: list[uuid.UUID] | None = Field(default=None, max_length=50000)
    filters: SummarySelectionFilters | None = None
    excluded_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50000)
    skip_existing: bool = True

    @model_validator(mode="after")
    def one_selection(self):
        if (self.paper_ids is None) == (self.filters is None):
            raise ValueError("Choose paper_ids or filters, not both")
        if self.paper_ids is not None and not self.paper_ids:
            raise ValueError("Select at least one paper")
        return self


class SummaryBatchRead(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    status: Literal["queued", "running", "paused", "completed", "completed_with_errors"]
    total: int
    pending: int
    running: int
    completed: int
    skipped: int
    failed: int
    created_at: datetime
    updated_at: datetime
    concurrency: int

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def serialize_database_timestamps_as_utc(cls, value):
        """SQLite drops timezone metadata; API timestamps must still be unambiguous UTC."""
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        return value


class SummaryBatchItemRead(BaseModel):
    paper_id: uuid.UUID
    title: str
    status: str
    stage: str | None = None
    error: str | None = None


class SummaryBatchPage(BaseModel):
    batch: SummaryBatchRead
    items: list[SummaryBatchItemRead]
    page: int
    size: int
    total: int
