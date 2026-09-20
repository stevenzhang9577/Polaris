import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.obsidian_vault import (
    ObsidianVaultConnection,
    VaultConflict,
    VaultFileState,
    VaultLibraryBinding,
)
from app.models.paper import PaperNote, PaperWiki, PaperWikiRevision, new_paper
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.paper_content import PaperContentVersion
from app.models.user import User
from app.services import paper_summaries, paper_wiki
from app.services.obsidian_vault_bridge import (
    DefaultVaultDomainAdapter,
    VaultBridgeError,
    VaultWatcher,
    atomic_write_text,
    configure_connection,
    content_hash,
    library_directory,
    managed_root,
    parse_markdown_document,
    parse_notes_body,
    render_markdown_document,
    render_notes_body,
    resolve_conflict,
    safe_managed_path,
    set_library_binding,
    sync_connection,
    three_way_merge,
    validate_vault_root,
)
from tests.conftest import register_and_login


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "Research Vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "Polaris").mkdir()
    return root


def test_markdown_frontmatter_roundtrip_keeps_unicode_and_body() -> None:
    raw = render_markdown_document(
        {
            "title": "中文论文",
            "polaris_type": "summary",
            "polaris_entity_id": str(uuid.uuid4()),
        },
        "# 标题\r\n\r\n正文",
    )
    parsed = parse_markdown_document(raw)
    assert parsed.metadata["title"] == "中文论文"
    assert parsed.body == "# 标题\n\n正文\n"
    assert "polaris_type: summary" in raw


@pytest.mark.parametrize(
    ("base", "polaris", "vault", "status", "content"),
    [
        ("a\n", "a\n", "b\n", "vault", "b\n"),
        ("a\n", "b\n", "a\n", "polaris", "b\n"),
        ("a\n", "b\n", "b\n", "unchanged", "b\n"),
        (
            "one\ntwo\nthree\n",
            "ONE\ntwo\nthree\n",
            "one\ntwo\nTHREE\n",
            "merged",
            "ONE\ntwo\nTHREE\n",
        ),
        ("one\ntwo\n", "ONE\ntwo\n", "UNO\ntwo\n", "conflict", None),
    ],
)
def test_three_way_merge(
    base: str,
    polaris: str,
    vault: str,
    status: str,
    content: str | None,
) -> None:
    outcome = three_way_merge(base, polaris, vault)
    assert outcome.status == status
    assert outcome.content == content


def test_managed_path_rejects_escape_and_symlink(tmp_path: Path) -> None:
    root = _vault(tmp_path) / "Polaris"
    with pytest.raises(VaultBridgeError, match="OBSIDIAN_PATH_OUTSIDE_MANAGED_ROOT"):
        safe_managed_path(root, "../outside.md")

    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(VaultBridgeError, match="OBSIDIAN_MANAGED_PATH_SYMLINK"):
        safe_managed_path(root, "linked/paper.md")


