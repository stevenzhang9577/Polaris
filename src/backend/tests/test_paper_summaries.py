"""Versioned paper summary lifecycle and compatibility projection."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper, TopicSourceLibrary
from app.models.paper import Paper, PaperWiki, PaperWikiRevision
from app.models.paper_assets import AssetGrant, PaperAsset, PdfBlob
from app.models.paper_content import PaperContentVersion
from app.services import paper_summaries
from tests.conftest import add_paper, make_project_with_library, register_and_login


async def _setup(client, *, suffix: str = "base"):
    token = await register_and_login(client, email=f"summary-{suffix}@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name=f"summary-{suffix}"
    )
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title=f"Summary Paper {suffix}",
            abstract="An abstract about reliable agents.",
            status="scored",
        )
        await session.commit()
    return headers, uuid.UUID(str(library_id)), paper.id


async def test_create_summary_queues_persisted_revision(client, queue_stub):
    headers, library_id, paper_id = await _setup(client, suffix="queue")

    response = await client.post(f"/api/papers/{paper_id}/summaries", headers=headers)

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["paper_id"] == str(paper_id)
    assert payload["status"] == "queued"
    assert payload["stage"] == "materialize"
    assert len(queue_stub.jobs) == 1
    name, args, kwargs = queue_stub.jobs[0]
    assert name == "generate_paper_summary_task"
    assert args[:3] == (
        payload["revision_id"],
        str(await _user_id(headers, client)),
        str(library_id),
    )
    assert uuid.UUID(args[3])
    assert kwargs == {"_job_id": f"paper-summary-{payload['revision_id']}"}
    async with get_sessionmaker()() as session:
        revision = await session.get(PaperWikiRevision, uuid.UUID(payload["revision_id"]))
        assert revision is not None and revision.content is None
        assert revision.source_library_id == library_id
        assert revision.source_project_id == uuid.UUID(args[3])


async def test_linked_library_summary_uses_callers_project_scope(client, queue_stub):
    _owner_headers, library_id, paper_id = await _setup(client, suffix="linked-scope")
    bob = await register_and_login(client, email="summary-linked-bob@example.com")
    bob_headers = {"Authorization": f"Bearer {bob}"}
    project_response = await client.post(
        "/api/projects", json={"name": "summary-linked-bob"}, headers=bob_headers
    )
    assert project_response.status_code == 201, project_response.text
    bob_project_id = uuid.UUID(project_response.json()["id"])
    link_response = await client.put(
        f"/api/projects/{bob_project_id}/source-libraries",
        json={"library_ids": [str(library_id)]},
        headers=bob_headers,
    )
    assert link_response.status_code == 200, link_response.text

    response = await client.post(f"/api/papers/{paper_id}/summaries", headers=bob_headers)

    assert response.status_code == 202, response.text
    revision_id = uuid.UUID(response.json()["revision_id"])
    async with get_sessionmaker()() as session:
        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        assert revision.source_library_id == library_id
        assert revision.source_project_id == bob_project_id


async def test_recompile_uses_only_the_current_librarys_private_fulltext(
    client, monkeypatch, tmp_path
):
    owner_token = await register_and_login(client, email="summary-private-owner@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    owner_id = await _user_id(owner_headers, client)
    other_token = await register_and_login(client, email="summary-private-other@example.com")
    other_headers = {"Authorization": f"Bearer {other_token}"}
    other_id = await _user_id(other_headers, client)
    owner_text = tmp_path / "owner.txt"
    other_text = tmp_path / "other.txt"
    owner_text.write_text("owner library private full text", encoding="utf-8")
    other_text.write_text("other library secret full text", encoding="utf-8")

    async with get_sessionmaker()() as session:
        owner_library = DirectionLibrary(name="private-owner", submitted_by=owner_id)
        other_library = DirectionLibrary(name="private-other", submitted_by=other_id)
        paper = Paper(
            title="Deduplicated private paper",
            abstract="safe abstract",
            # Deliberately point the legacy global field at the other library's text.
            full_text_path=str(other_text),
        )
        session.add_all([owner_library, other_library, paper])
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=owner_library.id,
                paper_id=paper.id,
                status="included",
            )
        )
        owner_blob = PdfBlob(
            sha256="c" * 64,
            byte_size=10,
            storage_key="pdf-blobs/cc/" + "c" * 64 + ".pdf",
        )
        other_blob = PdfBlob(
            sha256="d" * 64,
            byte_size=10,
            storage_key="pdf-blobs/dd/" + "d" * 64 + ".pdf",
        )
        session.add_all([owner_blob, other_blob])
        await session.flush()
        owner_asset = PaperAsset(
            paper_id=paper.id, blob_id=owner_blob.id, source="zotero", state="ready"
        )
        other_asset = PaperAsset(
            paper_id=paper.id, blob_id=other_blob.id, source="zotero", state="ready"
        )
        session.add_all([owner_asset, other_asset])
        await session.flush()
        owner_version = PaperContentVersion(
            paper_id=paper.id,
            asset_id=owner_asset.id,
            version_no=1,
            parser="test",
            status="ready",
            text_key=str(owner_text),
            is_current=False,
        )
        other_version = PaperContentVersion(
            paper_id=paper.id,
            asset_id=other_asset.id,
            version_no=2,
            parser="test",
            status="ready",
            text_key=str(other_text),
            is_current=True,
        )
        session.add_all(
            [
                AssetGrant(asset_id=owner_asset.id, library_id=owner_library.id),
                AssetGrant(asset_id=other_asset.id, library_id=other_library.id),
                owner_version,
                other_version,
            ]
        )
        await session.commit()
        paper_id = paper.id
        owner_library_id = owner_library.id
        owner_version_id = owner_version.id

    captured: dict[str, object] = {}

    async def compile_scoped(_paper, **kwargs):
        captured.update(kwargs)
        from app.services.wiki_compile import CompiledWiki

        return CompiledWiki(
            content="## TL;DR\nScoped summary.\n\n## 方法\nOnly authorized text.",
            model="fake-scoped",
        )

    monkeypatch.setattr("app.services.wiki_compile.compile_paper", compile_scoped)
    response = await client.post(f"/api/papers/{paper_id}/recompile", headers=owner_headers)

    assert response.status_code == 200, response.text
    assert captured["source_text"] == "owner library private full text"
    assert captured["source_level"] == "fulltext"
    assert captured["include_figures"] is False
    async with get_sessionmaker()() as session:
        revision = await session.scalar(
            select(PaperWikiRevision)
            .where(PaperWikiRevision.paper_id == paper_id)
            .order_by(PaperWikiRevision.created_at.desc())
        )
        assert revision is not None
        assert revision.content_version_id == owner_version_id
        assert revision.source_library_id == owner_library_id


async def test_completed_generation_gets_a_new_revision_scoped_job_id(client, queue_stub):
    headers, _library_id, paper_id = await _setup(client, suffix="repeat-job")

    first_response = await client.post(f"/api/papers/{paper_id}/summaries", headers=headers)
    assert first_response.status_code == 202, first_response.text
    first = first_response.json()
    async with get_sessionmaker()() as session:
        first_revision = await session.get(PaperWikiRevision, uuid.UUID(first["revision_id"]))
        assert first_revision is not None
        first_revision.status = "ready"
        first_revision.stage = "complete"
        first_revision.content = "## TL;DR\nFirst completed generation."
        await session.commit()

    second_response = await client.post(f"/api/papers/{paper_id}/summaries", headers=headers)
    assert second_response.status_code == 202, second_response.text
    second = second_response.json()

    assert second["revision_id"] != first["revision_id"]
    assert [job[2]["_job_id"] for job in queue_stub.jobs] == [
        f"paper-summary-{first['revision_id']}",
        f"paper-summary-{second['revision_id']}",
    ]


async def _user_id(headers: dict[str, str], client) -> uuid.UUID:
    response = await client.get("/api/users/me", headers=headers)
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["id"])


async def test_history_activation_soft_delete_and_restore(client):
    headers, _library_id, paper_id = await _setup(client, suffix="history")
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        assert paper is not None
        _wiki, first = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nFirst summary.\n\n## 方法\nFirst body.",
            model="fake-one",
            source_level="abstract",
        )
        _wiki, second = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nSecond summary.\n\n## 方法\nSecond body.",
            model="fake-two",
            source_level="abstract",
        )
        _wiki, no_tldr = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## 方法\nA revision without a TL;DR section.",
            model="fake-three",
            source_level="abstract",
        )
        await session.commit()

    response = await client.get(f"/api/papers/{paper_id}/summaries", headers=headers)
    assert response.status_code == 200, response.text
    history = response.json()
    assert [row["id"] for row in history] == [
        str(no_tldr.id),
        str(second.id),
        str(first.id),
    ]
    assert history[0]["is_current"] is True

    response = await client.post(
        f"/api/papers/{paper_id}/summaries/{first.id}/activate", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["current_revision"]["id"] == str(first.id)
    assert response.json()["current_revision"]["tldr"] == "First summary."

    response = await client.delete(f"/api/papers/{paper_id}/summary", headers=headers)
    assert response.status_code == 204, response.text
    assert (await client.get(f"/api/papers/{paper_id}/summary", headers=headers)).status_code == 404
    detail = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["has_wiki"] is False
    assert detail.json()["wiki_content"] is None
    assert detail.json()["tldr"] is None

    response = await client.post(f"/api/papers/{paper_id}/summary/restore", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["current_revision"]["id"] == str(first.id)
    assert response.json()["deleted_at"] is None
    restored_detail = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert restored_detail.json()["tldr"] == "First summary."

    response = await client.post(
        f"/api/papers/{paper_id}/summaries/{no_tldr.id}/activate", headers=headers
    )
    assert response.status_code == 200, response.text
    no_tldr_detail = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert no_tldr_detail.json()["tldr"] is None


async def test_failed_generation_keeps_last_ready_revision(app, monkeypatch):
    async with get_sessionmaker()() as session:
        paper = Paper(title="Failure-safe", abstract="metadata")
        session.add(paper)
        await session.flush()
        wiki, current = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nKeep me.\n\n## 方法\nStable body.",
            source_level="abstract",
        )
        queued = await paper_summaries.queue_summary_revision(
            session, paper=paper, created_by=None
        )
        await session.commit()
        old_revision_id = current.id
        queued_id = queued.id

    async def fail_compile(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.services.wiki_compile.compile_paper", fail_compile)
    async with get_sessionmaker()() as session:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await paper_summaries.generate_queued_revision(
                session, revision_id=queued_id, materialize=_noop_materializer
            )

    async with get_sessionmaker()() as session:
        failed = await session.get(PaperWikiRevision, queued_id)
        persisted = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        assert failed is not None and failed.status == "failed"
        assert persisted is not None and persisted.current_revision_id == old_revision_id
        assert persisted.content == wiki.content


async def test_delete_after_queue_wins_over_completed_generation(app, monkeypatch):
    async with get_sessionmaker()() as session:
        paper = Paper(title="Delete wins", abstract="metadata")
        session.add(paper)
        await session.flush()
        wiki, _current = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nOld visible summary.",
            source_level="abstract",
        )
        queued = await paper_summaries.queue_summary_revision(
            session, paper=paper, created_by=None
        )
        await session.commit()
        queued_id = queued.id
        # Make the ordering deterministic on SQLite, whose persisted timestamps can lose tz info.
        wiki.deleted_at = queued.created_at + timedelta(seconds=1)
        paper.tldr = None
        await session.commit()

    async def compile_success(*_args, **_kwargs):
        return SimpleNamespace(
            content="## TL;DR\nNew generated summary.\n\n## 方法\nBody.",
            model="fake-model",
        )

    monkeypatch.setattr("app.services.wiki_compile.compile_paper", compile_success)
    async with get_sessionmaker()() as session:
        result = await paper_summaries.generate_queued_revision(
            session, revision_id=queued_id, materialize=_noop_materializer
        )
        assert result.status == "ready"
        assert result.stage == "complete"

    async with get_sessionmaker()() as session:
        persisted = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == paper.id)
        )
        persisted_paper = await session.get(Paper, paper.id)
        revision = await session.get(PaperWikiRevision, queued_id)
        assert persisted is not None and persisted.deleted_at is not None
        assert persisted.current_revision_id == queued_id
        assert revision is not None and revision.status == "ready"
        assert persisted_paper is not None and persisted_paper.tldr is None


async def _noop_materializer(*_args, **_kwargs) -> None:
    return None


async def test_duplicate_delivery_does_not_reclaim_generating_revision(app, monkeypatch):
    async with get_sessionmaker()() as session:
        paper = Paper(title="Already generating", abstract="metadata")
        session.add(paper)
        await session.flush()
        revision = await paper_summaries.queue_summary_revision(
            session, paper=paper, created_by=None
        )
        revision.status = "generating"
        revision.stage = "compile"
        await session.commit()
        revision_id = revision.id

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("duplicate delivery must not execute generation side effects")

    monkeypatch.setattr("app.services.wiki_compile.compile_paper", fail_if_called)
    async with get_sessionmaker()() as session:
        result = await paper_summaries.generate_queued_revision(
            session,
            revision_id=revision_id,
            materialize=fail_if_called,
        )

        assert result.id == revision_id
        assert result.status == "generating"
        assert result.stage == "compile"


async def test_recovery_resets_generating_revision_and_queues_exact_revision(client):
    headers, library_id, paper_id = await _setup(client, suffix="recover")
    user_id = await _user_id(headers, client)
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        library = await session.get(DirectionLibrary, library_id)
        assert paper is not None
        assert library is not None
        revision = await paper_summaries.queue_summary_revision(
            session,
            paper=paper,
            created_by=user_id,
            library_id=library_id,
            project_id=library.project_id,
        )
        revision.status = "generating"
        revision.stage = "compile"
        revision.error_code = "WorkerLost"
        revision.error_detail = "old detail"
        await session.commit()
        revision_id = revision.id

    class RecordingRedis:
        def __init__(self):
            self.jobs: list[tuple[str, tuple, dict]] = []

        async def enqueue_job(self, name: str, *args, **kwargs) -> None:
            self.jobs.append((name, args, kwargs))

    redis = RecordingRedis()
    from worker.tasks import recover_paper_summary_jobs_task

    assert await recover_paper_summary_jobs_task({"redis": redis}, include_fresh=True) == 1

    assert len(redis.jobs) == 1
    name, args, kwargs = redis.jobs[0]
    assert name == "generate_paper_summary_task"
    assert args[:3] == (str(revision_id), str(user_id), str(library_id))
    assert uuid.UUID(args[3])
    assert kwargs["_job_id"].startswith(f"paper-summary-recovery-{revision_id}-")
    async with get_sessionmaker()() as session:
        recovered = await session.get(PaperWikiRevision, revision_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.stage == "materialize"
        assert recovered.error_code is None
        assert recovered.error_detail is None


async def test_recovery_finalizes_ready_project_stage_without_regeneration(client):
    headers, library_id, paper_id = await _setup(client, suffix="recover-project")
    user_id = await _user_id(headers, client)
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        assert paper is not None
        _wiki, revision = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nAlready projected.",
            created_by=user_id,
            source_level="abstract",
        )
        revision.stage = "project"
        revision.source_library_id = library_id
        await session.commit()
        revision_id = revision.id

    class RecordingRedis:
        def __init__(self):
            self.jobs: list[tuple[str, tuple, dict]] = []

        async def enqueue_job(self, name: str, *args, **kwargs) -> None:
            self.jobs.append((name, args, kwargs))

    redis = RecordingRedis()
    from worker.tasks import recover_paper_summary_jobs_task

    assert await recover_paper_summary_jobs_task({"redis": redis}) == 1
    assert redis.jobs == []
    async with get_sessionmaker()() as session:
        recovered = await session.get(PaperWikiRevision, revision_id)
        assert recovered is not None
        assert recovered.status == "ready"
        assert recovered.stage == "complete"


async def test_abstract_revision_becomes_stale_when_fulltext_arrives(app, tmp_path):
    async with get_sessionmaker()() as session:
        paper = Paper(title="Stale source", abstract="abstract")
        session.add(paper)
        await session.flush()
        _wiki, revision = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nAbstract only.",
            source_level="abstract",
        )
        await session.commit()
        assert not await paper_summaries.revision_is_stale(
            session, paper=paper, revision=revision
        )

        fulltext = tmp_path / "paper.txt"
        fulltext.write_text("full paper text", encoding="utf-8")
        paper.full_text_path = str(fulltext)
        await session.commit()
        assert await paper_summaries.revision_is_stale(
            session, paper=paper, revision=revision
        )


async def test_legacy_summary_source_returns_text_when_no_assets_exist(app, tmp_path):
    fulltext = tmp_path / "legacy-fulltext.txt"
    fulltext.write_text("legacy full text remains readable", encoding="utf-8")
    async with get_sessionmaker()() as session:
        paper = Paper(
            title="Legacy full text",
            abstract="abstract fallback",
            full_text_path=str(fulltext),
        )
        library = DirectionLibrary(name="legacy-library")
        session.add_all([paper, library])
        await session.flush()

        source = await paper_summaries.current_summary_source(
            session, paper, library_id=library.id
        )

        assert source.source_level == "fulltext"
        assert source.content_version_id is None
        assert source.text == "legacy full text remains readable"


async def test_summary_source_uses_only_the_requesting_library_grant(app, tmp_path):
    first_text = tmp_path / "private-a.txt"
    first_text.write_text("library A private text", encoding="utf-8")
    second_text = tmp_path / "private-b.txt"
    second_text.write_text("library B private text", encoding="utf-8")
    legacy_text = tmp_path / "legacy-global.txt"
    legacy_text.write_text("must not be used after assets exist", encoding="utf-8")

    async with get_sessionmaker()() as session:
        paper = Paper(
            title="Shared identity",
            abstract="safe metadata",
            full_text_path=str(legacy_text),
        )
        library_a = DirectionLibrary(name="private-a")
        library_b = DirectionLibrary(name="private-b")
        library_c = DirectionLibrary(name="private-c")
        session.add_all([paper, library_a, library_b, library_c])
        await session.flush()
        blob_a = PdfBlob(
            sha256="a" * 64,
            byte_size=10,
            storage_key="pdf-blobs/aa/" + "a" * 64 + ".pdf",
        )
        blob_b = PdfBlob(
            sha256="b" * 64,
            byte_size=10,
            storage_key="pdf-blobs/bb/" + "b" * 64 + ".pdf",
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
        session.add_all(
            [
                AssetGrant(asset_id=asset_a.id, library_id=library_a.id),
                AssetGrant(asset_id=asset_b.id, library_id=library_b.id),
                PaperContentVersion(
                    paper_id=paper.id,
                    asset_id=asset_a.id,
                    version_no=1,
                    parser="test",
                    status="ready",
                    text_key=str(first_text),
                    is_current=False,
                ),
                PaperContentVersion(
                    paper_id=paper.id,
                    asset_id=asset_b.id,
                    version_no=2,
                    parser="test",
                    status="ready",
                    text_key=str(second_text),
                    is_current=True,
                ),
            ]
        )
        await session.flush()

        source_a = await paper_summaries.current_summary_source(
            session, paper, library_id=library_a.id
        )
        source_b = await paper_summaries.current_summary_source(
            session, paper, library_id=library_b.id
        )
        source_c = await paper_summaries.current_summary_source(
            session, paper, library_id=library_c.id
        )
        unscoped_source = await paper_summaries.current_summary_source(session, paper)
        assert source_a.text == "library A private text"
        assert source_b.text == "library B private text"
        assert source_c.source_level == "abstract"
        assert source_c.text is None
        assert unscoped_source.source_level == "abstract"
        assert unscoped_source.content_version_id is None
        assert unscoped_source.text is None


async def test_obsidian_revision_tracks_immutable_fulltext_version(app, tmp_path):
    first_text = tmp_path / "version-one.txt"
    first_text.write_text("first full text", encoding="utf-8")
    second_text = tmp_path / "version-two.txt"
    second_text.write_text("second full text", encoding="utf-8")

    async with get_sessionmaker()() as session:
        paper = Paper(title="Obsidian source", abstract="abstract")
        library = DirectionLibrary(name="Obsidian source library")
        session.add_all([paper, library])
        await session.flush()
        blob = PdfBlob(
            sha256="1" * 64,
            byte_size=100,
            storage_key="papers/one.pdf",
        )
        session.add(blob)
        await session.flush()
        asset = PaperAsset(
            paper_id=paper.id,
            blob_id=blob.id,
            source="upload",
            state="ready",
        )
        session.add(asset)
        await session.flush()
        first_version = PaperContentVersion(
            paper_id=paper.id,
            asset_id=asset.id,
            version_no=1,
            parser="test",
            status="ready",
            text_key=str(first_text),
            is_current=True,
        )
        session.add(first_version)
        session.add(AssetGrant(asset_id=asset.id, library_id=library.id))
        await session.flush()
        _wiki, revision = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nEdited in Obsidian.",
            source_level="obsidian",
            source_library_id=library.id,
        )
        await session.commit()

        first_version.updated_at = utcnow() + timedelta(minutes=1)
        await session.commit()
        assert not await paper_summaries.revision_is_stale(
            session, paper=paper, revision=revision
        )

        first_version.is_current = False
        second_version = PaperContentVersion(
            paper_id=paper.id,
            asset_id=asset.id,
            version_no=2,
            parser="test",
            status="ready",
            text_key=str(second_text),
            is_current=True,
        )
        session.add(second_version)
        await session.commit()

        assert await paper_summaries.revision_is_stale(
            session, paper=paper, revision=revision
        )


async def test_public_library_reader_cannot_mutate_shared_summary(client):
    owner_headers, _library_id, paper_id = await _setup(client, suffix="write-boundary")
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        assert paper is not None
        _wiki, revision = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nShared but owner-controlled.",
            source_level="abstract",
        )
        await session.commit()
        revision_id = revision.id

    reader_token = await register_and_login(client, email="summary-reader@example.com")
    reader_headers = {"Authorization": f"Bearer {reader_token}"}
    response = await client.get(f"/api/papers/{paper_id}/summary", headers=reader_headers)
    assert response.status_code == 200, response.text

    response = await client.post(f"/api/papers/{paper_id}/summaries", headers=reader_headers)
    assert response.status_code == 404, response.text
    response = await client.post(
        f"/api/papers/{paper_id}/summaries/{revision_id}/activate",
        headers=reader_headers,
    )
    assert response.status_code == 404, response.text
    response = await client.delete(f"/api/papers/{paper_id}/summary", headers=reader_headers)
    assert response.status_code == 404, response.text

    response = await client.get(f"/api/papers/{paper_id}/summary", headers=owner_headers)
    assert response.status_code == 200, response.text
    assert response.json()["current_revision"]["id"] == str(revision_id)

    response = await client.delete(
        f"/api/papers/{paper_id}/summary", headers=owner_headers
    )
    assert response.status_code == 204, response.text
    assert (
        await client.get(f"/api/papers/{paper_id}/summary", headers=reader_headers)
    ).status_code == 404
    reader_history = await client.get(
        f"/api/papers/{paper_id}/summaries", headers=reader_headers
    )
    assert reader_history.status_code == 200, reader_history.text
    assert reader_history.json() == []
    assert "Shared but owner-controlled" not in reader_history.text

    owner_history = await client.get(
        f"/api/papers/{paper_id}/summaries", headers=owner_headers
    )
    assert owner_history.status_code == 200, owner_history.text
    assert owner_history.json()[0]["id"] == str(revision_id)


async def test_legacy_private_library_link_does_not_grant_summary_access(client):
    owner_token = await register_and_login(client, email="summary-link-owner@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    owner_id = await _user_id(owner_headers, client)
    outsider_token = await register_and_login(client, email="summary-link-outsider@example.com")
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}
    project_response = await client.post(
        "/api/projects", json={"name": "malformed-link"}, headers=outsider_headers
    )
    assert project_response.status_code == 201, project_response.text
    outsider_project_id = uuid.UUID(project_response.json()["id"])

    async with get_sessionmaker()() as session:
        private_library = DirectionLibrary(
            name="owner-private", submitted_by=owner_id, is_public=False
        )
        paper = Paper(title="Private linked paper", abstract="owner-only")
        session.add_all([private_library, paper])
        await session.flush()
        session.add_all(
            [
                LibraryPaper(
                    library_id=private_library.id,
                    paper_id=paper.id,
                    status="included",
                ),
                # Simulate a row created before link authorization was enforced.
                TopicSourceLibrary(
                    topic_id=outsider_project_id,
                    library_id=private_library.id,
                ),
            ]
        )
        await session.commit()
        paper_id = paper.id

    detail = await client.get(f"/api/papers/{paper_id}", headers=outsider_headers)
    assert detail.status_code == 404, detail.text
    response = await client.post(f"/api/papers/{paper_id}/summaries", headers=outsider_headers)
    assert response.status_code == 404, response.text


async def test_expired_soft_delete_is_purged(app):
    async with get_sessionmaker()() as session:
        paper = Paper(title="Expired", abstract="abstract")
        session.add(paper)
        await session.flush()
        wiki, _revision = await paper_summaries.append_ready_revision(
            session,
            paper=paper,
            content="## TL;DR\nExpired summary.",
            source_level="abstract",
        )
        wiki.deleted_at = utcnow() - timedelta(days=31)
        await session.commit()
        paper_id = paper.id

        assert await paper_summaries.purge_expired_summaries(session) == 1
        await session.commit()
        assert await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == paper_id)
        ) is None
        assert list(
            (
                await session.execute(
                    select(PaperWikiRevision).where(PaperWikiRevision.paper_id == paper_id)
                )
            ).scalars()
        ) == []


async def test_legacy_wiki_is_backfilled_on_read(client):
    headers, _library_id, paper_id = await _setup(client, suffix="legacy")
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        assert paper is not None
        session.add(
            PaperWiki(
                paper_id=paper.id,
                content="## TL;DR\nLegacy summary.",
                model="old-model",
            )
        )
        await session.commit()

    response = await client.get(f"/api/papers/{paper_id}/summary", headers=headers)
    assert response.status_code == 200, response.text
    revision = response.json()["current_revision"]
    assert revision["source_level"] == "legacy"
    assert revision["tldr"] == "Legacy summary."
    assert revision["is_current"] is True
