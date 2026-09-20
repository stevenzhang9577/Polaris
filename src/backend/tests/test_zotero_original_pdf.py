"""Original-file Zotero integration: no PDF copies, path refresh, and tenant guards."""

import uuid

import httpx
import pymupdf
import pytest
from sqlalchemy import select

from app.api.auth import current_active_user
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import new_paper
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.user import User
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
from app.services import zotero_local as zotero
from app.services.paper_assets import AssetError, resolve_asset_path


async def test_batch_expansion_pagination_fallback_and_unexpected_keys():
    pages = []

    def handler(request):
        if request.url.path.endswith("/items/B"):
            return httpx.Response(200, json={"key": "B", "data": {"itemType": "note"}})
        start = int(request.url.params.get("start", "0"))
        pages.append(start)
        # The server may impose a page cap smaller than the requested limit.
        return httpx.Response(
            200,
            json=([{"key": "EXTRA"}, {"key": "A"}] if start == 0 else [{"key": "C"}]),
            headers={"Total-Results": "3"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await zotero.ZoteroLocalClient(client=http).items_by_keys(["A", "B", "C"])
    assert set(result) == {"A", "B", "C"}
    assert pages == [0, 2]


async def test_7000_keys_expanded_batches_are_not_lost():
    keys = [f"P{i:07}" for i in range(7000)]

    def handler(request):
        batch = request.url.params["itemKey"].split(",")
        rows = [{"key": f"EXTRA{i}"} for i in range(60)] + [{"key": key} for key in batch]
        start = int(request.url.params["start"])
        return httpx.Response(
            200, json=rows[start : start + 100], headers={"Total-Results": str(len(rows))}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await zotero.ZoteroLocalClient(client=http).items_by_keys(keys)
    assert set(result) == set(keys)


@pytest.fixture
async def original_setup(app, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path / "polaris"))
    path = tmp_path / "中文 paper.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Original one")
        doc.save(path)
    state = {"path": path, "offline": False}

    def handler(request):
        if state["offline"]:
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/api/":
            return httpx.Response(200, text="OK")
        if request.url.path.endswith("/collections"):
            return httpx.Response(200, json=[{"key": "ROOT", "data": {"name": "Root"}}])
        if request.url.path.endswith("/items/top"):
            return httpx.Response(200, json={"PARENT": 1})
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "PARENT",
                        "version": 1,
                        "data": {
                            "itemType": "journalArticle",
                            "title": "Original paper",
                            "DOI": "10.1/original",
                        },
                    }
                ],
            )
        if request.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "ATTACH",
                        "version": 0,
                        "data": {
                            "itemType": "attachment",
                            "contentType": "application/pdf",
                        },
                    }
                ],
            )
        if request.url.path.endswith("/file/view/url"):
            return httpx.Response(200, text=state["path"].as_uri())
        raise AssertionError(str(request.url))

    client_class = zotero.ZoteroLocalClient
    monkeypatch.setattr(
        zotero,
        "ZoteroLocalClient",
        lambda: client_class(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
    )
    async with get_sessionmaker()() as session:
        owner = User(email="original@example.test", hashed_password="unused", is_active=True)
        session.add(owner)
        await session.flush()
        library = DirectionLibrary(name="Original", submitted_by=owner.id)
        paper = new_paper(
            source="zotero",
            title="Original paper",
            dedup_key="doi:10.1/original",
            doi="10.1/original",
        )
        session.add_all([library, paper])
        await session.flush()
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=owner.id,
            collection_key="ROOT",
            collection_name="Root",
        )
        session.add(binding)
        session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, status="included"))
        await session.commit()
        await zotero.sync_binding(session, binding=binding, requested_by=owner.id)
        link = await session.scalar(
            select(ZoteroItemLink).where(ZoteroItemLink.binding_id == binding.id)
        )
        assert link.pdf_status == "linked"
        assert link.local_pdf_path == str(path)
        assert link.attachment_key == "ATTACH"
        assert await session.scalar(select(PaperAsset.id)) is None
    app.dependency_overrides[current_active_user] = lambda: owner
    return owner, library, paper, state


async def test_import_read_move_offline_missing_and_security(original_setup, client, app, tmp_path):
    owner, library, paper, state = original_setup
    url = f"/api/libraries/{library.id}/papers/{paper.id}/zotero-local-pdf"
    original = state["path"].read_bytes()
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == original
    assert (await client.get(f"/api/papers/{paper.id}/pdf")).content == original
    assert "no-store" in response.headers["cache-control"]
    moved = tmp_path / "移动 file.pdf"
    state["path"].rename(moved)
    state["path"] = moved
    assert (await client.get(url, headers={"Range": "bytes=0-4"})).content == b"%PDF-"
    state["offline"] = True
    assert (await client.get(url)).content == original
    moved.unlink()
    response = await client.get(url)
    assert response.status_code == 422
    assert str(tmp_path) not in response.text
    assert not list((tmp_path / "polaris").rglob("*.pdf"))
    app.dependency_overrides[current_active_user] = lambda: User(
        id=uuid.uuid4(), email="other@example.test", hashed_password="unused", is_active=True
    )
    assert (await client.get(url)).status_code == 404
    app.dependency_overrides[current_active_user] = lambda: owner
    get_settings().profile = "server"
    assert (await client.get(url)).status_code == 409


async def test_summary_source_no_copy_and_same_version_replacement(original_setup, tmp_path):
    owner, library, paper, state = original_setup
    async with get_sessionmaker()() as session:
        version = await zotero.materialize_paper_pdf(
            session, paper_id=paper.id, library_id=library.id, user_id=owner.id
        )
        asset = await session.get(PaperAsset, version.asset_id)
        blob = await session.get(PdfBlob, asset.blob_id)
        assert asset.metadata_snapshot["storage_mode"] == "zotero_original"
        assert await resolve_asset_path(asset, blob) == state["path"]
        assert not list((tmp_path / "polaris").rglob("*.pdf"))
        from app.services.paper_content import parse_content_version

        async def offline_parser(path):
            assert path == state["path"]
            raise RuntimeError("Use local parser in test")

        parsed = await parse_content_version(session, version=version, mineru_parser=offline_parser)
        assert parsed.status == "ready_fallback"
        assert parsed.page_count == 1 and parsed.chunk_count == 1
        assert not list((tmp_path / "polaris").rglob("*.pdf"))
        same = await zotero.materialize_paper_pdf(
            session, paper_id=paper.id, library_id=library.id, user_id=owner.id
        )
        assert same.id == version.id
        replacement = tmp_path / "replacement.pdf"
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), "Changed bytes, same Zotero version")
            doc.save(replacement)
        replacement.replace(state["path"])
        with pytest.raises(AssetError, match="ZOTERO_PDF_SOURCE_CHANGED"):
            await resolve_asset_path(asset, blob)
        newer = await zotero.materialize_paper_pdf(
            session, paper_id=paper.id, library_id=library.id, user_id=owner.id
        )
        assert newer.id != version.id
        assert newer.asset_id != asset.id
        assert not list((tmp_path / "polaris").rglob("*.pdf"))