def test_atomic_write_replaces_file_without_temp_residue(tmp_path: Path) -> None:
    root = _vault(tmp_path) / "Polaris"
    target = atomic_write_text(root, "library/papers/paper.md", "first\n")
    assert target.read_text(encoding="utf-8") == "first\n"
    atomic_write_text(root, "library/papers/paper.md", "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert list(target.parent.glob("*.tmp")) == []


def test_vault_validation_requires_existing_obsidian_marker(tmp_path: Path) -> None:
    root = tmp_path / "not-a-vault"
    root.mkdir()
    with pytest.raises(VaultBridgeError, match="OBSIDIAN_VAULT_MARKER_MISSING"):
        validate_vault_root(root)
    assert validate_vault_root(_vault(tmp_path / "valid"))


def test_note_markers_roundtrip_and_new_note_slot() -> None:
    paper = new_paper(title="Markers", dedup_key="title:markers")
    note_id = uuid.uuid4()
    note = PaperNote(
        id=note_id,
        paper_id=uuid.uuid4(),
        author_id=uuid.uuid4(),
        content="现有笔记",
    )
    body = render_notes_body(paper, [note])
    parsed, new_note = parse_notes_body(body)
    assert parsed == {note_id: "现有笔记"}
    assert new_note is None

    edited = body.replace(
        "<!-- Add one new note here. Polaris clears this block after importing it. -->",
        "新增笔记",
    )
    _parsed, new_note = parse_notes_body(edited)
    assert new_note == "新增笔记"


def test_library_directory_is_stable_and_path_safe() -> None:
    library = DirectionLibrary(id=uuid.uuid4(), name="AI / Agents: 研究")
    name = library_directory(library)
    assert "/" not in name
    assert name.endswith(library.id.hex[:8])


@pytest.mark.asyncio
async def test_conflict_listing_hides_libraries_after_management_is_revoked(
    client, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    monkeypatch.setattr(
        "app.services.obsidian_vault_bridge.watcher_running", lambda _connection_id: True
    )
    owner_token = await register_and_login(
        client, email=f"vault-list-owner-{uuid.uuid4().hex}@example.com"
    )
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    owner_id = uuid.UUID((await client.get("/api/users/me", headers=owner_headers)).json()["id"])
    replacement_token = await register_and_login(
        client, email=f"vault-list-replacement-{uuid.uuid4().hex}@example.com"
    )
    replacement_headers = {"Authorization": f"Bearer {replacement_token}"}
    replacement_id = uuid.UUID(
        (await client.get("/api/users/me", headers=replacement_headers)).json()["id"]
    )
    vault = _vault(tmp_path)

    async with get_sessionmaker()() as session:
        library = DirectionLibrary(name="Revocable Vault", submitted_by=owner_id)
        connection = ObsidianVaultConnection(
            user_id=owner_id,
            vault_path=str(vault),
            status="ready",
        )
        session.add_all([library, connection])
        await session.flush()
        binding = VaultLibraryBinding(
            connection_id=connection.id,
            library_id=library.id,
            enabled=True,
        )
        state = VaultFileState(
            connection_id=connection.id,
            library_id=library.id,
            entity_type="summary",
            entity_id=uuid.uuid4(),
            relative_path="revocable/papers/private.md",
            base_content="private base",
            base_hash=content_hash("private base"),
            polaris_hash=content_hash("private polaris"),
            vault_hash=content_hash("private vault"),
            status="conflict",
        )
        session.add_all([binding, state])
        await session.flush()
        conflict = VaultConflict(
            connection_id=connection.id,
            file_state_id=state.id,
            library_id=library.id,
            entity_type="summary",
            entity_id=state.entity_id,
            relative_path=state.relative_path,
            base_content="private base",
            polaris_content="private polaris",
            vault_content="private vault",
            status="open",
        )
        session.add(conflict)
        await session.commit()
        library_id = library.id

    before = await client.get("/api/obsidian-vault/conflicts", headers=owner_headers)
    assert before.status_code == 200, before.text
    assert before.json()[0]["vault_content"] == "private vault"
    before_state = await client.get("/api/obsidian-vault", headers=owner_headers)
    assert before_state.status_code == 200, before_state.text
    assert before_state.json()["conflict_count"] == 1
    assert len(before_state.json()["bindings"]) == 1

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        assert library is not None
        library.submitted_by = replacement_id
        await session.commit()

    after = await client.get("/api/obsidian-vault/conflicts", headers=owner_headers)
    assert after.status_code == 200, after.text
    assert after.json() == []
    assert "private polaris" not in after.text
    assert "private vault" not in after.text
    after_state = await client.get("/api/obsidian-vault", headers=owner_headers)
    assert after_state.status_code == 200, after_state.text
    assert after_state.json()["conflict_count"] == 0
    assert after_state.json()["bindings"] == []


@pytest.mark.asyncio
async def test_watcher_debounces_multiple_changes(tmp_path: Path) -> None:
    root = _vault(tmp_path) / "Polaris"
    calls = 0
    called = asyncio.Event()

    async def callback() -> None:
        nonlocal calls
        calls += 1
        called.set()

    watcher = VaultWatcher(root, callback, debounce_seconds=0.05, poll_seconds=0.01)
    watcher.start()
    try:
        for index in range(3):
            path = root / f"paper-{index}.md"
            path.write_text(str(index), encoding="utf-8")
            os.utime(path, None)
            await asyncio.sleep(0.01)
        await asyncio.wait_for(called.wait(), timeout=1)
        await asyncio.sleep(0.04)
        assert calls == 1
    finally:
        await watcher.stop()


def test_content_hash_is_deterministic() -> None:
    assert content_hash("论文") == content_hash("论文")
    assert content_hash("论文") != content_hash("论文。")


@pytest.mark.asyncio
async def test_vault_edit_inherits_current_revision_provenance_not_global_pdf(app) -> None:
    async with get_sessionmaker()() as session:
        user = User(
            email="vault-provenance@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        library_a = DirectionLibrary(name="Private A")
        library_b = DirectionLibrary(name="Private B")
        paper = new_paper(title="Shared paper", dedup_key="title:shared-paper")
        session.add_all([user, library_a, library_b, paper])
        await session.flush()
        blob_a = PdfBlob(
            sha256="a" * 64,
            byte_size=10,
            storage_key="pdf-blobs/aa/a.pdf",
        )
        blob_b = PdfBlob(
            sha256="b" * 64,
            byte_size=10,
            storage_key="pdf-blobs/bb/b.pdf",
        )
        session.add_all([blob_a, blob_b])
        await session.flush()
        asset_a = PaperAsset(
            paper_id=paper.id,
            blob_id=blob_a.id,
            source="zotero",
            state="ready",
        )
        asset_b = PaperAsset(
            paper_id=paper.id,
            blob_id=blob_b.id,
            source="zotero",
            state="ready",
        )
        session.add_all([asset_a, asset_b])
        await session.flush()
        version_a = PaperContentVersion(
            paper_id=paper.id,
            asset_id=asset_a.id,
            version_no=1,
            parser="test",
            status="ready",
            is_current=False,
        )
        version_b = PaperContentVersion(
            paper_id=paper.id,
            asset_id=asset_b.id,
            version_no=2,
            parser="test",
            status="ready",
            is_current=True,
        )
        session.add_all([version_a, version_b])
        await session.flush()
        evidence_a = {"version": 1, "refs": [{"source": "library-a"}]}
        _wiki, source_revision = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nLibrary A source.",
            source_level="fulltext",
            content_version_id=version_a.id,
            source_fingerprint="a" * 64,
            evidence_manifest=evidence_a,
            source_library_id=library_a.id,
        )
        await session.commit()

        await DefaultVaultDomainAdapter().apply_summary(
            session,
            paper=paper,
            user=user,
            content="## TL;DR\nEdited in library A's Vault.",
        )
        await session.commit()
        edited = await session.scalar(
            select(PaperWikiRevision).where(
                PaperWikiRevision.paper_id == paper.id,
                PaperWikiRevision.id != source_revision.id,
            )
        )
        assert edited is not None
        assert edited.source_level == "obsidian"
        assert edited.content_version_id == version_a.id
        assert edited.content_version_id != version_b.id
        assert edited.source_fingerprint == "a" * 64
        assert edited.evidence_manifest == evidence_a
        assert edited.source_library_id == library_a.id


@pytest.mark.asyncio
async def test_sync_imports_vault_edit_and_preserves_overlapping_conflict(
    app, tmp_path: Path
) -> None:
    vault = _vault(tmp_path)
    async with get_sessionmaker()() as session:
        user = User(
            email="vault@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        session.add(user)
        await session.flush()
        library = DirectionLibrary(name="Agent Research", submitted_by=user.id)
        paper = new_paper(title="Editable Paper", dedup_key="title:editable-paper")
        session.add_all([library, paper])
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=library.id,
                paper_id=paper.id,
                status="compiled",
            )
        )
        session.add(
            PaperWiki(
                paper_id=paper.id,
                content="## TL;DR\nbase summary\n",
                model="legacy-model",
                compiled_by=user.id,
            )
        )
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
        first = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert first.files_written == 3  # summary + empty editable notes + library index
        notes_state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "notes")
        )
        assert notes_state is not None
        notes_path = safe_managed_path(managed_root(vault), notes_state.relative_path)
        assert "polaris-new:start" in notes_path.read_text(encoding="utf-8")

        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "summary")
        )
        assert state is not None
        root = managed_root(vault)
        path = safe_managed_path(root, state.relative_path)
        original_file = path.read_text(encoding="utf-8")
        path.unlink()
        deleted = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert deleted.files_deleted == 1
        deleted_wiki = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == paper.id)
        )
        assert deleted_wiki is not None and deleted_wiki.deleted_at is not None
        again = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert not path.exists()
        assert again.files_written == 0

        # Putting a valid managed file back inside the retention window restores the summary and
        # records a fresh Obsidian revision instead of hard-deleting/recreating the Paper.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original_file, encoding="utf-8")
        restored = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert restored.files_imported == 1
        restored_wiki = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == paper.id)
        )
        assert restored_wiki is not None and restored_wiki.deleted_at is None

        document = parse_markdown_document(path.read_text(encoding="utf-8"))
        edited = document.body.replace("base summary", "vault edit")
        path.write_text(
            render_markdown_document(document.metadata, edited), encoding="utf-8"
        )

        imported = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert imported.files_imported == 1
        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        assert wiki is not None
        assert "vault edit" in wiki.content
        assert wiki.model == "obsidian"
        assert wiki.current_revision_id is not None
        canonical_document = parse_markdown_document(path.read_text(encoding="utf-8"))
        assert canonical_document.metadata["polaris_revision_id"] == str(
            wiki.current_revision_id
        )
        assert canonical_document.metadata["polaris_model"] == "obsidian"
        assert canonical_document.metadata["polaris_source_level"] == "obsidian"

        await paper_wiki.upsert_wiki(
            session,
            paper=paper,
            content="## TL;DR\npolaris edit\n",
            model="test-model",
            compiled_by=user.id,
        )
        await session.commit()
        current_document = parse_markdown_document(path.read_text(encoding="utf-8"))
        path.write_text(
            render_markdown_document(
                current_document.metadata,
                current_document.body.replace("vault edit", "second vault edit"),
            ),
            encoding="utf-8",
        )

        conflicted = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert conflicted.conflicts == 1
        conflict = await session.scalar(select(VaultConflict))
        assert conflict is not None
        assert "polaris edit" in conflict.polaris_content
        assert "second vault edit" in conflict.vault_content
        assert path.read_text(encoding="utf-8").find("second vault edit") > 0
        companions = list((root / library_directory(library) / "conflicts").glob("*.md"))
        assert len(companions) == 1

        intruder = User(
            email="vault-conflict-intruder@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        session.add(intruder)
        await session.flush()
        with pytest.raises(VaultBridgeError, match="OBSIDIAN_CONFLICT_NOT_FOUND"):
            await resolve_conflict(
                session,
                conflict=conflict,
                strategy="vault",
                user=intruder,
            )

        # An open conflict remains live: deleting the Vault side updates the same conflict, and
        # choosing Vault applies the deletion instead of attempting an invalid empty summary.
        path.unlink()
        refreshed = await sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert refreshed.conflicts == 1
        await session.refresh(conflict)
        assert conflict.vault_content == ""
        await resolve_conflict(
            session,
            conflict=conflict,
            strategy="vault",
            user=user,
        )
        await session.commit()
        await session.refresh(state)
        assert state.status == "deleted"
        assert not path.exists()
        deleted_after_conflict = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == paper.id)
        )
        assert deleted_after_conflict is not None
        assert deleted_after_conflict.deleted_at is not None


