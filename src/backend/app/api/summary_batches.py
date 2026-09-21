"""Summary settings and persistent, user-scoped library batch jobs."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.paper import PaperWikiRevision
from app.models.summary_batch import SummaryBatch, SummaryBatchItem
from app.models.user import User
from app.schemas.summary_batch import (
    SummaryBatchCreate,
    SummaryBatchItemRead,
    SummaryBatchPage,
    SummaryBatchRead,
    SummarySettings,
)
from app.services import summary_batches as service

router = APIRouter(tags=["paper-summaries"])


def http_error(exc):
    code = str(exc)
    status = (
        404 if code.endswith("NOT_FOUND") else (409 if code.endswith(("CONFLICT", "BUSY")) else 422)
    )
    return HTTPException(status, detail=code)


@router.get("/summary-settings", response_model=SummarySettings)
async def get_summary_settings(user: User = Depends(current_active_user)):
    return SummarySettings(concurrency=service.concurrency_for(user))


@router.put("/summary-settings", response_model=SummarySettings)
async def put_summary_settings(
    body: SummarySettings,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
):
    user.settings = {**(user.settings or {}), "summary.concurrency": body.concurrency}
    await session.commit()
    return body


@router.post(
    "/libraries/{library_id}/summary-batches", response_model=SummaryBatchRead, status_code=202
)
async def create_summary_batch(
    library_id: uuid.UUID,
    body: SummaryBatchCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
):
    try:
        batch = await service.create_batch(session, library_id=library_id, user=user, request=body)
        await session.commit()
    except service.SummaryBatchError as exc:
        await session.rollback()
        raise http_error(exc) from exc
    if batch.status in {"queued", "running"}:
        await service.enqueue_batch(queue, batch.id)
    return await service.read_batch(session, batch, user)


@router.get("/libraries/{library_id}/summary-batches", response_model=list[SummaryBatchRead])
async def list_summary_batches(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
):
    try:
        await service.managed_library(session, library_id, user)
    except service.SummaryBatchError as exc:
        raise http_error(exc) from exc
    batches = (
        await session.scalars(
            select(SummaryBatch)
            .where(
                SummaryBatch.library_id == library_id,
                SummaryBatch.user_id == user.id,
            )
            .order_by(SummaryBatch.created_at.desc())
            .limit(20)
        )
    ).all()
    return [await service.read_batch(session, b, user) for b in batches]


@router.get("/libraries/{library_id}/summary-batches/{batch_id}", response_model=SummaryBatchPage)
async def get_summary_batch(
    library_id: uuid.UUID,
    batch_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
):
    try:
        batch = await service.owned_batch(session, library_id, batch_id, user)
    except service.SummaryBatchError as exc:
        raise http_error(exc) from exc
    view = await service.read_batch(session, batch, user)
    rows = (
        await session.execute(
            select(SummaryBatchItem, PaperWikiRevision.stage)
            .outerjoin(
                PaperWikiRevision,
                PaperWikiRevision.id == SummaryBatchItem.revision_id,
            )
            .where(SummaryBatchItem.batch_id == batch.id)
            .order_by(SummaryBatchItem.created_at, SummaryBatchItem.id)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return SummaryBatchPage(
        batch=view,
        page=page,
        size=size,
        total=view.total,
        items=[
            SummaryBatchItemRead(
                paper_id=i.paper_id, title=i.title, status=i.status, error=i.error, stage=stage
            )
            for i, stage in rows
        ],
    )


@router.post(
    "/libraries/{library_id}/summary-batches/{batch_id}/{action}", response_model=SummaryBatchRead
)
async def control_summary_batch(
    library_id: uuid.UUID,
    batch_id: uuid.UUID,
    action: Literal["pause", "resume", "retry", "cancel"],
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
):
    try:
        batch = await service.owned_batch(session, library_id, batch_id, user)
        await service.control_batch(session, batch=batch, action=action)
        await session.commit()
    except service.SummaryBatchError as exc:
        await session.rollback()
        raise http_error(exc) from exc
    if action != "pause" and batch.status in {"queued", "running"}:
        await service.enqueue_batch(queue, batch.id)
    return await service.read_batch(session, batch, user)
