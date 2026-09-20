"""Durable batch snapshots and cross-worker summary concurrency leases."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class SummaryBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "summary_batches"
    __table_args__ = (UniqueConstraint("user_id", "request_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[uuid.UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    skip_existing: Mapped[bool] = mapped_column(default=True)
    selection: Mapped[dict] = mapped_column(JSONVariant)
    runner_token: Mapped[uuid.UUID | None]
    runner_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SummaryBatchItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "summary_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "paper_id"),
        Index("ix_summary_batch_items_dispatch", "batch_id", "status"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("summary_batches.id", ondelete="CASCADE")
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_wiki_revisions.id", ondelete="SET NULL")
    )
    error: Mapped[str | None] = mapped_column(String(128))


class SummaryGenerationLease(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "summary_generation_leases"
    __table_args__ = (UniqueConstraint("user_id", "slot"), UniqueConstraint("paper_id"))

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    slot: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
