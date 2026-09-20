"""Resumable summary batches; persistent selection, bounded work, per-user capacity."""

import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, PaperWiki, PaperWikiRevision
from app.models.summary_batch import SummaryBatch, SummaryBatchItem, SummaryGenerationLease
from app.models.user import User
from app.schemas.summary_batch import SummaryBatchCreate, SummaryBatchRead
from app.services import libraries, paper_summaries, papers

logger = logging.getLogger(__name__)
LEASE_SECONDS = 300
_running_batches: set[uuid.UUID] = set()


class SummaryBatchError(ValueError):
    pass


def concurrency_for(user: User | None) -> int:
    value = (user.settings or {}).get("summary.concurrency", 3) if user else 3
    return max(1, min(10, value)) if isinstance(value, int) else 3


async def managed_library(session, library_id, user):
    library = await session.get(DirectionLibrary, library_id)
    if library is None or not await libraries.can_manage_library(
        session, library=library, user=user
    ):
        raise SummaryBatchError("LIBRARY_NOT_FOUND")
    return library


async def owned_batch(session, library_id, batch_id, user):
    await managed_library(session, library_id, user)
    batch = await session.scalar(
        select(SummaryBatch).where(
            SummaryBatch.id == batch_id,
            SummaryBatch.library_id == library_id,
            SummaryBatch.user_id == user.id,
        )
    )
    if batch is None:
        raise SummaryBatchError("SUMMARY_BATCH_NOT_FOUND")
    return batch


async def read_batch(session, batch, user=None) -> SummaryBatchRead:
    counts = dict(
        (
            await session.execute(
                select(SummaryBatchItem.status, func.count())
                .where(SummaryBatchItem.batch_id == batch.id)
                .group_by(SummaryBatchItem.status)
            )
        ).all()
    )
    if user is None:
        user = await session.get(User, batch.user_id)
    return SummaryBatchRead(
        id=batch.id,
        library_id=batch.library_id,
        status=batch.status,
        total=sum(counts.values()),
        **{
            key: counts.get(key, 0)
            for key in ("pending", "running", "completed", "skipped", "failed")
        },
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        concurrency=concurrency_for(user),
    )


async def create_batch(session: AsyncSession, *, library_id, user, request: SummaryBatchCreate):
    await managed_library(session, library_id, user)
    selection = request.model_dump(mode="json", exclude={"request_id"})
    for key in ("paper_ids", "excluded_ids"):
        if selection.get(key) is not None:
            selection[key] = sorted(set(selection[key]))
    fingerprint = hashlib.sha256(
        json.dumps({"library_id": str(library_id), **selection}, sort_keys=True).encode()
    ).hexdigest()

    async def receipt():
        found = await session.scalar(
            select(SummaryBatch).where(
                SummaryBatch.user_id == user.id, SummaryBatch.request_id == request.request_id
            )
        )
        if found is not None and found.fingerprint != fingerprint:
            raise SummaryBatchError("SUMMARY_BATCH_REQUEST_CONFLICT")
        return found

    existing = await receipt()
    if existing:
        return existing
    # Snapshot every matching member using the same filtering service as the list UI.
    # A 50k ceiling bounds one transaction without silently truncating the selection.
    filters = request.filters.model_dump() if request.filters is not None else {"status": "library"}
    filters.pop("sort", None)
    last_sync_only = filters.pop("last_sync_only", False)
    stmt = (
        papers.apply_paper_filters(
            libraries.member_paper_stmt(library_id),
            library_ids=[library_id],
            user_id=user.id,
            **filters,
        )
        .with_only_columns(Paper.id, Paper.title)
        .where(LibraryPaper.trash_reason.is_(None))
    )
    if last_sync_only:
        stmt = stmt.where(await papers.last_sync_filter(session, [library_id]))
    if request.paper_ids is None:
        views = list((await session.execute(stmt.limit(50001))).all())
        if len(views) > 50000:
            raise SummaryBatchError("SUMMARY_BATCH_TOO_LARGE")
    else:
        # Snapshot only IDs/titles, never load thousands of summary/fulltext bodies into RAM.
        views = []
        ids = sorted(set(request.paper_ids))
        for start in range(0, len(ids), 500):
            views.extend(
                (await session.execute(stmt.where(Paper.id.in_(ids[start : start + 500])))).all()
            )
    wanted = set(request.paper_ids) if request.paper_ids is not None else None
    excluded = set(request.excluded_ids)
    selected = [v for v in views if (wanted is None or v.id in wanted) and v.id not in excluded]
    if wanted is not None and wanted - {v.id for v in selected} - excluded:
        raise SummaryBatchError("PAPER_NOT_FOUND")
    if not selected:
        raise SummaryBatchError("SUMMARY_BATCH_EMPTY")
    try:
        async with session.begin_nested():
            batch = SummaryBatch(
                user_id=user.id,
                library_id=library_id,
                request_id=request.request_id,
                fingerprint=fingerprint,
                selection=selection,
                skip_existing=request.skip_existing,
            )
            session.add(batch)
            await session.flush()
            # Small inserts avoid SQLite parameter limits even for thousands of papers.
            for start in range(0, len(selected), 250):
                await session.execute(
                    insert(SummaryBatchItem),
                    [
                        {"batch_id": batch.id, "paper_id": v.id, "title": v.title}
                        for v in selected[start : start + 250]
                    ],
                )
    except IntegrityError:
        existing = await receipt()
        if existing is None:
            raise
        return existing
    return batch


