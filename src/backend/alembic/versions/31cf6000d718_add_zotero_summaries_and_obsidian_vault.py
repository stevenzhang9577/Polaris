"""Add Zotero Local sync, versioned summaries, and editable Obsidian Vault state.

Revision ID: 31cf6000d718
Revises: c1d80a3fb492
Create Date: 2026-09-20 10:37:13.992186

"""

import hashlib
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "31cf6000d718"
down_revision: str | None = "c1d80a3fb492"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    # Revision history is created before the PaperWiki pointer that references it.
    op.create_table(
        "paper_wiki_revisions",
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "content_version_id",
            sa.Uuid(),
            sa.ForeignKey("paper_content_versions.id", ondelete="SET NULL"),
        ),
        sa.Column("source_level", sa.String(16), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("tldr", sa.Text()),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("schema_version", sa.String(64)),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "source_library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("source_fingerprint", sa.String(64)),
        sa.Column("evidence_manifest", _JSON),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(24)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
    )
    for name, columns in (
        ("ix_paper_wiki_revisions_paper_id", ["paper_id"]),
        ("ix_paper_wiki_revisions_content_version_id", ["content_version_id"]),
        ("ix_paper_wiki_revisions_created_by", ["created_by"]),
        ("ix_paper_wiki_revisions_source_library_id", ["source_library_id"]),
        ("ix_paper_wiki_revisions_source_project_id", ["source_project_id"]),
        ("ix_paper_wiki_revisions_source_fingerprint", ["source_fingerprint"]),
        ("ix_paper_wiki_revisions_status", ["status"]),
        ("ix_paper_wiki_revisions_paper_created", ["paper_id", "created_at"]),
    ):
        op.create_index(name, "paper_wiki_revisions", columns)
    op.create_index(
        "uq_paper_wiki_revisions_one_inflight",
        "paper_wiki_revisions",
        ["paper_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'generating')"),
        sqlite_where=sa.text("status IN ('queued', 'generating')"),
    )

    with op.batch_alter_table("paper_wikis") as batch:
        batch.add_column(sa.Column("current_revision_id", sa.Uuid()))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True)))
        batch.create_foreign_key(
            "fk_paper_wikis_current_revision_id_paper_wiki_revisions",
            "paper_wiki_revisions",
            ["current_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_paper_wikis_current_revision_id", ["current_revision_id"])

    with op.batch_alter_table("paper_notes") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_paper_notes_deleted_at", ["deleted_at"])

    # Existing current wiki rows become immutable legacy revisions without changing content.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, paper_id, content, model, compiled_by, created_at, updated_at "
            "FROM paper_wikis"
        )
    ).mappings()
    revision_table = sa.table(
        "paper_wiki_revisions",
        sa.column("id", sa.Uuid()),
        sa.column("paper_id", sa.Uuid()),
        sa.column("source_level", sa.String()),
        sa.column("content", sa.Text()),
        sa.column("tldr", sa.Text()),
        sa.column("model", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("source_fingerprint", sa.String()),
        sa.column("status", sa.String()),
        sa.column("stage", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for row in rows:
        revision_id = uuid.uuid4()
        content = row["content"] or ""
        connection.execute(
            revision_table.insert().values(
                id=revision_id,
                paper_id=row["paper_id"],
                source_level="legacy",
                content=content,
                tldr=None,
                model=row["model"],
                created_by=row["compiled_by"],
                source_fingerprint=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                status="ready",
                stage="complete",
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        connection.execute(
            sa.text("UPDATE paper_wikis SET current_revision_id = :revision WHERE id = :wiki"),
            {"revision": revision_id, "wiki": row["id"]},
        )

    op.create_table(
        "zotero_local_bindings",
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("zotero_library_type", sa.String(16), nullable=False, server_default="user"),
        sa.Column("zotero_library_id", sa.String(64), nullable=False, server_default="0"),
        sa.Column("zotero_instance_id", sa.String(255)),
        sa.Column("collection_key", sa.String(64), nullable=False),
        sa.Column("collection_name", sa.String(512), nullable=False),
        sa.Column("include_descendants", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_library_version", sa.Integer()),
        sa.Column("status", sa.String(24), nullable=False, server_default="idle"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("next_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
        sa.UniqueConstraint("library_id", name="uq_zotero_local_bindings_library"),
    )
    op.create_index("ix_zotero_local_bindings_library_id", "zotero_local_bindings", ["library_id"])
    op.create_index("ix_zotero_local_bindings_created_by", "zotero_local_bindings", ["created_by"])
    op.create_index("ix_zotero_local_bindings_due", "zotero_local_bindings", ["status", "next_sync_at"])

    op.create_table(
        "zotero_sync_runs",
        sa.Column("binding_id", sa.Uuid(), sa.ForeignKey("zotero_local_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("full", sa.Boolean(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("existing", sa.Integer(), nullable=False),
        sa.Column("ignored", sa.Integer(), nullable=False),
        sa.Column("missing", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("error_samples", _JSON),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
    )
    op.create_index("ix_zotero_sync_runs_binding_id", "zotero_sync_runs", ["binding_id"])
    op.create_index("ix_zotero_sync_runs_requested_by", "zotero_sync_runs", ["requested_by"])
    op.create_index("ix_zotero_sync_runs_binding_created", "zotero_sync_runs", ["binding_id", "created_at"])
    op.create_index(
        "uq_zotero_sync_runs_one_active",
        "zotero_sync_runs",
        ["binding_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "zotero_item_links",
        sa.Column("binding_id", sa.Uuid(), sa.ForeignKey("zotero_local_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_key", sa.String(64), nullable=False),
        sa.Column("item_version", sa.Integer(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="SET NULL")),
        sa.Column("item_type", sa.String(64)),
        sa.Column("metadata_snapshot", _JSON),
        sa.Column("attachment_key", sa.String(64)),
        sa.Column("attachment_version", sa.Integer()),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("membership_created_by_sync", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_run_id", sa.Uuid(), sa.ForeignKey("zotero_sync_runs.id", ondelete="SET NULL")),
        sa.Column("last_error", sa.Text()),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
        sa.UniqueConstraint("binding_id", "item_key", name="uq_zotero_item_links_binding_item"),
    )
    for name, columns in (
        ("ix_zotero_item_links_binding_id", ["binding_id"]),
        ("ix_zotero_item_links_paper_id", ["paper_id"]),
        ("ix_zotero_item_links_last_seen_run_id", ["last_seen_run_id"]),
        ("ix_zotero_item_links_binding_status", ["binding_id", "status"]),
        ("ix_zotero_item_links_paper", ["paper_id"]),
    ):
        op.create_index(name, "zotero_item_links", columns)

    op.create_table(
        "obsidian_vault_connections",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vault_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ready"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", name="uq_obsidian_vault_connections_user"),
    )
    op.create_index("ix_obsidian_vault_connections_user_id", "obsidian_vault_connections", ["user_id"])

    op.create_table(
        "obsidian_vault_library_bindings",
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("obsidian_vault_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
        sa.UniqueConstraint("connection_id", "library_id", name="uq_obsidian_vault_binding_connection_library"),
    )
    op.create_index("ix_obsidian_vault_library_bindings_connection_id", "obsidian_vault_library_bindings", ["connection_id"])
    op.create_index("ix_obsidian_vault_library_bindings_library_id", "obsidian_vault_library_bindings", ["library_id"])

    op.create_table(
        "obsidian_vault_file_states",
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("obsidian_vault_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("base_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_hash", sa.String(64), nullable=False),
        sa.Column("polaris_hash", sa.String(64), nullable=False),
        sa.Column("vault_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="synced"),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
        sa.UniqueConstraint("connection_id", "relative_path", name="uq_obsidian_vault_file_connection_path"),
        sa.UniqueConstraint("connection_id", "library_id", "entity_type", "entity_id", name="uq_obsidian_vault_file_entity"),
    )
    op.create_index("ix_obsidian_vault_file_states_connection_id", "obsidian_vault_file_states", ["connection_id"])
    op.create_index("ix_obsidian_vault_file_states_library_id", "obsidian_vault_file_states", ["library_id"])
    op.create_index("ix_obsidian_vault_file_connection_status", "obsidian_vault_file_states", ["connection_id", "status"])

    op.create_table(
        "obsidian_vault_conflicts",
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("obsidian_vault_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_state_id", sa.Uuid(), sa.ForeignKey("obsidian_vault_file_states.id", ondelete="CASCADE"), nullable=False),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(24), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("base_content", sa.Text(), nullable=False),
        sa.Column("polaris_content", sa.Text(), nullable=False),
        sa.Column("vault_content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.String(16)),
        sa.Column("resolved_content", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("id", sa.Uuid(), primary_key=True),
        *_timestamps(),
    )
    op.create_index("ix_obsidian_vault_conflicts_connection_id", "obsidian_vault_conflicts", ["connection_id"])
    op.create_index("ix_obsidian_vault_conflicts_file_state_id", "obsidian_vault_conflicts", ["file_state_id"])
    op.create_index("ix_obsidian_vault_conflicts_library_id", "obsidian_vault_conflicts", ["library_id"])
    op.create_index("ix_obsidian_vault_conflict_connection_status", "obsidian_vault_conflicts", ["connection_id", "status"])


def downgrade() -> None:
    for name in (
        "ix_obsidian_vault_conflict_connection_status",
        "ix_obsidian_vault_conflicts_library_id",
        "ix_obsidian_vault_conflicts_file_state_id",
        "ix_obsidian_vault_conflicts_connection_id",
    ):
        op.drop_index(name, table_name="obsidian_vault_conflicts")
    op.drop_table("obsidian_vault_conflicts")
    for name in (
        "ix_obsidian_vault_file_connection_status",
        "ix_obsidian_vault_file_states_library_id",
        "ix_obsidian_vault_file_states_connection_id",
    ):
        op.drop_index(name, table_name="obsidian_vault_file_states")
    op.drop_table("obsidian_vault_file_states")
    op.drop_index("ix_obsidian_vault_library_bindings_library_id", table_name="obsidian_vault_library_bindings")
    op.drop_index("ix_obsidian_vault_library_bindings_connection_id", table_name="obsidian_vault_library_bindings")
    op.drop_table("obsidian_vault_library_bindings")
    op.drop_index("ix_obsidian_vault_connections_user_id", table_name="obsidian_vault_connections")
    op.drop_table("obsidian_vault_connections")

    for name in (
        "ix_zotero_item_links_paper",
        "ix_zotero_item_links_binding_status",
        "ix_zotero_item_links_last_seen_run_id",
        "ix_zotero_item_links_paper_id",
        "ix_zotero_item_links_binding_id",
    ):
        op.drop_index(name, table_name="zotero_item_links")
    op.drop_table("zotero_item_links")
    op.drop_index("uq_zotero_sync_runs_one_active", table_name="zotero_sync_runs")
    op.drop_index("ix_zotero_sync_runs_binding_created", table_name="zotero_sync_runs")
    op.drop_index("ix_zotero_sync_runs_requested_by", table_name="zotero_sync_runs")
    op.drop_index("ix_zotero_sync_runs_binding_id", table_name="zotero_sync_runs")
    op.drop_table("zotero_sync_runs")
    op.drop_index("ix_zotero_local_bindings_due", table_name="zotero_local_bindings")
    op.drop_index("ix_zotero_local_bindings_created_by", table_name="zotero_local_bindings")
    op.drop_index("ix_zotero_local_bindings_library_id", table_name="zotero_local_bindings")
    op.drop_table("zotero_local_bindings")

    with op.batch_alter_table("paper_notes") as batch:
        batch.drop_index("ix_paper_notes_deleted_at")
        batch.drop_column("deleted_at")

    with op.batch_alter_table("paper_wikis") as batch:
        batch.drop_index("ix_paper_wikis_current_revision_id")
        batch.drop_constraint(
            "fk_paper_wikis_current_revision_id_paper_wiki_revisions",
            type_="foreignkey",
        )
        batch.drop_column("deleted_at")
        batch.drop_column("current_revision_id")
    for name in (
        "uq_paper_wiki_revisions_one_inflight",
        "ix_paper_wiki_revisions_paper_created",
        "ix_paper_wiki_revisions_status",
        "ix_paper_wiki_revisions_source_fingerprint",
        "ix_paper_wiki_revisions_source_project_id",
        "ix_paper_wiki_revisions_source_library_id",
        "ix_paper_wiki_revisions_created_by",
        "ix_paper_wiki_revisions_content_version_id",
        "ix_paper_wiki_revisions_paper_id",
    ):
        op.drop_index(name, table_name="paper_wiki_revisions")
    op.drop_table("paper_wiki_revisions")
