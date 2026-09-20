"""Versioned paper summaries with a backwards-compatible ``PaperWiki`` projection.

The revision table is the audit trail. ``PaperWiki`` remains the cheap, single-row read model used
by the existing UI, search, exports and concept linker. A revision is only projected into that row
after generation succeeds, so a failed retry never destroys the last usable summary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.paper import (
    SUMMARY_SOURCE_LEVELS,
    Paper,
    PaperWiki,
    PaperWikiRevision,
)

logger = logging.getLogger(__name__)

SUMMARY_RETENTION_DAYS = 30
SUMMARY_PROMPT_VERSION = "librarian-v1"
SUMMARY_SCHEMA_VERSION = "paper-summary-v1"
_READY_CONTENT_STATUSES = frozenset({"ready", "ready_fallback", "vector_ready"})
_ACTIVATABLE_REVISION_STATUSES = frozenset({"ready", "stale"})
_TLDR_RE = re.compile(r"^## TL;DR\s*\n(.+?)(?=\n##\s|\Z)", re.MULTILINE | re.DOTALL)
_EVIDENCE_RE = re.compile(r"\n?<!-- polaris-ai-evidence:(\{.*?\}) -->\s*$", re.DOTALL)


class SummaryNotFoundError(LookupError):
    pass


class SummaryRevisionNotFoundError(LookupError):
    pass


class SummaryRevisionStateError(ValueError):
    pass


class SummaryRestoreExpiredError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SummarySource:
    source_level: str
    content_version_id: uuid.UUID | None
    fingerprint: str
    text: str | None = None


MaterializeSummarySource = Callable[
    [AsyncSession, Paper, uuid.UUID | None, uuid.UUID | None], Awaitable[None]
]


async def _materialize_zotero_source(
    session: AsyncSession,
    paper: Paper,
    user_id: uuid.UUID | None,
    library_id: uuid.UUID | None,
) -> None:
    """Optional bridge to the local Zotero adapter, absent on server-only deployments."""
    if user_id is None or library_id is None or not get_settings().is_desktop:
        return
    try:
        from app.services.zotero_local import materialize_paper_pdf
    except (ImportError, AttributeError):
        return
    version = await materialize_paper_pdf(
        session,
        paper_id=paper.id,
        user_id=user_id,
        library_id=library_id,
    )
    if version is None or version.status in _READY_CONTENT_STATUSES:
        return
    if version.status != "queued":
        return  # another parse attempt owns this version; this summary falls back to metadata
    from app.services.paper_content import parse_content_version, vectorize_content_version

    version = await parse_content_version(session, version=version)
    try:
        await vectorize_content_version(
            session, version=version, user_id=user_id, library_id=library_id
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - vectors are not required for summary generation
        logger.warning("summary source vectorization failed for %s", version.id, exc_info=True)


def extract_tldr(markdown: str | None) -> str | None:
    """Extract the prose under ``## TL;DR`` without retaining Markdown whitespace."""
    if not markdown:
        return None
    match = _TLDR_RE.search(markdown)
    if not match:
        return None
    return " ".join(match.group(1).strip().split()) or None


def extract_evidence_manifest(markdown: str | None) -> dict[str, Any] | None:
    """Read the hidden immutable evidence locator appended by the compiler."""
    if not markdown:
        return None
    match = _EVIDENCE_RE.search(markdown)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


