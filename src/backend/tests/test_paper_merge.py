"""P6 重复论文合并（任务 3）：全表 repoint（含冲突分支）、候选发现、合并权限。"""

import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library import UserLibraryEntry
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.obsidian_vault import ObsidianVaultConnection, VaultFileState
from app.models.paper import (
    Paper,
    PaperChunk,
    PaperHighlight,
    PaperNote,
    PaperUserMeta,
    PaperWiki,
    PaperWikiRevision,
    paper_concepts,
)
from app.models.paper_assets import AssetGrant, PaperAsset, PdfBlob
from app.models.paper_content import (
    PaperContentChunk,
    PaperContentChunkVector,
    PaperContentVersion,
    PaperContentVersionVector,
)
from app.models.publication import UserPublication
from app.models.topic_shelf import TopicPaper
from app.models.user import User
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
from app.services import paper_merge as merge_service
from app.services.libraries import get_library_for_project
from tests.conftest import add_concept, add_paper, ensure_project_library, register_and_login


async def _setup(client, *, email="merge-owner@example.com", name="合并方向"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return headers, resp.json()["id"]


async def _user_id(session, email):
    return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_merge_papers_full_repoint_with_conflicts(client):
    headers, project_id = await _setup(client)
    _headers_b, project_b = await _setup(client, email="merge-b@example.com", name="第二方向")

    async with get_sessionmaker()() as session:
        owner_id = await _user_id(session, "merge-owner@example.com")
        other_id = await _user_id(session, "merge-b@example.com")
        pid = uuid.UUID(project_id)
        pid_b = uuid.UUID(project_b)
        lib_a = await ensure_project_library(session, pid)
        lib_b = await ensure_project_library(session, pid_b)

        # keep：A 库 scored（无 wiki、无全文分段）
        keep = await add_paper(
            session,
            project_id=project_id,
            title="Sparse Attention Methods",
            year=2025,
            arxiv_id="2501.00001",
            dedup_key="arxiv:2501.00001",
            status="scored",
            relevance_score=0.8,
        )
        # drop：A 库 compiled（有 wiki）+ B 库成员 + 分段 + 概念 + 笔记划线 + 个人视角
        drop = await add_paper(
            session,
            project_id=project_id,
            title="Sparse Attention Methods (v2)",
            year=2025,
            doi="10.1000/sparse",
            dedup_key="doi:10.1000/sparse",
            status="compiled",
            relevance_score=0.9,
            wiki_content="# 解读\n讲 [[Attention]]。",
        )
        session.add(LibraryPaper(library_id=lib_b.id, paper_id=drop.id, status="candidate"))
        session.add(PaperChunk(paper_id=drop.id, seq=0, text="chunk one"))
        session.add(PaperChunk(paper_id=drop.id, seq=1, text="chunk two"))
        shared = await add_concept(
            session, project_id=project_id, name="Attention", slug="attention"
        )
        only_drop = await add_concept(
            session, project_id=project_id, name="Sparsity", slug="sparsity"
        )
        await session.execute(
            paper_concepts.insert(),
            [
                {"paper_id": keep.id, "concept_id": shared.id},
                {"paper_id": drop.id, "concept_id": shared.id},  # 两边都有 → 去重
                {"paper_id": drop.id, "concept_id": only_drop.id},  # 仅 drop → repoint
            ],
        )
        session.add(PaperNote(paper_id=drop.id, author_id=owner_id, content="note"))
        session.add(
            PaperHighlight(
                paper_id=drop.id,
                author_id=owner_id,
                page=1,
                rects=[{"x0": 0, "y0": 0, "x1": 1, "y1": 1}],
                selected_text="hi",
            )
        )
        # 个人视角冲突：keep 未读不星标，drop 已读且星标 → 合并取并/更靠后
        session.add(PaperUserMeta(paper_id=keep.id, user_id=owner_id, starred=False))
        session.add(
            PaperUserMeta(
                paper_id=drop.id, user_id=owner_id, starred=True, reading_status="read"
            )
        )
        # 另一用户只有 drop 行 → repoint
        session.add(PaperUserMeta(paper_id=drop.id, user_id=other_id, starred=True))
        # 书架冲突：同课题两行（keep 行无备注）
        session.add(TopicPaper(topic_id=pid, paper_id=keep.id))
        session.add(TopicPaper(topic_id=pid, paper_id=drop.id, note="why"))
        session.add(TopicPaper(topic_id=pid_b, paper_id=drop.id))  # 仅 drop → repoint
        # 软引用
        session.add(
            UserLibraryEntry(
                user_id=owner_id,
                dedup_key="doi:10.1000/sparse",
                title=drop.title,
                last_paper_id=drop.id,
            )
        )
        session.add(
            UserPublication(
                user_id=owner_id,
                dedup_key="doi:10.1000/sparse",
                title=drop.title,
                source="manual",
                paper_id=drop.id,
            )
        )
        await session.commit()
        keep_id, drop_id = keep.id, drop.id
        lib_a_id, lib_b_id = lib_a.id, lib_b.id
        shared_id, only_drop_id = shared.id, only_drop.id

    async with get_sessionmaker()() as session:
        report = await merge_service.merge_papers(session, keep_id=keep_id, drop_id=drop_id)

    assert report["dropped_dedup_key"] == "doi:10.1000/sparse"
    assert report["library_memberships"] == {"repointed": 1, "merged": 1}
    assert report["topic_papers"] == {"repointed": 1, "merged": 1}
    assert report["paper_user_meta"] == {"repointed": 1, "merged": 1}
    assert report["notes_repointed"] == 1
    assert report["highlights_repointed"] == 1
    assert report["concept_links"] == {"repointed": 1, "deduped": 1}
    assert report["chunks_moved"] == 2
    assert report["library_entries_repointed"] == 1
    assert report["publications_repointed"] == 1
    assert "doi" in report["fields_filled"]

    async with get_sessionmaker()() as session:
        assert await session.get(Paper, drop_id) is None
        keep = await session.get(Paper, keep_id)
        assert keep.doi == "10.1000/sparse"  # 缺项回填
        assert keep.arxiv_id == "2501.00001"  # keep 原值不被覆盖
        # A 库成员行合并：wiki 补上、状态升为 compiled、分数保留 keep 原值
        member_a = (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == lib_a_id, LibraryPaper.paper_id == keep_id
                )
            )
        ).scalar_one()
        assert member_a.status == "compiled"
        assert keep.wiki_content and "[[Attention]]" in keep.wiki_content
        assert member_a.relevance_score == 0.8
        # B 库成员行 repoint 到 keep
        member_b = (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == lib_b_id, LibraryPaper.paper_id == keep_id
                )
            )
        ).scalar_one()
        assert member_b.status == "candidate"
        # 个人视角合并
        owner_id = await _user_id(session, "merge-owner@example.com")
        meta = (
            await session.execute(
                select(PaperUserMeta).where(
                    PaperUserMeta.paper_id == keep_id, PaperUserMeta.user_id == owner_id
                )
            )
        ).scalar_one()
        assert meta.starred is True
        assert meta.reading_status == "read"
        # 概念链：shared 只剩一条、only_drop 已 repoint
        links = (
            await session.execute(
                select(paper_concepts.c.concept_id).where(paper_concepts.c.paper_id == keep_id)
            )
        ).scalars().all()
        assert sorted(map(str, links)) == sorted(map(str, [shared_id, only_drop_id]))
        # 分段随合并迁移
        chunk_count = len(
            (
                await session.execute(
                    select(PaperChunk.id).where(PaperChunk.paper_id == keep_id)
                )
            ).all()
        )
        assert chunk_count == 2
        # 书架：keep 行补了备注（wiki 快照列已退役删除，解读统一走 paper_wikis）
        shelf = (
            await session.execute(
                select(TopicPaper).where(TopicPaper.paper_id == keep_id)
            )
        ).scalars().all()
        assert len(shelf) == 2
        merged_row = next(t for t in shelf if t.topic_id == uuid.UUID(project_id))
        assert merged_row.note == "why"
        # 软引用 repoint
        entry = (
            (await session.execute(select(UserLibraryEntry))).scalars().first()
        )
        assert entry.last_paper_id == keep_id
        pub = (await session.execute(select(UserPublication))).scalars().first()
        assert pub.paper_id == keep_id

        # 幂等：drop 已不存在 → ValueError
        try:
            await merge_service.merge_papers(session, keep_id=keep_id, drop_id=drop_id)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


