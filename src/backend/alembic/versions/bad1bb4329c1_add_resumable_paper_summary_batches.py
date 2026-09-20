"""add resumable paper summary batches

Revision ID: bad1bb4329c1
Revises: 0a93d8114114
Create Date: 2026-09-21 05:48:59.827190

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bad1bb4329c1"
down_revision: str | None = "0a93d8114114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "summary_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("runner_token", sa.Uuid()),
        sa.Column("runner_expires_at", sa.DateTime(timezone=True)),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("skip_existing", sa.Boolean(), nullable=False),
        sa.Column(
            "selection", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "request_id"),
    )
    op.create_index("ix_summary_batches_library_id", "summary_batches", ["library_id"])
    op.create_index("ix_summary_batches_status", "summary_batches", ["status"])
    op.create_table(
        "summary_batch_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Uuid(),
            sa.ForeignKey("summary_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "revision_id", sa.Uuid(), sa.ForeignKey("paper_wiki_revisions.id", ondelete="SET NULL")
        ),
        sa.Column("error", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "paper_id"),
    )
    op.create_index(
        "ix_summary_batch_items_dispatch", "summary_batch_items", ["batch_id", "status"]
    )
    op.create_table(
        "summary_generation_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "slot"),
        sa.UniqueConstraint("paper_id"),
    )
    op.create_index(
        "ix_summary_generation_leases_expires_at", "summary_generation_leases", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("summary_generation_leases")
    op.drop_table("summary_batch_items")
    op.drop_table("summary_batches")
