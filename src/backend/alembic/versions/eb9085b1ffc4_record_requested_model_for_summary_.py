"""record requested model for summary attempts

Revision ID: eb9085b1ffc4
Revises: 4758f07e3148
Create Date: 2026-09-21 13:10:47.239245

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'eb9085b1ffc4'
down_revision: str | None = '4758f07e3148'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_wiki_revisions", sa.Column("requested_model", sa.String(255), nullable=True))
    op.add_column("paper_wiki_revisions", sa.Column("provider_name", sa.String(128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("paper_wiki_revisions") as batch:
        batch.drop_column("provider_name")
        batch.drop_column("requested_model")
