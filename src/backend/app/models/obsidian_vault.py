"""Editable Obsidian vault projection state.

These tables belong to the desktop-only, bidirectional vault bridge.  The database remains
the authoritative store for papers while ``VaultFileState.base_content`` is the three-way
merge base shared by the database and the managed Markdown file.
"""

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ObsidianVaultConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One local Obsidian vault per desktop user profile."""

    __tablename__ = "obsidian_vault_connections"
    __table_args__ = (UniqueConstraint("user_id", name="uq_obsidian_vault_connections_user"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Desktop-only local path.  It must never be copied into remote settings or logs.
    vault_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default="ready", server_default="ready", nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class VaultLibraryBinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A library selected for projection into a connection's managed ``Polaris`` folder."""

    __tablename__ = "obsidian_vault_library_bindings"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "library_id", name="uq_obsidian_vault_binding_connection_library"
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obsidian_vault_connections.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VaultFileState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Merge state for one managed Markdown file.

    ``base_content`` is body-only Markdown: generated frontmatter is metadata and is never a
    user-editable merge input.  ``deleted_at`` is a bridge tombstone used to coordinate a
    soft-delete with the owning domain service without hard-deleting papers or attachments.
    """

    __tablename__ = "obsidian_vault_file_states"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "relative_path", name="uq_obsidian_vault_file_connection_path"
        ),
        UniqueConstraint(
            "connection_id",
            "library_id",
            "entity_type",
            "entity_id",
            name="uq_obsidian_vault_file_entity",
        ),
        Index("ix_obsidian_vault_file_connection_status", "connection_id", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obsidian_vault_connections.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # summary | notes | library_index
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # Paper id for summary/notes; library id for library_index.
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    base_content: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    base_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    polaris_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vault_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default="synced", server_default="synced", nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VaultConflict(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An unresolved overlapping edit; all three inputs are preserved verbatim."""

    __tablename__ = "obsidian_vault_conflicts"
    __table_args__ = (
        Index("ix_obsidian_vault_conflict_connection_status", "connection_id", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obsidian_vault_connections.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    file_state_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("obsidian_vault_file_states.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    base_content: Mapped[str] = mapped_column(Text, nullable=False)
    polaris_content: Mapped[str] = mapped_column(Text, nullable=False)
    vault_content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="open", server_default="open", nullable=False
    )
    resolution: Mapped[str | None] = mapped_column(String(16))
    resolved_content: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def version(self) -> str:
        """Opaque compare-and-swap token for the exact conflict shown in the client."""
        payload = [self.base_content, self.polaris_content, self.vault_content,
                   self.status, self.relative_path]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
