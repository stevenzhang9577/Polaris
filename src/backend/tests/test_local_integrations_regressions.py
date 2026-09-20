"""Regression cases from the local integrations adversarial audit."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.obsidian_vault import VaultConflict, VaultFileState
from app.models.paper import PaperWiki, new_paper
from app.models.user import User
from app.services import obsidian_vault_bridge as bridge
from app.services import paper_wiki
from app.services.zotero_local import _find_paper_for_zotero


async def setup_vault(session, tmp_path):
    vault = tmp_path / "Vault"
    (vault / ".obsidian").mkdir(parents=True)
    user = User(
        email="audit@example.com",
        hashed_password="test",
        is_active=True,
        is_verified=True,
        is_superuser=False,
    )
    session.add(user)
    await session.flush()
    library = DirectionLibrary(name="Audit", submitted_by=user.id)
    paper = new_paper(title="Audit paper", dedup_key="title:audit")
    session.add_all([library, paper])
    await session.flush()
    session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, status="compiled"))
    await paper_wiki.upsert_wiki(
        session, paper=paper, content="## TL;DR\nbase\n", compiled_by=user.id
    )
    connection = await bridge.configure_connection(session, user_id=user.id, vault_path=str(vault))
    await bridge.set_library_binding(
        session, connection_id=connection.id, library_id=library.id, enabled=True
    )
    await bridge.sync_connection(session, connection=connection, user=user)
    await session.commit()
    return vault, user, paper, connection


@pytest.mark.asyncio
async def test_new_note_is_given_stable_id_before_next_edit(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "notes")
        )
        root = bridge.managed_root(vault)
        path = bridge.safe_managed_path(root, state.relative_path)
        original = path.read_text()
        bridge.atomic_write_text(
            root,
            state.relative_path,
            original.replace(
                "<!-- Add one new note here. Polaris clears this block after importing it. -->",
                "first note",
            ),
        )
        await bridge.sync_connection(session, connection=connection, user=user)
        await session.commit()
        imported = bridge.parse_markdown_document(path.read_text())
        blocks, new_note = bridge.parse_notes_body(imported.body)
        assert new_note is None and list(blocks.values()) == ["first note"], (blocks, new_note)
        again = await bridge.sync_connection(session, connection=connection, user=user)
        assert again.files_imported == 0
        assert (
            bridge.parse_notes_body(bridge.parse_markdown_document(path.read_text()).body)[0]
            == blocks
        )


@pytest.mark.asyncio
async def test_resolving_conflict_does_not_overwrite_newer_vault_content(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "summary")
        )
        root = bridge.managed_root(vault)
        path = bridge.safe_managed_path(root, state.relative_path)
        doc = bridge.parse_markdown_document(path.read_text())
        await paper_wiki.upsert_wiki(
            session, paper=paper, content="## TL;DR\npolaris edit\n", compiled_by=user.id
        )
        bridge.atomic_write_text(
            root,
            state.relative_path,
            bridge.render_markdown_document(doc.metadata, "## TL;DR\nvault edit\n"),
        )
        await bridge.sync_connection(session, connection=connection, user=user)
        await session.commit()
        conflict = await session.scalar(select(VaultConflict).where(VaultConflict.status == "open"))
        assert conflict is not None
        bridge.atomic_write_text(
            root,
            state.relative_path,
            bridge.render_markdown_document(doc.metadata, "## TL;DR\nnew unsynced edit\n"),
        )
        with pytest.raises(bridge.VaultBridgeError, match="OBSIDIAN_CONFLICT_CHANGED"):
            await bridge.resolve_conflict(session, conflict=conflict, strategy="vault", user=user)
        await session.commit()
        assert "new unsynced edit" in path.read_text()
        assert "new unsynced edit" in conflict.vault_content
        with pytest.raises(bridge.VaultBridgeError, match="OBSIDIAN_CONFLICT_CHANGED"):
            await bridge.resolve_conflict(
                session,
                conflict=conflict,
                strategy="vault",
                user=user,
                expected_version="0" * 64,
            )
        await bridge.resolve_conflict(
            session,
            conflict=conflict,
            strategy="vault",
            user=user,
            expected_version=conflict.version,
        )
        assert "new unsynced edit" in path.read_text()


@pytest.mark.asyncio
async def test_title_fallback_matches_existing_identifier_backed_paper(app):
    async with get_sessionmaker()() as session:
        paper = new_paper(
            title="A Shared Title",
            doi="10.1234/example",
            dedup_key="doi:10.1234/example",
            year=2025,
            authors=[{"name": "Alice"}],
        )
        session.add(paper)
        await session.flush()
        found = await _find_paper_for_zotero(
            session,
            {
                "doi": None,
                "arxiv_id": None,
                "title": "A Shared Title",
                "year": 2025,
                "authors": [{"name": "Alice"}],
            },
        )
        assert found is not None and found.id == paper.id


@pytest.mark.asyncio
async def test_vault_restore_rejects_expired_summary(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "summary")
        )
        root = bridge.managed_root(vault)
        path = bridge.safe_managed_path(root, state.relative_path)
        saved = path.read_text()
        path.unlink()
        await bridge.sync_connection(session, connection=connection, user=user)
        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        wiki.deleted_at = utcnow() - timedelta(days=31)
        state.deleted_at = utcnow() - timedelta(days=31)
        await session.commit()
        bridge.atomic_write_text(root, state.relative_path, saved)
        await bridge.sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert wiki.deleted_at is not None


@pytest.mark.asyncio
async def test_unchanged_conflict_does_not_rewrite_watched_file(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "summary")
        )
        root = bridge.managed_root(vault)
        doc = bridge.parse_markdown_document(
            bridge.safe_managed_path(root, state.relative_path).read_text()
        )
        await paper_wiki.upsert_wiki(
            session, paper=paper, content="## TL;DR\npolaris edit\n", compiled_by=user.id
        )
        bridge.atomic_write_text(
            root,
            state.relative_path,
            bridge.render_markdown_document(doc.metadata, "## TL;DR\nvault edit\n"),
        )
        await bridge.sync_connection(session, connection=connection, user=user)
        await session.commit()
        snapshot = bridge.VaultWatcher(root, lambda: None)._snapshot()
        companion = next(root.glob("*/conflicts/*.md"))
        modified = companion.stat().st_mtime_ns
        await bridge.sync_connection(session, connection=connection, user=user)
        await session.commit()
        assert bridge.VaultWatcher(root, lambda: None)._snapshot() == snapshot
        assert companion.stat().st_mtime_ns == modified


@pytest.mark.asyncio
async def test_connection_only_refresh_preserves_existing_route_destination(app, monkeypatch):
    from app.core.config import get_settings
    from app.models.llm_config import LLMProviderConfig, ModelRoute
    from app.services import llm_local_config as lc

    monkeypatch.setattr(get_settings(), "profile", "desktop")
    config = lc.DiscoveredConfig(
        source="claude_code",
        source_key="default",
        display_name="Claude Code",
        kind="anthropic",
        transport="anthropic_messages",
        auth_scheme="x_api_key",
        endpoint_origin="https://new-provider.example",
        models=["new-model"],
        default_model="new-model",
        effort=None,
        credential_status="available",
        importable=True,
        warnings=[],
        fingerprint="new",
        base_url="https://new-provider.example",
        api_key="test-key",
    )
    monkeypatch.setattr(lc, "discover_from_disk", lambda: lc.DiscoveryResult([config], []))

    async def probe(*args):
        return True, 1, None

    monkeypatch.setattr(lc.llm_admin, "test_model", probe)
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(
            name="imported",
            kind="anthropic",
            transport="anthropic_messages",
            auth_scheme="x_api_key",
            base_url="https://old-provider.example",
            import_source="claude_code",
            import_source_key="default",
        )
        session.add(provider)
        await session.flush()
        route = ModelRoute(stage="agent", provider_id=provider.id, model="old-model")
        session.add(route)
        await session.commit()
        await lc.import_local_config(
            session,
            owner_id=None,
            source="claude_code",
            source_key="default",
            stages=[],
            overwrite_routes=False,
        )
        await session.refresh(provider)
        assert provider.base_url == "https://old-provider.example"
        await session.refresh(route)
        assert route.provider_id == provider.id
        imported = await lc.import_local_config(
            session,
            owner_id=None,
            source="claude_code",
            source_key="default",
            stages=["agent"],
            overwrite_routes=True,
        )
        await session.refresh(route)
        assert route.provider_id == imported.provider.id != provider.id
        assert route.model == "new-model"


@pytest.mark.asyncio
async def test_summary_context_can_reach_newer_zotero_library(app, tmp_path):
    from app.api.papers import _get_summary_writable_paper
    from app.models.zotero_local import ZoteroLocalBinding

    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        zotero_library = DirectionLibrary(name="Zotero", submitted_by=user.id)
        session.add(zotero_library)
        await session.flush()
        session.add(
            LibraryPaper(library_id=zotero_library.id, paper_id=paper.id, status="included")
        )
        session.add(
            ZoteroLocalBinding(
                library_id=zotero_library.id,
                created_by=user.id,
                collection_key="AUDIT123",
                collection_name="Audit",
            )
        )
        await session.commit()
        view = await _get_summary_writable_paper(session, paper.id, user)
        assert view.library_id == zotero_library.id
        original_library_id = await session.scalar(
            select(LibraryPaper.library_id).where(
                LibraryPaper.paper_id == paper.id, LibraryPaper.library_id != zotero_library.id
            )
        )
        view = await _get_summary_writable_paper(
            session, paper.id, user, library_id=original_library_id
        )
        assert view.library_id == original_library_id
        from fastapi import HTTPException

        foreign_library = DirectionLibrary(name="Not a member", submitted_by=user.id)
        session.add(foreign_library)
        await session.flush()
        with pytest.raises(HTTPException) as denied:
            await _get_summary_writable_paper(
                session, paper.id, user, library_id=foreign_library.id
            )
        assert denied.value.status_code == 404


def test_upgrade_backfills_existing_wiki_rows(tmp_path):
    import uuid

    from sqlalchemy import create_engine, text

    from alembic import command
    from app.models.paper import Paper
    from tests.test_migrations import PRE_ZOTERO_REVISION, _make_config

    db_path = tmp_path / "existing-wiki.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, PRE_ZOTERO_REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    paper_id, wiki_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            Paper.__table__.insert().values(
                id=paper_id,
                title="Existing paper",
                source="manual",
                dedup_key="title:migration-audit",
            )
        )
        conn.execute(
            text(
                "INSERT INTO paper_wikis (id, paper_id, content, created_at, updated_at) "
                "VALUES (:id, :pid, :content, :now, :now)"
            ),
            {
                "id": wiki_id.hex,
                "pid": paper_id.hex,
                "content": "## TL;DR\nExisting summary",
                "now": "2026-09-19 12:00:00",
            },
        )
    engine.dispose()
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        revision = conn.execute(
            text(
                "SELECT r.content, r.source_level FROM paper_wikis w "
                "JOIN paper_wiki_revisions r ON r.id = w.current_revision_id"
            )
        ).one()
        assert revision == ("## TL;DR\nExisting summary", "legacy")
    command.downgrade(cfg, PRE_ZOTERO_REVISION)
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM paper_wiki_revisions")) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_conflict_rechecks_file_after_awaiting_domain_write(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await session.scalar(
            select(VaultFileState).where(VaultFileState.entity_type == "summary")
        )
        root = bridge.managed_root(vault)
        path = bridge.safe_managed_path(root, state.relative_path)
        metadata = bridge.parse_markdown_document(path.read_text()).metadata
        await paper_wiki.upsert_wiki(
            session, paper=paper, content="## TL;DR\npolaris\n", compiled_by=user.id
        )
        bridge.atomic_write_text(
            root,
            state.relative_path,
            bridge.render_markdown_document(metadata, "## TL;DR\nvault\n"),
        )
        await bridge.sync_connection(session, connection=connection, user=user)
        conflict = await session.scalar(select(VaultConflict))

        class ConcurrentEdit(bridge.DefaultVaultDomainAdapter):
            async def apply_summary(self, session, *, paper, user, content):
                await super().apply_summary(session, paper=paper, user=user, content=content)
                bridge.atomic_write_text(
                    root,
                    state.relative_path,
                    bridge.render_markdown_document(
                        metadata, "## TL;DR\nedited during resolution\n"
                    ),
                )

        paper_id = paper.id
        with pytest.raises(bridge.VaultBridgeError, match="OBSIDIAN_CONFLICT_CHANGED"):
            await bridge.resolve_conflict(
                session, conflict=conflict, strategy="vault", user=user, adapter=ConcurrentEdit()
            )
        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper_id))
        assert wiki.content == "## TL;DR\npolaris\n"
        assert "edited during resolution" in path.read_text()


@pytest.mark.asyncio
async def test_title_fallback_handles_unicode_and_rejects_ambiguous_matches(app):
    async with get_sessionmaker()() as session:
        paper = new_paper(title="模型 评测", doi="10.1234/a", dedup_key="doi:10.1234/a")
        unrelated = new_paper(title="模型 训练", doi="10.1234/b", dedup_key="doi:10.1234/b")
        session.add_all([paper, unrelated])
        await session.flush()
        fields = {"title": "模型 评测", "year": None, "authors": [], "doi": None, "arxiv_id": None}
        assert (await _find_paper_for_zotero(session, fields)).id == paper.id
        session.add(new_paper(title="模型 评测", doi="10.1234/c", dedup_key="doi:10.1234/c"))
        await session.flush()
        assert await _find_paper_for_zotero(session, fields) is None


@pytest.mark.asyncio
async def test_6000_metadata_items_deduplicate_and_incremental_sync_fetches_no_bodies(
    app, monkeypatch
):
    import uuid

    import httpx
    from sqlalchemy import func, insert

    from app.core.config import get_settings
    from app.models.paper import Paper
    from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
    from app.services.zotero_local import ZoteroLocalClient, sync_binding

    monkeypatch.setattr(get_settings(), "profile", "desktop")
    count = 6000
    keys = [f"K{index:07d}" for index in range(count)]
    versions = {key: 1 for key in keys}
    fetched = 0

    def handler(request):
        nonlocal fetched
        path = request.url.path
        if path == "/api/":
            return httpx.Response(200, text="Local API", request=request)
        if path.endswith("/collections"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "ROOT",
                        "version": 1,
                        "data": {"name": "Scale"},
                    }
                ],
                request=request,
            )
        if path.endswith("/items/top"):
            start = int(request.url.params.get("start", "0"))
            page = {key: versions[key] for key in keys[start : start + 100]}
            return httpx.Response(
                200, json=page, headers={"Total-Results": str(count)}, request=request
            )
        if path.endswith("/items"):
            batch = request.url.params["itemKey"].split(",")
            fetched += len(batch)
            return httpx.Response(
                200,
                json=[
                    {
                        "key": key,
                        "version": 1,
                        "data": {
                            "itemType": "journalArticle",
                            "title": f"Scale paper {int(key[1:])}",
                        },
                    }
                    for key in batch
                ],
                request=request,
            )
        raise AssertionError(f"Metadata sync must not request PDF data: {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        local = ZoteroLocalClient(client=http)
        async with get_sessionmaker()() as session:
            await session.execute(
                insert(Paper),
                [
                    {
                        "id": uuid.uuid4(),
                        "title": f"Scale paper {index}",
                        "source": "manual",
                        "doi": f"10.1234/scale-{index}",
                        "dedup_key": f"doi:10.1234/scale-{index}",
                    }
                    for index in range(count)
                ],
            )
            library = DirectionLibrary(name="Scale")
            session.add(library)
            await session.flush()
            binding = ZoteroLocalBinding(
                library_id=library.id, collection_key="ROOT", collection_name="Scale"
            )
            session.add(binding)
            await session.commit()
            first = await sync_binding(session, binding=binding, requested_by=None, client=local)
            assert (first.existing, first.created, first.failed) == (count, 0, 0)
            assert await session.scalar(select(func.count()).select_from(Paper)) == count
            assert await session.scalar(select(func.count()).select_from(ZoteroItemLink)) == count
            assert fetched == count
            second = await sync_binding(session, binding=binding, requested_by=None, client=local)
            assert second.processed == count and second.failed == 0
            assert fetched == count