async def enqueue_batch(queue, batch_id):
    """Commit precedes delivery; periodic reconciliation retries a failed delivery."""
    try:
        enqueue = getattr(queue, "enqueue", None) or queue.enqueue_job
        await enqueue(
            "run_paper_summary_batch_task",
            str(batch_id),
            _job_id=f"summary-batch-{batch_id}-{uuid.uuid4().hex}",
        )
    except Exception:
        logger.warning("Summary batch delivery deferred: %s", batch_id)


async def control_batch(session, *, batch, action):
    if action == "pause" and batch.status in {"queued", "running"}:
        batch.status = "paused"
    elif action == "resume" and batch.status == "paused":
        batch.status = "queued"
    elif action == "retry":
        if batch.status in {"queued", "running"}:
            raise SummaryBatchError("SUMMARY_BATCH_BUSY")
        await session.execute(
            update(SummaryBatchItem)
            .where(
                SummaryBatchItem.batch_id == batch.id,
                SummaryBatchItem.status == "failed",
            )
            .values(status="pending", error=None, revision_id=None)
        )
        batch.status = "queued"
    await session.flush()


async def try_acquire_capacity(user_id, paper_id):
    """DB uniqueness enforces both a user-wide ceiling and one generator per paper."""
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise SummaryBatchError("USER_NOT_FOUND")
        now = utcnow()
        await session.execute(
            delete(SummaryGenerationLease).where(SummaryGenerationLease.expires_at < now)
        )
        slots = set(
            (
                await session.scalars(
                    select(SummaryGenerationLease.slot).where(
                        SummaryGenerationLease.user_id == user_id
                    )
                )
            ).all()
        )
        limit = concurrency_for(user)
        if len(slots) >= limit:
            await session.commit()
            return None
        slot = next((s for s in range(limit) if s not in slots), None)
        lease = SummaryGenerationLease(
            user_id=user_id,
            paper_id=paper_id,
            slot=slot,
            expires_at=now + timedelta(seconds=LEASE_SECONDS),
        )
        session.add(lease)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None
        return lease.id


@asynccontextmanager
async def hold_capacity(lease_id):
    owner = asyncio.current_task()

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(30)
                async with get_sessionmaker()() as session:
                    renewed = await session.execute(
                        update(SummaryGenerationLease)
                        .where(SummaryGenerationLease.id == lease_id)
                        .values(expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS))
                    )
                    await session.commit()
                    if renewed.rowcount != 1:
                        raise SummaryBatchError("SUMMARY_LEASE_LOST")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never keep generating after losing the fencing lease.
            if owner is not None:
                owner.cancel()

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        async with get_sessionmaker()() as session:
            await session.execute(
                delete(SummaryGenerationLease).where(SummaryGenerationLease.id == lease_id)
            )
            await session.commit()


@asynccontextmanager
async def summary_capacity(user_id, paper_id):
    lease_id = None
    while lease_id is None:
        lease_id = await try_acquire_capacity(user_id, paper_id)
        if lease_id is None:
            await asyncio.sleep(1)
    async with hold_capacity(lease_id):
        yield


