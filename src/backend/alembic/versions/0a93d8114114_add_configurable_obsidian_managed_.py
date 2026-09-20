"""add configurable obsidian managed directory

Revision ID: 0a93d8114114
Revises: 57022e4415e1
Create Date: 2026-09-21 05:46:56.411873

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0a93d8114114'
down_revision: str | None = '57022e4415e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "obsidian_vault_connections",
        sa.Column("managed_directory", sa.String(128), server_default="Polaris", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("obsidian_vault_connections", "managed_directory")