@pytest.mark.asyncio
async def test_summary_delete_conflict_refreshes_and_resolves_without_losing_vault_edit(
    app, tmp_path: Path
) -> None:
    vault = _vault(tmp_path)
    async with get_sessionmaker()() as session:
        user = User(
            email="vault-delete-conflict@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        session.add(user)
        await session.flush()
        library = DirectionLibrary(name="Delete Conflict", submitted_by=user.id)
        paper = new_paper(title="Do Not Lose This Edit", dedup_key="title:delete-conflict")
        session.add_all([library, paper])
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=library.id,
                paper_id=paper.id,
                status="compiled",
            )
        )
        await paper_wiki.upsert_wiki(
            session,
            paper=paper,
            content="## TL;DR\nbase summary\n",
            model="test-model",
            compiled_by=user.id,
        )
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
            select(VaultFileState).where(
                VaultFileState.connection_id == connection.id,
                VaultFileState.entity_type == "summary",
                VaultFileState.entity_id == paper.id,
            )
        )
        assert state is not None
        root = managed_root(vault)
        path = safe_managed_path(root, state.relative_path)
        document = parse_markdown_document(path.read_text(encoding="utf-8"))
        first_vault_edit = document.body.replace(
            "base summary", "vault edit before deletion"
        )
        path.write_text(
            render_markdown_document(document.metadata, first_vault_edit),
            encoding="utf-8",
        )

        await paper_summaries.soft_delete_summary(session, paper=paper)
        await session.commit()
        conflicted = await sync_connection(session, connection=connection, user=user)
        await session.commit()

        assert conflicted.conflicts == 1
        assert path.is_file()
        assert "vault edit before deletion" in path.read_text(encoding="utf-8")
        conflict = await session.scalar(
            select(VaultConflict).where(
                VaultConflict.file_state_id == state.id,
                VaultConflict.status == "open",
            )
        )
        assert conflict is not None
        assert conflict.polaris_content == ""
        assert "vault edit before deletion" in conflict.vault_content
        companions = list((root / library_directory(library) / "conflicts").glob("*.md"))
        assert len(companions) == 1
        companion = companions[0]

        current_document = parse_markdown_document(path.read_text(encoding="utf-8"))
        second_vault_edit = current_document.body.replace(
            "vault edit before deletion", "vault edit after conflict"
        )
        path.write_text(
            render_markdown_document(current_document.metadata, second_vault_edit),
            encoding="utf-8",
        )
        refreshed = await sync_connection(session, connection=connection, user=user)
        await session.commit()

        assert refreshed.conflicts == 1
        await session.refresh(conflict)
        assert conflict.polaris_content == ""
        assert "vault edit after conflict" in conflict.vault_content
        assert "vault edit after conflict" in companion.read_text(encoding="utf-8")
        assert (
            await session.scalar(
                select(VaultConflict).where(
                    VaultConflict.file_state_id == state.id,
                    VaultConflict.status == "open",
                )
            )
        ).id == conflict.id

        await resolve_conflict(
            session,
            conflict=conflict,
            strategy="polaris",
            user=user,
        )
        await session.commit()

        await session.refresh(state)
        await session.refresh(conflict)
        assert state.status == "deleted"
        assert state.deleted_at is not None
        assert conflict.status == "resolved"
        assert conflict.resolution == "polaris"
        assert not path.exists()
        assert not companion.exists()
        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        assert wiki is not None and wiki.deleted_at is not None
