"""Zotero Desktop Local API adapter, durable sync, and lazy PDF materialization."""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import new_paper
from app.models.paper_assets import PaperAsset
from app.models.user import User
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding, ZoteroSyncRun
from app.services.dedup import pool_dedup_key
from app.services.zotero_local import (
    ZoteroLocalClient,
    ZoteroLocalError,
    collection_keys_for_binding,
    materialize_paper_pdf,
    recover_interrupted_sync_runs,
    sync_binding,
)


def _response(request: httpx.Request, *, json=None, text=None, headers=None, status=200):
    return httpx.Response(status, json=json, text=text, headers=headers, request=request)


async def test_client_probe_collections_versions_and_recursive_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return _response(
                request,
                text="Nothing to see here.",
                headers={
                    "Content-Type": "text/plain",
                    "X-Zotero-Version": "10.0.3",
                    "Zotero-API-Version": "3",
                    "Zotero-Server-ID": "instance-a",
                },
            )
        if request.url.path.endswith("/collections"):
            return _response(
                request,
                json=[
                    {
                        "key": "ROOT",
                        "version": 8,
                        "data": {"name": "Root", "parentCollection": False},
                    },
                    {
                        "key": "CHILD",
                        "version": 9,
                        "data": {"name": "Child", "parentCollection": "ROOT"},
                    },
                ],
                headers={"Total-Results": "2"},
            )
        if request.url.path.endswith("/collections/ROOT/items/top"):
            assert request.url.params["format"] == "versions"
            assert request.url.params["itemType"] == "-attachment"
            return _response(
                request,
                json={"ITEMA": 11},
                headers={"Total-Results": "1", "Last-Modified-Version": "42"},
            )
        if request.url.path.endswith("/items/ATTACH/file/view/url"):
            return _response(
                request,
                status=302,
                headers={"Location": "file:///tmp/Zotero%20Paper.pdf"},
            )
        raise AssertionError(str(request.url))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ZoteroLocalClient(base_url="http://127.0.0.1:23119/api", client=http)
    probe = await client.probe()
    assert (probe.api_version, probe.zotero_version, probe.instance_id) == (
        3,
        "10.0.3",
        "instance-a",
    )
    collections = await client.collections()
    assert collections[0].parent_key is None
    assert collections[0].child_count == 1
    assert collection_keys_for_binding(collections, "ROOT") == ["ROOT", "CHILD"]
    snapshot = await client.collection_item_versions("ROOT")
    assert snapshot.versions == {"ITEMA": 11}
    assert snapshot.library_version == 42
    assert await client.attachment_file_url("ATTACH") == "file:///tmp/Zotero%20Paper.pdf"
    await http.aclose()