async def test_duplicate_candidates_and_merge_api(client):
    headers, project_id = await _setup(client, email="cand-owner@example.com")
    stranger_token = await register_and_login(client, email="cand-stranger@example.com")
    stranger = {"Authorization": f"Bearer {stranger_token}"}

    async with get_sessionmaker()() as session:
        keep = await add_paper(
            session,
            project_id=project_id,
            title="A Survey of LLM Agents",
            year=2025,
            status="compiled",
            wiki_content="# wiki",
        )
        drop = await add_paper(
            session,
            project_id=project_id,
            title="a survey of  llm-agents",  # 规范化后同标题
            year=2025,
            status="scored",
        )
        await add_paper(session, project_id=project_id, title="Unrelated Paper", status="scored")
        await session.commit()
        keep_id, drop_id = str(keep.id), str(drop.id)
        library_id = str((await get_library_for_project(session, uuid.UUID(project_id))).id)

    # 候选发现：无关用户 403；可管理者拿到一组（首行 = 有 wiki 的建议保留行）
    resp = await client.get(f"/api/libraries/{library_id}/duplicate-candidates", headers=stranger)
    assert resp.status_code == 403
    resp = await client.get(f"/api/libraries/{library_id}/duplicate-candidates", headers=headers)
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    assert len(groups) == 1
    assert groups[0]["reason"] == "title"
    assert [p["id"] for p in groups[0]["papers"]] == [keep_id, drop_id]
    assert groups[0]["papers"][0]["has_wiki"] is True

    # 合并：无关用户 403；可管理者成功；再次合并（drop 已删）→ 400
    body = {"keep_id": keep_id, "drop_id": drop_id}
    resp = await client.post("/api/papers/merge", json=body, headers=stranger)
    assert resp.status_code == 403
    resp = await client.post("/api/papers/merge", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["kept_id"] == keep_id
    assert result["details"]["library_memberships"] == {"repointed": 0, "merged": 1}
    resp = await client.post("/api/papers/merge", json=body, headers=headers)
    assert resp.status_code == 400
    # 合并后候选清空
    resp = await client.get(f"/api/libraries/{library_id}/duplicate-candidates", headers=headers)
    assert resp.json() == []


async def test_merge_preserves_assets_content_revisions_and_zotero_links(app):
    async with get_sessionmaker()() as session:
        user = User(
            email="merge-content@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        library_a = DirectionLibrary(name="Merge content A", submitted_by=None)
        library_b = DirectionLibrary(name="Merge content B", submitted_by=None)
        keep = Paper(title="Keep content", dedup_key="title:keep-content")
        drop = Paper(title="Drop content", dedup_key="title:drop-content")
        session.add_all([user, library_a, library_b, keep, drop])
        await session.flush()
        blob = PdfBlob(
            sha256="c" * 64,
            byte_size=42,
            storage_key="pdf-blobs/cc/content.pdf",
        )
        session.add(blob)
        await session.flush()
        keep_asset = PaperAsset(
            paper_id=keep.id,
            blob_id=blob.id,
            source="zotero",
            sharing_scope="private",
            state="ready",
        )
        drop_asset = PaperAsset(
            paper_id=drop.id,
            blob_id=blob.id,
            source="zotero",
            sharing_scope="public",
            state="ready",
            is_preferred=True,
        )
        session.add_all([keep_asset, drop_asset])
        await session.flush()
        session.add_all(
            [
                AssetGrant(
                    asset_id=keep_asset.id,
                    library_id=library_a.id,
                    status="revoked",
                    can_read=False,
                    can_process=False,
                ),
                AssetGrant(
                    asset_id=drop_asset.id,
                    library_id=library_a.id,
                    status="active",
                    can_read=True,
                    can_process=True,
                    granted_by=user.id,
                ),
                AssetGrant(
                    asset_id=drop_asset.id,
                    library_id=library_b.id,
                    status="active",
                    can_read=True,
                    can_process=True,
                    granted_by=user.id,
                ),
            ]
        )
        keep_version = PaperContentVersion(
            paper_id=keep.id,
            asset_id=keep_asset.id,
            version_no=1,
            parser="test",
            status="ready",
            is_current=True,
        )
        drop_version = PaperContentVersion(
            paper_id=drop.id,
            asset_id=drop_asset.id,
            version_no=1,
            parser="test",
            status="ready",
            is_current=True,
        )
        session.add_all([keep_version, drop_version])
        await session.flush()
        drop_chunk = PaperContentChunk(
            content_version_id=drop_version.id,
            seq=0,
            text="drop full text chunk",
        )
        session.add(drop_chunk)
        await session.flush()
        session.add_all(
            [
                PaperContentVersionVector(
                    content_version_id=drop_version.id,
                    space="test-space",
                    dim=1,
                    embedding=[0.1],
                    model="test",
                ),
                PaperContentChunkVector(
                    chunk_id=drop_chunk.id,
                    space="test-space",
                    dim=1,
                    embedding=[0.2],
                    model="test",
                ),
            ]
        )
        keep_ready = PaperWikiRevision(
            paper_id=keep.id,
            content_version_id=keep_version.id,
            source_level="fulltext",
            content="keep ready",
            status="ready",
            stage="complete",
        )
        keep_inflight = PaperWikiRevision(
            paper_id=keep.id,
            source_level="abstract",
            status="queued",
            stage="materialize",
        )
        drop_ready = PaperWikiRevision(
            paper_id=drop.id,
            content_version_id=drop_version.id,
            source_level="fulltext",
            content="drop ready",
            status="ready",
            stage="complete",
        )
        drop_inflight = PaperWikiRevision(
            paper_id=drop.id,
            source_level="abstract",
            status="generating",
            stage="compile",
        )
        session.add_all([keep_ready, keep_inflight, drop_ready, drop_inflight])
        await session.flush()
        session.add_all(
            [
                PaperWiki(
                    paper_id=keep.id,
                    content=keep_ready.content or "",
                    current_revision_id=keep_ready.id,
                ),
                PaperWiki(
                    paper_id=drop.id,
                    content=drop_ready.content or "",
                    current_revision_id=drop_ready.id,
                ),
            ]
        )
        binding = ZoteroLocalBinding(
            library_id=library_a.id,
            collection_key="ROOT",
            collection_name="Root",
        )
        session.add(binding)
        await session.flush()
        zotero_link = ZoteroItemLink(
            binding_id=binding.id,
            item_key="DROPITEM",
            item_version=1,
            paper_id=drop.id,
            status="active",
        )
        session.add(zotero_link)
        await session.commit()
        ids = {
            "keep": keep.id,
            "drop": drop.id,
            "keep_asset": keep_asset.id,
            "drop_asset": drop_asset.id,
            "drop_version": drop_version.id,
            "drop_chunk": drop_chunk.id,
            "drop_ready": drop_ready.id,
            "drop_inflight": drop_inflight.id,
            "keep_inflight": keep_inflight.id,
            "zotero_link": zotero_link.id,
            "library_a": library_a.id,
            "library_b": library_b.id,
        }

        report = await merge_service.merge_papers(
            session, keep_id=ids["keep"], drop_id=ids["drop"]
        )
        assert report["content_assets"] == {
            "assets_repointed": 0,
            "assets_merged": 1,
            "grants_repointed": 1,
            "grants_merged": 1,
            "content_versions_repointed": 1,
        }
        assert report["summary_revisions"] == {
            "repointed": 2,
            "inflight_cancelled": 1,
        }
        assert report["zotero_links_repointed"] == 1

    async with get_sessionmaker()() as session:
        assert await session.get(Paper, ids["drop"]) is None
        assets = list(
            (
                await session.execute(
                    select(PaperAsset).where(PaperAsset.paper_id == ids["keep"])
                )
            ).scalars()
        )
        assert [asset.id for asset in assets] == [ids["keep_asset"]]
        assert assets[0].sharing_scope == "public"
        assert assets[0].is_preferred is True
        assert await session.get(PaperAsset, ids["drop_asset"]) is None
        grants = list(
            (
                await session.execute(
                    select(AssetGrant).where(AssetGrant.asset_id == ids["keep_asset"])
                )
            ).scalars()
        )
        assert {grant.library_id for grant in grants} == {
            ids["library_a"],
            ids["library_b"],
        }
        assert all(grant.status == "active" and grant.can_read for grant in grants)
        moved_version = await session.get(PaperContentVersion, ids["drop_version"])
        assert moved_version is not None
        assert moved_version.paper_id == ids["keep"]
        assert moved_version.asset_id == ids["keep_asset"]
        assert moved_version.version_no == 2
        assert moved_version.is_current is False
        assert await session.get(PaperContentChunk, ids["drop_chunk"]) is not None
        assert await session.scalar(
            select(PaperContentVersionVector).where(
                PaperContentVersionVector.content_version_id == ids["drop_version"]
            )
        ) is not None
        assert await session.scalar(
            select(PaperContentChunkVector).where(
                PaperContentChunkVector.chunk_id == ids["drop_chunk"]
            )
        ) is not None
        revisions = {
            row.id: row
            for row in (
                await session.execute(
                    select(PaperWikiRevision).where(
                        PaperWikiRevision.paper_id == ids["keep"]
                    )
                )
            ).scalars()
        }
        assert ids["drop_ready"] in revisions
        assert revisions[ids["drop_ready"]].content_version_id == ids["drop_version"]
        assert revisions[ids["drop_inflight"]].status == "failed"
        assert (
            revisions[ids["drop_inflight"]].error_code
            == "PAPER_MERGED_INFLIGHT_CANCELLED"
        )
        assert revisions[ids["keep_inflight"]].status == "queued"
        link = await session.get(ZoteroItemLink, ids["zotero_link"])
        assert link is not None and link.paper_id == ids["keep"]


async def test_merge_refuses_projected_obsidian_paper_without_disk_rekey(app):
    async with get_sessionmaker()() as session:
        user = User(
            email="merge-vault@example.com",
            hashed_password="test",
            is_active=True,
            is_verified=True,
            is_superuser=False,
        )
        library = DirectionLibrary(name="Merge Vault")
        keep = Paper(title="Keep vault", dedup_key="title:keep-vault")
        drop = Paper(title="Drop vault", dedup_key="title:drop-vault")
        session.add_all([user, library, keep, drop])
        await session.flush()
        connection = ObsidianVaultConnection(user_id=user.id, vault_path="/not-accessed")
        session.add(connection)
        await session.flush()
        session.add(
            VaultFileState(
                connection_id=connection.id,
                library_id=library.id,
                entity_type="summary",
                entity_id=drop.id,
                relative_path="library/papers/drop.md",
                base_content="summary\n",
                base_hash="a" * 64,
                polaris_hash="a" * 64,
                vault_hash="a" * 64,
            )
        )
        await session.commit()
        keep_id, drop_id = keep.id, drop.id

        with pytest.raises(ValueError, match="PAPER_MERGE_OBSIDIAN_REKEY_REQUIRED"):
            await merge_service.merge_papers(
                session, keep_id=keep_id, drop_id=drop_id
            )
        assert await session.get(Paper, drop_id) is not None
