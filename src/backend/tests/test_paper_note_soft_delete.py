"""PaperNote tombstones and Obsidian notes-file recovery."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.obsidian_vault import VaultFileState
from app.models.paper import PaperNote, new_paper
from app.models.user import User
from app.services.notes import purge_expired_notes
from app.services.obsidian_vault_bridge import (
    configure_connection,
    managed_root,
    safe_managed_path,
    set_library_binding,
    sync_connection,
)


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "Paper Notes Vault"
    (root / ".obsidian").mkdir(parents=True)
    return root


@pytest.mark.asyncio
async def test_vault_notes_file_delete_soft_deletes_and_recreate_restores(
    app, tmp_path: Path
) -> None:
    vault = _vault(tmp_path)
    async with get_sessionmaker()() as session:
        user = User(
            email="note-vault@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        session.add(user)
        await session.flush()
        library = DirectionLibrary(name="Private Notes", submitted_by=user.id)
        paper = new_paper(title="Recoverable Notes", dedup_key="title:recoverable-notes")
        session.add_all([library, paper])
        await session.flush()
        session.add(
            LibraryPaper(library_id=library.id, paper_id=paper.id, status="included")
        )
        note = PaperNote(paper_id=paper.id, author_id=user.id, content="keep my judgment")
        session.add(note)
        await session.commit()

        connection = await configure_connection(
            session, user_id=user.id, vault_path=str(vault)
        )
        await set_library_binding(
            session,
            connection_id=connection.id,
            library_id=library.id,
            enabled=True,
        )
        await sync_connection(session, connection=connection, user=user)
        await session.commit()

        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "notes")
        )
        assert state is not None
        path = safe_managed_path(managed_root(vault), state.relative_path)
        original = path.read_text(encoding="utf-8")
        assert str(note.id) in original

        path.unlink()
        deleted = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        await session.refresh(note)
        assert deleted.files_deleted == 1
        assert note.deleted_at is not None
        assert not path.exists()

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original, encoding="utf-8")
        restored = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        await session.refresh(note)
        assert restored.files_imported == 1
        assert note.deleted_at is None
        assert note.content == "keep my judgment"


@pytest.mark.asyncio
async def test_expired_note_tombstones_are_purged(app) -> None:
    async with get_sessionmaker()() as session:
        user = User(
            email="expired-note@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        paper = new_paper(title="Expired Note", dedup_key="title:expired-note")
        session.add_all([user, paper])
        await session.flush()
        expired = PaperNote(
            paper_id=paper.id,
            author_id=user.id,
            content="expired",
            deleted_at=utcnow() - timedelta(days=31),
        )
        retained = PaperNote(
            paper_id=paper.id,
            author_id=user.id,
            content="retained",
            deleted_at=utcnow() - timedelta(days=29),
        )
        session.add_all([expired, retained])
        await session.commit()
        expired_id, retained_id = expired.id, retained.id

        assert await purge_expired_notes(session) == 1
        assert await session.get(PaperNote, expired_id) is None
        assert await session.get(PaperNote, retained_id) is not None