@pytest.mark.asyncio
async def test_sync_deduplicates_archives_only_owned_membership_and_restores(
    app, monkeypatch
):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    owner_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        owner = User(
            id=owner_id,
            email="zotero-local@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
            display_name="Owner",
        )
        session.add(owner)
        await session.flush()
        library = DirectionLibrary(name="Local Zotero", submitted_by=owner_id)
        existing = new_paper(
            source="manual",
            dedup_key=pool_dedup_key(
                arxiv_id=None, doi="10.1000/existing", title="Existing paper"
            ),
            title="Existing paper",
            doi="10.1000/existing",
        )
        session.add_all([library, existing])
        await session.flush()
        manual_membership = LibraryPaper(
            library_id=library.id, paper_id=existing.id, status="included"
        )
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=owner.id,
            collection_key="ROOT",
            collection_name="Root",
        )
        session.add_all([manual_membership, binding])
        await session.commit()
        library_id, binding_id, existing_id = library.id, binding.id, existing.id

    state = {"keys": {"NEW": 3, "EXISTING": 4, "IGNORED": 1}}

    items = {
        "NEW": {
            "key": "NEW",
            "version": 3,
            "data": {
                "key": "NEW",
                "itemType": "journalArticle",
                "title": "A New Zotero Paper",
                "DOI": "10.1000/new",
                "date": "2025-04-02",
                "creators": [
                    {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
                ],
            },
        },
        "EXISTING": {
            "key": "EXISTING",
            "version": 4,
            "data": {
                "key": "EXISTING",
                "itemType": "conferencePaper",
                "title": "A Different Zotero Title",
                "DOI": "10.1000/EXISTING",
                "date": "2024",
                "creators": [],
            },
        },
        "IGNORED": {
            "key": "IGNORED",
            "version": 1,
            "data": {"key": "IGNORED", "itemType": "webpage", "title": "Not a paper"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return _response(
                request,
                text="Nothing to see here.",
                headers={"Zotero-Server-ID": "instance-a"},
            )
        if request.url.path.endswith("/collections"):
            return _response(
                request,
                json=[
                    {"key": "ROOT", "version": 1, "data": {"name": "Root"}},
                    {
                        "key": "CHILD",
                        "version": 1,
                        "data": {"name": "Child", "parentCollection": "ROOT"},
                    },
                ],
                headers={"Total-Results": "2"},
            )
        if request.url.path.endswith("/collections/ROOT/items/top"):
            return _response(
                request,
                json=state["keys"],
                headers={
                    "Total-Results": str(len(state["keys"])),
                    "Last-Modified-Version": "20",
                },
            )
        if request.url.path.endswith("/collections/CHILD/items/top"):
            return _response(
                request,
                json={},
                headers={"Total-Results": "0", "Last-Modified-Version": "20"},
            )
        if request.url.path.endswith("/items"):
            keys = request.url.params.get("itemKey", "").split(",")
            return _response(request, json=[items[key] for key in keys if key in items])
        raise AssertionError(str(request.url))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    local = ZoteroLocalClient(client=http)
    async with get_sessionmaker()() as session:
        binding = await session.get(ZoteroLocalBinding, binding_id)
        first = await sync_binding(
            session, binding=binding, requested_by=owner_id, client=local
        )
        assert (first.created, first.existing, first.ignored, first.failed) == (1, 1, 1, 0)
        links = (
            (
                await session.execute(
                    select(ZoteroItemLink).where(ZoteroItemLink.binding_id == binding_id)
                )
            )
            .scalars()
            .all()
        )
        by_key = {link.item_key: link for link in links}
        assert by_key["NEW"].membership_created_by_sync is True
        assert by_key["EXISTING"].paper_id == existing_id
        assert by_key["EXISTING"].membership_created_by_sync is False
        new_paper_id = by_key["NEW"].paper_id

        state["keys"] = {}
        second = await sync_binding(
            session, binding=binding, requested_by=owner_id, full=True, client=local
        )
        assert second.missing == 3
        new_membership = await session.scalar(
            select(LibraryPaper).where(
                LibraryPaper.library_id == library_id,
                LibraryPaper.paper_id == new_paper_id,
            )
        )
        manual_membership_row = await session.scalar(
            select(LibraryPaper).where(
                LibraryPaper.library_id == library_id,
                LibraryPaper.paper_id == existing_id,
            )
        )
        assert (new_membership.status, new_membership.trash_reason) == (
            "excluded",
            "zotero_removed",
        )
        assert manual_membership_row.status == "included"

        state["keys"] = {"NEW": 3}
        third = await sync_binding(
            session, binding=binding, requested_by=owner_id, client=local
        )
        assert third.updated == 1
        await session.refresh(new_membership)
        assert (new_membership.status, new_membership.trash_reason) == ("included", None)
    await http.aclose()


@pytest.mark.asyncio
async def test_removing_one_of_two_links_to_same_paper_keeps_membership(app, monkeypatch):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    owner_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        owner = User(
            id=owner_id,
            email="zotero-duplicate-links@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        session.add(owner)
        await session.flush()
        library = DirectionLibrary(name="Duplicate links", submitted_by=owner_id)
        session.add(library)
        await session.flush()
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=owner.id,
            collection_key="ROOT",
            collection_name="Root",
        )
        session.add(binding)
        await session.commit()
        library_id, binding_id = library.id, binding.id

    state = {"keys": {"PRIMARY": 1, "DUPLICATE": 1}}
    items = {
        key: {
            "key": key,
            "version": 1,
            "data": {
                "key": key,
                "itemType": "journalArticle",
                "title": f"Duplicate record {key}",
                "DOI": "10.1000/shared-record",
                "creators": [],
            },
        }
        for key in state["keys"]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return _response(request, text="OK", headers={"Zotero-Server-ID": "instance-a"})
        if request.url.path.endswith("/collections"):
            return _response(
                request,
                json=[{"key": "ROOT", "version": 1, "data": {"name": "Root"}}],
                headers={"Total-Results": "1"},
            )
        if request.url.path.endswith("/collections/ROOT/items/top"):
            return _response(
                request,
                json=state["keys"],
                headers={
                    "Total-Results": str(len(state["keys"])),
                    "Last-Modified-Version": "2",
                },
            )
        if request.url.path.endswith("/items"):
            keys = request.url.params.get("itemKey", "").split(",")
            return _response(request, json=[items[key] for key in keys if key in items])
        raise AssertionError(str(request.url))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    local = ZoteroLocalClient(client=http)
    async with get_sessionmaker()() as session:
        binding = await session.get(ZoteroLocalBinding, binding_id)
        first = await sync_binding(
            session, binding=binding, requested_by=owner_id, client=local
        )
        assert (first.created, first.existing) == (1, 1)
        links = list(
            (
                await session.execute(
                    select(ZoteroItemLink).where(ZoteroItemLink.binding_id == binding_id)
                )
            ).scalars()
        )
        by_key = {link.item_key: link for link in links}
        assert by_key["PRIMARY"].paper_id == by_key["DUPLICATE"].paper_id
        assert by_key["PRIMARY"].membership_created_by_sync is True
        assert by_key["DUPLICATE"].membership_created_by_sync is False

        state["keys"] = {"DUPLICATE": 1}
        second = await sync_binding(
            session, binding=binding, requested_by=owner_id, client=local
        )
        assert second.missing == 1
        membership = await session.scalar(
            select(LibraryPaper).where(
                LibraryPaper.library_id == library_id,
                LibraryPaper.paper_id == by_key["PRIMARY"].paper_id,
            )
        )
        await session.refresh(by_key["PRIMARY"])
        assert by_key["PRIMARY"].status == "missing"
        assert membership.status == "included"
        assert membership.trash_reason is None

        state["keys"] = {}
        third = await sync_binding(
            session, binding=binding, requested_by=owner_id, client=local
        )
        assert third.missing == 1
        await session.refresh(by_key["DUPLICATE"])
        await session.refresh(membership)
        assert by_key["DUPLICATE"].status == "missing"
        assert (membership.status, membership.trash_reason) == (
            "excluded",
            "zotero_removed",
        )
    await http.aclose()


@pytest.mark.asyncio
async def test_sync_rejects_a_different_zotero_instance_before_reconciliation(
    app, monkeypatch
):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    owner_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        owner = User(
            id=owner_id,
            email="zotero-instance-mismatch@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        session.add(owner)
        await session.flush()
        library = DirectionLibrary(name="Bound instance", submitted_by=owner_id)
        session.add(library)
        await session.flush()
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=owner.id,
            collection_key="ROOT",
            collection_name="Root",
            zotero_instance_id="instance-a",
        )
        session.add(binding)
        await session.commit()
        binding_id = binding.id

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/"
        return _response(request, text="OK", headers={"Zotero-Server-ID": "instance-b"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    local = ZoteroLocalClient(client=http)
    async with get_sessionmaker()() as session:
        binding = await session.get(ZoteroLocalBinding, binding_id)
        with pytest.raises(ZoteroLocalError) as caught:
            await sync_binding(
                session, binding=binding, requested_by=owner_id, client=local
            )
        assert caught.value.code == "ZOTERO_INSTANCE_MISMATCH"

        await session.refresh(binding)
        run = await session.scalar(
            select(ZoteroSyncRun)
            .where(ZoteroSyncRun.binding_id == binding_id)
            .order_by(ZoteroSyncRun.created_at.desc())
        )
        assert run.status == "failed"
        assert run.processed == 0
        assert run.error_samples[-1]["error"].endswith("ZOTERO_INSTANCE_MISMATCH")
        assert binding.status == "error"
        assert binding.zotero_instance_id == "instance-a"
    await http.aclose()


@pytest.mark.asyncio
async def test_recover_interrupted_sync_runs_requeues_and_resets_bindings(app):
    owner_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        owner = User(
            id=owner_id,
            email="zotero-recovery@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        session.add(owner)
        await session.flush()
        library = DirectionLibrary(name="Recovery", submitted_by=owner_id)
        session.add(library)
        await session.flush()
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=owner.id,
            collection_key="ROOT",
            collection_name="Root",
            status="syncing",
            last_error="worker stopped",
        )
        session.add(binding)
        await session.flush()
        run = ZoteroSyncRun(
            binding_id=binding.id,
            requested_by=owner.id,
            status="running",
            full=False,
            error_samples=[],
        )
        session.add(run)
        await session.commit()

        assert await recover_interrupted_sync_runs(session) == 1
        await session.refresh(run)
        await session.refresh(binding)
        assert run.status == "queued"
        assert run.finished_at is None
        assert binding.status == "idle"
        assert binding.last_error is None


@pytest.mark.asyncio
async def test_materialize_paper_pdf_creates_zotero_asset_and_queued_version(
    app, monkeypatch, tmp_path
):
    import pymupdf

    monkeypatch.setattr(get_settings(), "profile", "desktop")
    pdf_path = tmp_path / "中文 paper.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Zotero local PDF")
    document.save(pdf_path)
    document.close()
    updated_pdf_path = tmp_path / "中文 paper updated.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Updated Zotero local PDF")
    document.save(updated_pdf_path)
    document.close()

    owner_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        owner = User(
            id=owner_id,
            email="zotero-pdf@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        session.add(owner)
        await session.flush()
        library = DirectionLibrary(name="PDF library", submitted_by=owner_id)
        paper = new_paper(
            source="zotero",
            dedup_key="doi:10.1000/pdf",
            title="PDF paper",
            doi="10.1000/pdf",
        )
        session.add_all([library, paper])
        await session.flush()
        membership = LibraryPaper(library_id=library.id, paper_id=paper.id, status="included")
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=owner.id,
            collection_key="ROOT",
            collection_name="Root",
        )
        session.add_all([membership, binding])
        await session.flush()
        session.add(
            ZoteroItemLink(
                binding_id=binding.id,
                item_key="PARENT",
                item_version=1,
                paper_id=paper.id,
                item_type="journalArticle",
                status="active",
                membership_created_by_sync=True,
            )
        )
        await session.commit()
        library_id, paper_id = library.id, paper.id

    state = {"attachment_version": 7, "pdf_path": pdf_path}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/items/PARENT/children"):
            return _response(
                request,
                json=[
                    {
                        "key": "ATTACH",
                        "version": state["attachment_version"],
                        "data": {
                            "key": "ATTACH",
                            "itemType": "attachment",
                            "contentType": "application/pdf",
                            "path": "attachments:file.pdf",
                        },
                    }
                ],
            )
        if request.url.path.endswith("/items/ATTACH/file/view/url"):
            return _response(request, text=state["pdf_path"].as_uri())
        raise AssertionError(str(request.url))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    local = ZoteroLocalClient(client=http)
    async with get_sessionmaker()() as session:
        version = await materialize_paper_pdf(
            session,
            paper_id=paper_id,
            user_id=owner_id,
            library_id=library_id,
            client=local,
        )
        assert version is not None and version.status == "queued"
        asset = await session.get(PaperAsset, version.asset_id)
        assert asset.source == "zotero"
        assert asset.source_locator == "zotero://0/ATTACH"
        assert str(pdf_path) not in asset.source_locator
        again = await materialize_paper_pdf(
            session,
            paper_id=paper_id,
            user_id=owner_id,
            library_id=library_id,
            client=local,
        )
        assert again.id == version.id

        version.status = "ready"
        await session.commit()
        state["attachment_version"] = 8
        state["pdf_path"] = updated_pdf_path
        updated = await materialize_paper_pdf(
            session,
            paper_id=paper_id,
            user_id=owner_id,
            library_id=library_id,
            client=local,
        )
        assert updated.id != version.id
        assert updated.asset_id != version.asset_id
        assert updated.version_no == 2
        link = await session.scalar(
            select(ZoteroItemLink).where(ZoteroItemLink.paper_id == paper_id)
        )
        assert (link.attachment_key, link.attachment_version) == ("ATTACH", 8)
    await http.aclose()
