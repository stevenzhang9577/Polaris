"""Add LLM transports, auth schemes, and local-import provenance.

Revision ID: 9a7d4c2e6f10
Revises: 31cf6000d718
Create Date: 2026-09-20 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a7d4c2e6f10"
down_revision: str | None = "31cf6000d718"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_providers") as batch:
        batch.add_column(
            sa.Column(
                "transport",
                sa.String(32),
                nullable=False,
                server_default="chat_completions",
            )
        )
        batch.add_column(
            sa.Column("auth_scheme", sa.String(24), nullable=False, server_default="bearer")
        )
        batch.add_column(sa.Column("import_source", sa.String(32)))
        batch.add_column(sa.Column("import_source_key", sa.String(255)))
        batch.add_column(sa.Column("import_fingerprint", sa.String(64)))
        batch.add_column(sa.Column("imported_at", sa.DateTime(timezone=True)))

    # Existing rows predate explicit transports. Preserve their prior runtime
    # behavior rather than applying the OpenAI defaults to every family.
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE llm_providers SET transport = 'anthropic_messages', "
            "auth_scheme = 'x_api_key' WHERE kind = 'anthropic'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE llm_providers SET transport = 'fake', auth_scheme = 'none' "
            "WHERE kind = 'fake'"
        )
    )

    op.create_index(
        "uq_llm_providers_global_import_source_key",
        "llm_providers",
        ["import_source", "import_source_key"],
        unique=True,
        sqlite_where=sa.text("owner_id IS NULL AND import_source IS NOT NULL"),
        postgresql_where=sa.text("owner_id IS NULL AND import_source IS NOT NULL"),
    )
    op.create_index(
        "uq_llm_providers_owner_import_source_key",
        "llm_providers",
        ["owner_id", "import_source", "import_source_key"],
        unique=True,
        sqlite_where=sa.text("owner_id IS NOT NULL AND import_source IS NOT NULL"),
        postgresql_where=sa.text("owner_id IS NOT NULL AND import_source IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_llm_providers_owner_import_source_key", table_name="llm_providers"
    )
    op.drop_index(
        "uq_llm_providers_global_import_source_key", table_name="llm_providers"
    )
    with op.batch_alter_table("llm_providers") as batch:
        batch.drop_column("imported_at")
        batch.drop_column("import_fingerprint")
        batch.drop_column("import_source_key")
        batch.drop_column("import_source")
        batch.drop_column("auth_scheme")
        batch.drop_column("transport")
