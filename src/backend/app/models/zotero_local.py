"""Persistent state for read-only Zotero Desktop collection synchronization."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class ZoteroLocalBinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One Polaris library bound to one collection in the local Zotero library."""

    __tablename__ = "zotero_local_bindings"
    __table_args__ = (
        UniqueConstraint("library_id", name="uq_zotero_local_bindings_library"),
        Index("ix_zotero_local_bindings_due", "status", "next_sync_at"),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # The Local API exposes the current user's library as users/0.  Keep the identity
    # explicit so a future group/Web API adapter can use the same persistence shape.
    zotero_library_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    zotero_library_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="0", server_default="0"
    )
    zotero_instance_id: Mapped[str | None] = mapped_column(String(255))
    collection_key: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(512), nullable=False)
    include_descendants: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_library_version: Mapped[int | None]
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="idle", server_default="idle"
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ZoteroSyncRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable progress and result counters for one collection reconciliation."""

    __tablename__ = "zotero_sync_runs"
    __table_args__ = (
        Index("ix_zotero_sync_runs_binding_created", "binding_id", "created_at"),
        Index(
            "uq_zotero_sync_runs_one_active",
            "binding_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zotero_local_bindings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    full: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total: Mapped[int] = mapped_column(nullable=False, default=0)
    processed: Mapped[int] = mapped_column(nullable=False, default=0)
    created: Mapped[int] = mapped_column(nullable=False, default=0)
    updated: Mapped[int] = mapped_column(nullable=False, default=0)
    existing: Mapped[int] = mapped_column(nullable=False, default=0)
    ignored: Mapped[int] = mapped_column(nullable=False, default=0)
    missing: Mapped[int] = mapped_column(nullable=False, default=0)
    failed: Mapped[int] = mapped_column(nullable=False, default=0)
    error_samples: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONVariant)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ZoteroItemLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Stable Zotero item identity mapped to a global Polaris paper."""

    __tablename__ = "zotero_item_links"
    __table_args__ = (
        UniqueConstraint("binding_id", "item_key", name="uq_zotero_item_links_binding_item"),
        Index("ix_zotero_item_links_binding_status", "binding_id", "status"),
        Index("ix_zotero_item_links_paper", "paper_id"),
    )

    binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zotero_local_bindings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    item_version: Mapped[int] = mapped_column(nullable=False, default=0)
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), index=True
    )
    item_type: Mapped[str | None] = mapped_column(String(64))
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    attachment_key: Mapped[str | None] = mapped_column(String(64))
    attachment_version: Mapped[int | None]
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active"
    )
    membership_created_by_sync: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("zotero_sync_runs.id", ondelete="SET NULL"), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
