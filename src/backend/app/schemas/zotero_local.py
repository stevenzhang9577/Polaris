"""Wire schemas for the Zotero Desktop Local API integration."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ZoteroProbeRead(BaseModel):
    available: bool
    api_version: int | None = None
    zotero_version: str | None = None
    instance_id: str | None = None


class ZoteroCollectionRead(BaseModel):
    key: str
    name: str
    parent_key: str | None = None
    version: int = 0
    child_count: int = 0


class ZoteroBindingCreate(BaseModel):
    collection_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9]+$")


class ZoteroBindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    zotero_library_type: str
    zotero_library_id: str
    zotero_instance_id: str | None
    collection_key: str
    collection_name: str
    include_descendants: bool
    last_library_version: int | None
    status: str
    last_synced_at: datetime | None
    next_sync_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ZoteroSyncRequest(BaseModel):
    full: bool = False


class ZoteroSyncRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    binding_id: uuid.UUID
    requested_by: uuid.UUID | None
    status: str
    full: bool
    total: int
    processed: int
    created: int
    updated: int
    existing: int
    ignored: int
    missing: int
    failed: int
    error_samples: list[dict[str, Any]] | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ZoteroMaterializeRead(BaseModel):
    paper_id: uuid.UUID
    asset_id: uuid.UUID
    attachment_key: str
    attachment_version: int | None = None
    byte_size: int
    source_locator: str
