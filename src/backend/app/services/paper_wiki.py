"""论文解读的唯一读写入口（不 import fastapi）。

一篇论文只有一份解读（``paper_wikis``，见 models/paper.PaperWiki）：库内编译、
每日推送编译、单篇重新编译都 upsert 同一行，读解读一律查这里——没有就是没有，
不再有「库版 / 个人版 / 快照」的优先级链。
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper, PaperWiki
from app.models.user import User


async def upsert_wiki(
    session: AsyncSession,
    *,
    paper: Paper,
    content: str,
    model: str | None = None,
    compiled_by: uuid.UUID | None = None,
    source_level: str | None = None,
    content_version_id: uuid.UUID | None = None,
    source_fingerprint: str | None = None,
    evidence_manifest: dict | None = None,
    source_library_id: uuid.UUID | None = None,
    source_project_id: uuid.UUID | None = None,
) -> PaperWiki:
    """追加一个版本并更新兼容 ``PaperWiki`` 投影（调用方负责 commit）。

    ``compiled_by`` 每次覆盖成最新编译的人；失败发生在投影切换前时，旧版本保持可读。
    """
    from app.services.paper_summaries import append_ready_revision, extract_tldr

    compiled_tldr = extract_tldr(content)
    wiki, _revision = await append_ready_revision(
        session,
        paper=paper,
        content=content,
        model=model,
        created_by=compiled_by,
        source_level=source_level,
        content_version_id=content_version_id,
        source_fingerprint=source_fingerprint,
        evidence_manifest=evidence_manifest,
        source_library_id=source_library_id,
        source_project_id=source_project_id,
    )
    # Keep this compatibility projection explicit at the legacy write boundary: callers outside
    # the versioned summary API still expect the latest compiled TL;DR on the Paper row.
    paper.tldr = compiled_tldr
    return wiki



async def content_for(session: AsyncSession, paper_id: uuid.UUID) -> str | None:
    """单篇解读正文（没有则 None）；只有 paper_id、拿不到 Paper 对象时用。"""
    stmt = select(PaperWiki.content).where(
        PaperWiki.paper_id == paper_id, PaperWiki.deleted_at.is_(None)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def compiler_names(
    session: AsyncSession, user_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """编译者 id → 显示名（重新编译前的覆盖提示用）。

    compiled_by 是 SET NULL：人被删掉 / 存量迁移数据留空的一律不在结果里，
    调用方按「未知」显示。"""
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {
        uid: (display_name or "").strip() or email.split("@", 1)[0]
        for uid, display_name, email in rows
    }
