"""Shared paper read-model projection (no FastAPI dependencies).

Both project/global paper routes and library-scoped routes expose the same paper detail
contract.  Keeping the contextual Zotero and permission fields here prevents one route
family from accidentally returning a weaker or broader view than the other.
"""

import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper_assets import AssetGrant, PaperAsset, PdfBlob
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
from app.schemas.paper import PaperDetail, PaperRead
from app.services import libraries as libraries_service
from app.services import paper_wiki as paper_wiki_service
from app.services import papers as papers_service


async def reads_with_extras(
    session: AsyncSession,
    papers: Sequence[papers_service.PaperView],
    user_id: uuid.UUID,
    *,
    detail: bool = False,
    library_ids: Sequence[uuid.UUID] | None = None,
) -> list[PaperRead]:
    """Project paper views into the public schemas with user/context-scoped extras.

    ``library_ids`` controls only shared library tags.  Zotero fields always follow each
    view's exact ``library_id`` so a paper collected in multiple libraries cannot inherit a
    different library's local item key or materialization permission.
    """
    if not papers:
        return []

    extras = await papers_service.paper_extras_map(
        session,
        paper_ids=[paper.id for paper in papers],
        user_id=user_id,
        library_ids=(
            library_ids
            if library_ids is not None
            else [paper.library_id for paper in papers if paper.library_id is not None]
        ),
    )
    if not detail:
        return [
            PaperRead.model_validate(paper).model_copy(update=extras[paper.id])
            for paper in papers
        ]

    names = await paper_wiki_service.compiler_names(
        session,
        (
            paper.paper.wiki.compiled_by
            for paper in papers
            if paper.paper.wiki is not None and paper.paper.wiki.deleted_at is None
        ),
    )
    paper_ids = [paper.id for paper in papers]
    context_library_by_paper = {paper.id: paper.library_id for paper in papers}
    contextual_library_ids = {
        library_id for library_id in context_library_by_paper.values() if library_id is not None
    }
    pool_paper_ids = {
        paper_id
        for paper_id, library_id in context_library_by_paper.items()
        if library_id is None
    }

    link_stmt = (
        select(ZoteroItemLink, ZoteroLocalBinding.library_id)
        .join(ZoteroLocalBinding, ZoteroLocalBinding.id == ZoteroItemLink.binding_id)
        .where(ZoteroItemLink.paper_id.in_(paper_ids))
        .order_by(
            ZoteroItemLink.paper_id,
            (ZoteroItemLink.status == "active").desc(),
            ZoteroLocalBinding.library_id,
            ZoteroItemLink.item_key,
        )
    )
    if contextual_library_ids and pool_paper_ids:
        link_stmt = link_stmt.where(
            or_(
                ZoteroLocalBinding.library_id.in_(contextual_library_ids),
                (
                    ZoteroItemLink.paper_id.in_(pool_paper_ids)
                    & ZoteroLocalBinding.library_id.in_(
                        libraries_service.visible_library_ids_stmt(user_id)
                    )
                ),
            )
        )
    elif contextual_library_ids:
        link_stmt = link_stmt.where(
            ZoteroLocalBinding.library_id.in_(contextual_library_ids)
        )
    else:
        link_stmt = link_stmt.where(
            ZoteroLocalBinding.library_id.in_(
                libraries_service.visible_library_ids_stmt(user_id)
            )
        )

    zotero_links: dict[uuid.UUID, tuple[ZoteroItemLink, uuid.UUID]] = {}
    for link, zotero_library_id in (await session.execute(link_stmt)).all():
        if link.paper_id is None:
            continue
        context_library_id = context_library_by_paper.get(link.paper_id)
        if context_library_id is not None and zotero_library_id != context_library_id:
            continue
        zotero_links.setdefault(link.paper_id, (link, zotero_library_id))

    manageable_library_ids = set(
        (
            await session.execute(
                select(DirectionLibrary.id).where(
                    or_(
                        DirectionLibrary.submitted_by.is_(None),
                        DirectionLibrary.submitted_by == user_id,
                    )
                )
            )
        ).scalars()
    )
    # A summary is global.  Its write guard also permits a public library explicitly linked
    # to one of the caller's projects; visible_library_clause is the shared predicate for that
    # rule, while plain public read access alone is intentionally insufficient.
    manageable_summary_papers = set(
        (
            await session.execute(
                select(LibraryPaper.paper_id)
                .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
                .where(
                    LibraryPaper.paper_id.in_(paper_ids),
                    LibraryPaper.trash_reason.is_(None),
                    libraries_service.visible_library_clause(user_id),
                )
            )
        ).scalars()
    )
    materialized_zotero = set(
        (
            await session.execute(
                select(PaperAsset.paper_id, AssetGrant.library_id)
                .join(AssetGrant, AssetGrant.asset_id == PaperAsset.id)
                .where(
                    PaperAsset.paper_id.in_(paper_ids),
                    PaperAsset.source == "zotero",
                    PaperAsset.state == "ready",
                    AssetGrant.status == "active",
                    AssetGrant.can_read.is_(True),
                )
            )
        ).all()
    )
    asset_paper_ids = set(
        (
            await session.execute(
                select(PaperAsset.paper_id).where(PaperAsset.paper_id.in_(paper_ids))
            )
        ).scalars()
    )
    readable_asset_paper_ids = set(
        (
            await session.execute(
                select(PaperAsset.paper_id)
                .join(PdfBlob, PdfBlob.id == PaperAsset.blob_id)
                .join(AssetGrant, AssetGrant.asset_id == PaperAsset.id)
                .join(DirectionLibrary, DirectionLibrary.id == AssetGrant.library_id)
                .where(
                    PaperAsset.paper_id.in_(paper_ids),
                    PaperAsset.state == "ready",
                    PdfBlob.state == "ready",
                    AssetGrant.status == "active",
                    AssetGrant.can_read.is_(True),
                    or_(
                        libraries_service.visible_library_clause(user_id),
                        DirectionLibrary.is_public.is_(True),
                    ),
                )
            )
        ).scalars()
    )

    out: list[PaperRead] = []
    for paper in papers:
        compiled_by = (
            paper.paper.wiki.compiled_by
            if paper.paper.wiki is not None and paper.paper.wiki.deleted_at is None
            else None
        )
        zotero_entry = zotero_links.get(paper.id)
        link = zotero_entry[0] if zotero_entry is not None else None
        zotero_library_id = zotero_entry[1] if zotero_entry is not None else None
        zotero_pdf_status = None
        if link is not None:
            if link.status in {"missing", "error"}:
                zotero_pdf_status = link.status
            elif link.pdf_status is not None:
                zotero_pdf_status = link.pdf_status
            elif (paper.id, zotero_library_id) in materialized_zotero:
                zotero_pdf_status = "materialized"
            else:
                zotero_pdf_status = "on_demand"

        update = extras[paper.id] | {
            "compiled_by_name": names.get(compiled_by) if compiled_by else None,
            "zotero_source": link is not None,
            "zotero_item_key": link.item_key if link is not None else None,
            "zotero_pdf_status": zotero_pdf_status,
            "zotero_library_id": zotero_library_id,
            "can_materialize_zotero": bool(
                zotero_library_id is not None and zotero_library_id in manageable_library_ids
            ),
            "can_manage_summary": paper.id in manageable_summary_papers,
            "pdf_available": (
                bool(
                    link
                    and link.pdf_status == "linked"
                    and zotero_library_id in manageable_library_ids
                )
                or (
                    paper.id in readable_asset_paper_ids
                    if paper.id in asset_paper_ids
                    else bool(paper.paper.pdf_path and Path(paper.paper.pdf_path).is_file())
                )
            ),
        }
        out.append(PaperDetail.model_validate(paper).model_copy(update=update))
    return out


async def paper_detail(
    session: AsyncSession,
    paper: papers_service.PaperView,
    user_id: uuid.UUID,
) -> PaperDetail:
    """Project one detail with the same permission and Zotero rules as batch reads."""
    (detail,) = await reads_with_extras(session, [paper], user_id, detail=True)
    return detail  # type: ignore[return-value]