async def current_summary_source(
    session: AsyncSession,
    paper: Paper,
    *,
    library_id: uuid.UUID | None = None,
) -> SummarySource:
    """Resolve the best currently available source without exposing local file paths."""
    from app.models.paper_assets import PaperAsset

    has_assets = (
        await session.scalar(
            select(PaperAsset.id).where(PaperAsset.paper_id == paper.id).limit(1)
        )
        is not None
    )
    if library_id is not None:
        from app.services.paper_content import latest_readable_content_version

        version = await latest_readable_content_version(
            session,
            paper_id=paper.id,
            library_id=library_id,
            require_process=True,
            ready_only=True,
        )
    else:
        # Asset-backed text always needs an explicit library grant. A background/global caller
        # without library context may use safe metadata, but must never pick the paper-global
        # ``is_current`` version because that version can belong to another tenant's private PDF.
        version = None
    if version is not None:
        text: str | None = None
        path = Path(version.text_key) if version.text_key else None
        if path is not None and path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
        if text:
            # Content versions are immutable. Parser/vector state updates may touch
            # ``updated_at`` without changing the source, so the stable version id is the
            # correct stale boundary.
            fingerprint = _sha256(f"content-version:{version.id}")
            return SummarySource("fulltext", version.id, fingerprint, text)

    # Once asset/grant storage is in use, the legacy Paper path may point at another library's
    # private PDF.  A scoped caller may only fall back to that path when no asset exists at all.
    path = Path(paper.full_text_path) if paper.full_text_path and not has_assets else None
    if path is not None and path.is_file():
        stat = path.stat()
        text = path.read_text(encoding="utf-8", errors="ignore")
        if text.strip():
            fingerprint = _sha256(
                f"legacy-fulltext:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            )
            return SummarySource("fulltext", None, fingerprint, text)

    metadata = json.dumps(
        {
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "year": paper.year,
            "venue": paper.venue,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return SummarySource("abstract", None, _sha256(metadata))


async def _locked_wiki(session: AsyncSession, paper_id: uuid.UUID) -> PaperWiki | None:
    return await session.scalar(
        select(PaperWiki).where(PaperWiki.paper_id == paper_id).with_for_update()
    )


async def _project_revision(
    session: AsyncSession,
    *,
    paper: Paper,
    revision: PaperWikiRevision,
    restore: bool,
) -> PaperWiki:
    if revision.content is None or revision.status not in _ACTIVATABLE_REVISION_STATUSES:
        raise SummaryRevisionStateError("SUMMARY_REVISION_NOT_READY")
    wiki = await _locked_wiki(session, paper.id)
    if wiki is None:
        wiki = PaperWiki(
            paper_id=paper.id,
            content=revision.content,
            model=revision.model,
            compiled_by=revision.created_by,
            current_revision_id=revision.id,
        )
        session.add(wiki)
    else:
        wiki.content = revision.content
        wiki.model = revision.model
        wiki.compiled_by = revision.created_by
        wiki.current_revision_id = revision.id
        wiki.updated_at = utcnow()
    if restore:
        wiki.deleted_at = None
    # A hidden summary must not leak its preview through the denormalized Paper row. This also
    # preserves a delete that races with a queued regeneration.
    paper.tldr = None if wiki.deleted_at is not None else revision.tldr
    await session.flush()
    set_committed_value(paper, "wiki", wiki)
    return wiki


async def append_ready_revision(
    session: AsyncSession,
    *,
    paper: Paper,
    content: str,
    model: str | None = None,
    created_by: uuid.UUID | None = None,
    source_level: str | None = None,
    content_version_id: uuid.UUID | None = None,
    source_fingerprint: str | None = None,
    evidence_manifest: dict[str, Any] | None = None,
    source_library_id: uuid.UUID | None = None,
    source_project_id: uuid.UUID | None = None,
    prompt_version: str | None = SUMMARY_PROMPT_VERSION,
    schema_version: str | None = SUMMARY_SCHEMA_VERSION,
    restore: bool = True,
) -> tuple[PaperWiki, PaperWikiRevision]:
    """Append an immutable ready revision and atomically make it current."""
    if not content.strip():
        raise SummaryRevisionStateError("SUMMARY_CONTENT_EMPTY")
    # Callers that edit an existing revision (notably the Obsidian bridge) provide its immutable
    # provenance explicitly. Do not re-resolve the paper-global current content version here: it
    # may belong to another library's private asset and would mismatch the retained evidence.
    source: SummarySource | None = None
    if source_level is None or source_fingerprint is None:
        source = await current_summary_source(
            session, paper, library_id=source_library_id
        )
    level = source_level or (source.source_level if source is not None else "legacy")
    if level not in SUMMARY_SOURCE_LEVELS:
        raise SummaryRevisionStateError("SUMMARY_SOURCE_LEVEL_INVALID")
    revision = PaperWikiRevision(
        paper_id=paper.id,
        content_version_id=(
            content_version_id
            if source is None or content_version_id is not None
            else source.content_version_id
        ),
        source_level=level,
        content=content,
        tldr=extract_tldr(content),
        model=model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        created_by=created_by,
        source_library_id=source_library_id,
        source_project_id=source_project_id,
        source_fingerprint=(
            source_fingerprint
            if source_fingerprint is not None
            else source.fingerprint if source is not None else None
        ),
        evidence_manifest=evidence_manifest or extract_evidence_manifest(content),
        status="ready",
        stage="complete",
    )
    session.add(revision)
    await session.flush()
    wiki = await _project_revision(session, paper=paper, revision=revision, restore=restore)
    return wiki, revision


async def ensure_legacy_revision(
    session: AsyncSession, *, paper: Paper, wiki: PaperWiki
) -> PaperWikiRevision:
    """Lazily cover pre-migration rows; the Alembic migration performs the bulk backfill."""
    if wiki.current_revision_id is not None:
        revision = await session.get(PaperWikiRevision, wiki.current_revision_id)
        if revision is not None:
            return revision
    revision = PaperWikiRevision(
        paper_id=paper.id,
        source_level="legacy",
        content=wiki.content,
        tldr=extract_tldr(wiki.content),
        model=wiki.model,
        created_by=wiki.compiled_by,
        source_fingerprint=_sha256(wiki.content),
        evidence_manifest=extract_evidence_manifest(wiki.content),
        status="ready",
        stage="complete",
        prompt_version=None,
        schema_version=None,
        created_at=wiki.created_at,
        updated_at=wiki.updated_at,
    )
    session.add(revision)
    await session.flush()
    wiki.current_revision_id = revision.id
    await session.flush()
    return revision


async def queue_summary_revision(
    session: AsyncSession,
    *,
    paper: Paper,
    created_by: uuid.UUID | None,
    library_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> PaperWikiRevision:
    """Create (or reuse) the one in-flight generation for a paper."""
    # Serialize the check/create pair per paper. The queue job id handles delivery deduplication;
    # this row lock prevents two API replicas from persisting orphan queued revisions first.
    await session.execute(select(Paper.id).where(Paper.id == paper.id).with_for_update())
    existing = await session.scalar(
        select(PaperWikiRevision)
        .where(
            PaperWikiRevision.paper_id == paper.id,
            PaperWikiRevision.status.in_(("queued", "generating")),
        )
        .order_by(PaperWikiRevision.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    source = await current_summary_source(session, paper, library_id=library_id)
    revision = PaperWikiRevision(
        paper_id=paper.id,
        content_version_id=source.content_version_id,
        source_level=source.source_level,
        created_by=created_by,
        source_library_id=library_id,
        source_project_id=project_id,
        source_fingerprint=source.fingerprint,
        prompt_version=SUMMARY_PROMPT_VERSION,
        schema_version=SUMMARY_SCHEMA_VERSION,
        status="queued",
        stage="materialize",
    )
    try:
        async with session.begin_nested():
            session.add(revision)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(PaperWikiRevision)
            .where(
                PaperWikiRevision.paper_id == paper.id,
                PaperWikiRevision.status.in_(("queued", "generating")),
            )
            .order_by(PaperWikiRevision.created_at.desc())
            .limit(1)
        )
        if existing is None:
            raise
        return existing
    return revision


async def get_current_summary(
    session: AsyncSession,
    *,
    paper: Paper,
    include_deleted: bool = False,
) -> tuple[PaperWiki, PaperWikiRevision] | None:
    wiki = await _locked_wiki(session, paper.id)
    if wiki is None or (wiki.deleted_at is not None and not include_deleted):
        return None
    revision = await ensure_legacy_revision(session, paper=paper, wiki=wiki)
    return wiki, revision


async def list_summary_revisions(
    session: AsyncSession, *, paper: Paper
) -> list[PaperWikiRevision]:
    wiki = await _locked_wiki(session, paper.id)
    if wiki is not None:
        await ensure_legacy_revision(session, paper=paper, wiki=wiki)
    return list(
        (
            await session.execute(
                select(PaperWikiRevision)
                .where(PaperWikiRevision.paper_id == paper.id)
                .order_by(PaperWikiRevision.created_at.desc(), PaperWikiRevision.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def revision_is_stale(
    session: AsyncSession, *, paper: Paper, revision: PaperWikiRevision
) -> bool:
    if revision.status == "stale":
        return True
    if revision.status != "ready":
        return False
    source = await current_summary_source(
        session, paper, library_id=revision.source_library_id
    )
    if revision.source_level == "legacy":
        return False
    if revision.source_level == "abstract" and source.source_level == "fulltext":
        revision.status = "stale"
        await session.flush()
        return True
    if revision.source_level in {"fulltext", "obsidian"}:
        stale = bool(
            revision.source_fingerprint
            and source.fingerprint
            and revision.source_fingerprint != source.fingerprint
        )
        if stale:
            revision.status = "stale"
            await session.flush()
        return stale
    return False


async def activate_revision(
    session: AsyncSession,
    *,
    paper: Paper,
    revision_id: uuid.UUID,
) -> tuple[PaperWiki, PaperWikiRevision]:
    revision = await session.scalar(
        select(PaperWikiRevision).where(
            PaperWikiRevision.id == revision_id,
            PaperWikiRevision.paper_id == paper.id,
        )
    )
    if revision is None:
        raise SummaryRevisionNotFoundError(str(revision_id))
    wiki = await _project_revision(session, paper=paper, revision=revision, restore=False)
    return wiki, revision


async def soft_delete_summary(session: AsyncSession, *, paper: Paper) -> PaperWiki:
    wiki = await _locked_wiki(session, paper.id)
    if wiki is None:
        raise SummaryNotFoundError(str(paper.id))
    if wiki.deleted_at is None:
        wiki.deleted_at = utcnow()
        wiki.updated_at = utcnow()
    paper.tldr = None
    await session.flush()
    set_committed_value(paper, "wiki", wiki)
    return wiki


def restore_until(wiki: PaperWiki) -> datetime | None:
    return (
        wiki.deleted_at + timedelta(days=SUMMARY_RETENTION_DAYS)
        if wiki.deleted_at is not None
        else None
    )


async def restore_summary(
    session: AsyncSession, *, paper: Paper
) -> tuple[PaperWiki, PaperWikiRevision]:
    wiki = await _locked_wiki(session, paper.id)
    if wiki is None or wiki.deleted_at is None:
        raise SummaryNotFoundError(str(paper.id))
    deadline = restore_until(wiki)
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)  # SQLite drops timezone metadata
    if deadline is not None and utcnow() > deadline:
        raise SummaryRestoreExpiredError(str(paper.id))
    revision = await ensure_legacy_revision(session, paper=paper, wiki=wiki)
    wiki.deleted_at = None
    wiki.updated_at = utcnow()
    paper.tldr = revision.tldr
    await session.flush()
    set_committed_value(paper, "wiki", wiki)
    return wiki, revision


async def purge_expired_summaries(session: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(days=SUMMARY_RETENTION_DAYS)
    wikis = list(
        (
            await session.execute(
                select(PaperWiki).where(
                    PaperWiki.deleted_at.is_not(None), PaperWiki.deleted_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    for wiki in wikis:
        wiki.current_revision_id = None
        paper = await session.get(Paper, wiki.paper_id)
        if paper is not None:
            paper.tldr = None
    if wikis:
        await session.flush()
        paper_ids = [wiki.paper_id for wiki in wikis]
        await session.execute(
            delete(PaperWikiRevision).where(PaperWikiRevision.paper_id.in_(paper_ids))
        )
        for wiki in wikis:
            await session.delete(wiki)
        await session.flush()
    return len(wikis)


async def generate_queued_revision(
    session: AsyncSession,
    *,
    revision_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    materialize: MaterializeSummarySource | None = None,
) -> PaperWikiRevision:
    """Run materialize/parse/compile/project for one persisted revision."""
    revision = await session.scalar(
        select(PaperWikiRevision)
        .where(PaperWikiRevision.id == revision_id)
        .with_for_update()
    )
    if revision is None:
        raise SummaryRevisionNotFoundError(str(revision_id))
    user_id = revision.created_by or user_id
    library_id = revision.source_library_id or library_id
    project_id = revision.source_project_id or project_id
    if revision.status in {*_ACTIVATABLE_REVISION_STATUSES, "failed"}:
        return revision
    if revision.status == "generating":
        # A duplicate ARQ delivery arrived after the first worker claimed the revision.  Crash
        # recovery explicitly resets stale rows to queued before dispatching them again.
        return revision
    paper_id = revision.paper_id
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise SummaryNotFoundError(str(revision.paper_id))

    try:
        revision.status = "generating"
        revision.stage = "materialize"
        revision.error_code = None
        revision.error_detail = None
        await session.commit()

        materializer = materialize or _materialize_zotero_source
        try:
            await materializer(session, paper, user_id, library_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - missing/unusable local PDF falls back to metadata
            logger.warning(
                "paper summary source materialization failed for %s; using available metadata",
                paper.id,
                exc_info=True,
            )
            await session.rollback()
            paper = await session.get(Paper, paper_id)
            revision = await session.get(PaperWikiRevision, revision_id)
            assert paper is not None and revision is not None
        revision.stage = "parse"
        await session.commit()

        source = await current_summary_source(session, paper, library_id=library_id)
        revision.stage = "compile"
        await session.commit()

        from app.services.wiki_compile import compile_paper

        compiled = await compile_paper(
            paper,
            session=session,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            source_text=source.text,
            source_level=source.source_level,
            include_figures=False,
        )
        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        revision.content_version_id = source.content_version_id
        revision.source_level = source.source_level
        revision.source_fingerprint = source.fingerprint
        revision.content = compiled.content
        revision.tldr = extract_tldr(compiled.content)
        revision.model = compiled.model or None
        revision.evidence_manifest = extract_evidence_manifest(compiled.content)
        revision.status = "ready"
        revision.stage = "project"
        current_wiki = await _locked_wiki(session, paper.id)
        restore_projection = True
        if current_wiki is not None and current_wiki.deleted_at is not None:
            deleted_at = current_wiki.deleted_at
            queued_at = revision.created_at
            if deleted_at.tzinfo is None:
                deleted_at = deleted_at.replace(tzinfo=UTC)
            if queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=UTC)
            # Regenerating a summary that was already in the trash is an explicit restore. A
            # deletion after this revision was queued is a newer user intent and must win.
            restore_projection = deleted_at <= queued_at
        await _project_revision(
            session,
            paper=paper,
            revision=revision,
            restore=restore_projection,
        )
        await session.commit()
        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        revision.stage = "complete"
        await session.commit()

        # Concept links and both file projections are best-effort side effects.  The ready DB
        # revision remains authoritative and must not be turned into a failed generation because
        # a local filesystem or a secondary linker is temporarily unavailable.
        try:
            from app.core.llm.router import get_llm_router
            from app.services.concepts import link_paper_concepts
            from app.services.libraries import get_membership

            membership = (
                await get_membership(session, library_id=library_id, paper_id=paper_id)
                if library_id is not None
                else None
            )
            await link_paper_concepts(
                session,
                paper,
                membership,
                llm=get_llm_router(),
                user_id=user_id,
                project_id=project_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.warning("summary concept projection failed for %s", paper_id, exc_info=True)

        try:
            from app.services.file_projection import refresh_wiki_vaults_for_paper

            await refresh_wiki_vaults_for_paper(session, paper_id)
        except Exception:  # noqa: BLE001
            logger.warning("internal summary projection failed for %s", paper_id, exc_info=True)

        if get_settings().is_desktop:
            from app.services.obsidian_vault_bridge import enqueue_paper_projection

            await enqueue_paper_projection(paper_id=paper_id, entity_type="summary")

        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        return revision
    except Exception as exc:
        logger.exception("paper summary generation failed for %s", revision_id)
        await session.rollback()
        failed = await session.get(PaperWikiRevision, revision_id)
        if failed is not None and failed.status not in _ACTIVATABLE_REVISION_STATUSES:
            failed.status = "failed"
            failed.stage = None
            failed.error_code = type(exc).__name__[:64]
            # API/MCP clients receive a stable code; provider responses and local paths remain
            # server-side diagnostics only.
            failed.error_detail = "SUMMARY_GENERATION_FAILED"
            await session.commit()
        raise