async def _process_item(item_id, lease_id):
    async with hold_capacity(lease_id), get_sessionmaker()() as session:
        item = await session.get(SummaryBatchItem, item_id)
        if item is None:
            return
        batch_id = item.batch_id
        batch = await session.get(SummaryBatch, batch_id)
        if batch is None or batch.status not in {"queued", "running"}:
            return
        claimed = await session.execute(
            update(SummaryBatchItem)
            .where(SummaryBatchItem.id == item_id, SummaryBatchItem.status == "pending")
            .values(status="running", error=None)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            return
        await session.commit()
        try:
            user = await session.get(User, batch.user_id)
            if user is None or not user.is_active:
                raise SummaryBatchError("USER_NOT_FOUND")
            library = await managed_library(session, batch.library_id, user)
            membership = await session.scalar(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == batch.library_id,
                    LibraryPaper.paper_id == item.paper_id,
                    LibraryPaper.trash_reason.is_(None),
                )
            )
            if membership is None:
                raise SummaryBatchError("PAPER_NOT_FOUND")
            paper = await session.get(Paper, item.paper_id)
            awaited = (
                await session.get(PaperWikiRevision, item.revision_id) if item.revision_id else None
            )
            if awaited is not None and awaited.status in {"ready", "stale"}:
                item.status = "completed"
                await session.commit()
                return
            if awaited is not None and awaited.status == "failed":
                raise SummaryBatchError(awaited.error_code or "SUMMARY_GENERATION_FAILED")
            current = await session.scalar(
                select(PaperWiki).where(
                    PaperWiki.paper_id == item.paper_id, PaperWiki.deleted_at.is_(None)
                )
            )
            if batch.skip_existing and current is not None and current.content:
                revision = (
                    await session.get(PaperWikiRevision, current.current_revision_id)
                    if current.current_revision_id
                    else None
                )
                if revision is None or not await paper_summaries.revision_is_stale(
                    session, paper=paper, revision=revision
                ):
                    item.status = "skipped"
                    await session.commit()
                    return
            revision = await paper_summaries.queue_summary_revision(
                session,
                paper=paper,
                created_by=user.id,
                library_id=library.id,
                project_id=library.project_id,
            )
            item.revision_id = revision.id
            if revision.created_by != user.id:
                # Observe another user's already-authorized generation. Never spend their
                # credentials under our capacity lease or silently take ownership of it.
                item.status = "pending"
                await session.commit()
                return
            await session.commit()
            # A crashed generator's lease has expired; this worker now owns the paper.
            if revision.status == "generating":
                revision.status = "queued"
                await session.commit()
            await paper_summaries.generate_queued_revision(
                session,
                revision_id=revision.id,
                user_id=user.id,
                library_id=library.id,
                project_id=library.project_id,
            )
            await session.refresh(revision)
            if revision.status not in {"ready", "stale"}:
                raise SummaryBatchError(revision.error_code or "SUMMARY_GENERATION_FAILED")
            item.status = "completed"
            item.error = None
            await session.commit()
        except Exception as exc:
            await session.rollback()
            persisted_item = await session.get(SummaryBatchItem, item_id)
            revision = (
                await session.get(PaperWikiRevision, persisted_item.revision_id)
                if persisted_item is not None and persisted_item.revision_id is not None
                else None
            )
            error_code = (
                str(exc)
                if isinstance(exc, SummaryBatchError)
                else paper_summaries.summary_failure_code(exc)
            )
            if revision is not None and revision.error_code:
                error_code = revision.error_code
            await session.execute(
                update(SummaryBatchItem)
                .where(SummaryBatchItem.id == item_id)
                .values(
                    status="failed",
                    error=error_code[:128],
                )
            )
            if error_code in paper_summaries.RETRYABLE_SUMMARY_ERROR_CODES:
                await session.execute(
                    update(SummaryBatch)
                    .where(
                        SummaryBatch.id == batch_id,
                        SummaryBatch.status.in_(("queued", "running")),
                    )
                    .values(status="paused")
                )
            await session.commit()


async def recover_batch_items(session, batch_id):
    """Only reclaim items with no live generator, including after application restart."""
    rows = (
        await session.scalars(
            select(SummaryBatchItem).where(
                SummaryBatchItem.batch_id == batch_id,
                SummaryBatchItem.status == "running",
                ~select(SummaryGenerationLease.id)
                .where(
                    SummaryGenerationLease.paper_id == SummaryBatchItem.paper_id,
                    SummaryGenerationLease.expires_at > utcnow(),
                )
                .exists(),
            )
        )
    ).all()
    for item in rows:
        revision = (
            await session.get(PaperWikiRevision, item.revision_id) if item.revision_id else None
        )
        if revision and revision.status in {"ready", "stale"}:
            item.status = "completed"
        elif revision and revision.status == "failed":
            item.status = "failed"
            item.error = revision.error_code or "SUMMARY_GENERATION_FAILED"
        else:
            item.status = "pending"
    await session.commit()


