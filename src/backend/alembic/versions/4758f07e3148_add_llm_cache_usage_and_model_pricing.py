"""add llm cache usage and model pricing

Revision ID: 4758f07e3148
Revises: bad1bb4329c1
Create Date: 2026-09-21 08:12:05.204750

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4758f07e3148"
down_revision: str | None = "bad1bb4329c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keep the provider-side price table separate from the usage ledger. Prices
    # can change later; each call copies the rates it used into pricing_snapshot.
    with op.batch_alter_table("llm_providers") as batch:
        batch.add_column(sa.Column("model_pricing", sa.JSON(), nullable=True))

    # batch_alter_table uses native ALTER TABLE on PostgreSQL and transparently
    # recreates the table when SQLite cannot perform an operation in place. The
    # server default both backfills existing rows and protects direct inserts.
    with op.batch_alter_table("llm_usage") as batch:
        batch.add_column(sa.Column("provider_name", sa.String(255), nullable=True))
        batch.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cache_creation_tokens", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "usage_estimated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(sa.Column("cost_usd", sa.Numeric(24, 14), nullable=True))
        batch.add_column(sa.Column("pricing_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch:
        batch.drop_column("pricing_snapshot")
        batch.drop_column("cost_usd")
        batch.drop_column("usage_estimated")
        batch.drop_column("cache_creation_tokens")
        batch.drop_column("cache_read_tokens")
        batch.drop_column("provider_name")

    with op.batch_alter_table("llm_providers") as batch:
        batch.drop_column("model_pricing")
