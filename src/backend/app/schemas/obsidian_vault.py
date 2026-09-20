"""Wire schemas for the desktop Obsidian vault bridge."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObsidianVaultConfigure(BaseModel):
    vault_path: str = Field(min_length=1, max_length=4096)


class ObsidianVaultConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vault_path: str
    status: str
    watching: bool = False
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class VaultLibraryBindingUpdate(BaseModel):
    enabled: bool = True


class VaultLibraryBindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    library_name: str | None = None
    enabled: bool
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ObsidianVaultStateRead(BaseModel):
    connection: ObsidianVaultConnectionRead | None
    bindings: list[VaultLibraryBindingRead]
    conflict_count: int = 0


class ObsidianVaultSyncRequest(BaseModel):
    library_id: uuid.UUID | None = None


class ObsidianVaultSyncRead(BaseModel):
    files_written: int
    files_imported: int
    files_unchanged: int
    files_deleted: int
    conflicts: int
    errors: list[str]


class VaultConflictRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: str
    library_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    relative_path: str
    base_content: str
    polaris_content: str
    vault_content: str
    status: str
    resolution: str | None
    resolved_content: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VaultConflictResolve(BaseModel):
    strategy: Literal["polaris", "vault", "merged"]
    expected_version: str = Field(min_length=64, max_length=64)
    content: str | None = Field(default=None, max_length=2_000_000)

    @model_validator(mode="after")
    def require_merged_content(self) -> "VaultConflictResolve":
        if self.strategy == "merged" and not (self.content or "").strip():
            raise ValueError("content is required for merged conflict resolution")
        return self