async def run_batch(batch_id):
    if batch_id in _running_batches:
        return
    _running_batches.add(batch_id)
    token = uuid.uuid4()
    heartbeat = None

    async def renew():
        owner = dispatcher
        try:
            while True:
                await asyncio.sleep(30)
                async with get_sessionmaker()() as session:
                    result = await session.execute(
                        update(SummaryBatch)
                        .where(SummaryBatch.id == batch_id, SummaryBatch.runner_token == token)
                        .values(runner_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS))
                    )
                    await session.commit()
                    if result.rowcount != 1:
                        raise SummaryBatchError("SUMMARY_BATCH_LEASE_LOST")
        except asyncio.CancelledError:
            raise
        except Exception:
            owner.cancel()

    dispatcher = asyncio.current_task()
    try:
        async with get_sessionmaker()() as session:
            claim = await session.execute(
                update(SummaryBatch)
                .where(
                    SummaryBatch.id == batch_id,
                    SummaryBatch.status.in_(("queued", "running")),
                    or_(
                        SummaryBatch.runner_expires_at.is_(None),
                        SummaryBatch.runner_expires_at < utcnow(),
                    ),
                )
                .values(
                    runner_token=token,
                    runner_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS),
                )
            )
            await session.commit()
            if claim.rowcount != 1:
                return
        heartbeat = asyncio.create_task(renew())
        await _run_batch(batch_id)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        try:
            async with get_sessionmaker()() as session:
                await session.execute(
                    update(SummaryBatch)
                    .where(SummaryBatch.id == batch_id, SummaryBatch.runner_token == token)
                    .values(runner_token=None, runner_expires_at=None)
                )
                await session.commit()
        finally:
            _running_batches.discard(batch_id)


async def _run_batch(batch_id):
    """Long-running delivery with durable checkpoints; duplicate deliveries are harmless."""
    active: set[asyncio.Task] = set()
    try:
        while True:
            async with get_sessionmaker()() as session:
                batch = await session.get(SummaryBatch, batch_id)
                if batch is None:
                    break
                await recover_batch_items(session, batch_id)
                await session.refresh(batch)
                if batch.status not in {"queued", "running"} and not (
                    batch.status == "paused" and active
                ):
                    break
                await session.execute(
                    update(SummaryBatch)
                    .where(SummaryBatch.id == batch_id, SummaryBatch.status == "queued")
                    .values(status="running")
                )
                await session.commit()
                view = await read_batch(session, batch)
                if not view.pending and not view.running:
                    await session.execute(
                        update(SummaryBatch)
                        .where(
                            SummaryBatch.id == batch_id,
                            SummaryBatch.status.in_(("queued", "running")),
                        )
                        .values(status="completed_with_errors" if view.failed else "completed")
                    )
                    await session.commit()
                    break
                pending = (
                    list(
                        (
                            await session.execute(
                                select(SummaryBatchItem.id, SummaryBatchItem.paper_id)
                                .where(
                                    SummaryBatchItem.batch_id == batch_id,
                                    SummaryBatchItem.status == "pending",
                                    ~select(PaperWikiRevision.id)
                                    .where(
                                        PaperWikiRevision.id == SummaryBatchItem.revision_id,
                                        PaperWikiRevision.status.in_(("queued", "generating")),
                                        or_(
                                            PaperWikiRevision.created_by != batch.user_id,
                                            PaperWikiRevision.created_by.is_(None),
                                        ),
                                    )
                                    .exists(),
                                )
                                .order_by(SummaryBatchItem.created_at, SummaryBatchItem.id)
                                .limit(max(0, view.concurrency - len(active)))
                            )
                        ).all()
                    )
                    if batch.status != "paused"
                    else []
                )
                user_id = batch.user_id
            for item_id, paper_id in pending:
                lease_id = await try_acquire_capacity(user_id, paper_id)
                if lease_id is not None:
                    task = asyncio.create_task(_process_item(item_id, lease_id))
                    active.add(task)
            if active:
                done, active = await asyncio.wait(
                    active, timeout=1, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
            else:
                await asyncio.sleep(1)
    finally:
        # Paused batches remain in the loop until their active children finish. Exiting here
        # means shutdown/error/deletion, so cancel before waiting (not after hours of LLM work).
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def recover_batches(queue):
    async with get_sessionmaker()() as session:
        batches = list(
            (
                await session.scalars(
                    select(SummaryBatch).where(
                        SummaryBatch.status.in_(("queued", "running", "paused")),
                        or_(
                            SummaryBatch.runner_expires_at.is_(None),
                            SummaryBatch.runner_expires_at < utcnow(),
                        ),
                    )
                )
            ).all()
        )
        for batch in batches:
            await recover_batch_items(session, batch.id)
        due = [batch.id for batch in batches if batch.status != "paused"]
    for batch_id in due:
        await enqueue_batch(queue, batch_id)
    return len(due)
