"""add Zotero original PDF references

Revision ID: 57022e4415e1
Revises: 615363d9c6af
Create Date: 2026-09-21 03:09:54.467121

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "57022e4415e1"
down_revision: str | None = "615363d9c6af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("zotero_item_links", sa.Column("local_pdf_path", sa.Text(), nullable=True))
    op.add_column("zotero_item_links", sa.Column("pdf_status", sa.String(24), nullable=True))
    op.add_column("zotero_item_links", sa.Column("pdf_error", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("zotero_item_links", "pdf_error")
    op.drop_column("zotero_item_links", "pdf_status")
    op.drop_column("zotero_item_links", "local_pdf_path")
