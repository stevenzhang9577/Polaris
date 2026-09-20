"""Desktop-only HTTP endpoints for Zotero Local API collection synchronization."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.library_direction import DirectionLibrary
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.user import User
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
from app.schemas.zotero_local import (
    ZoteroBindingCreate,
    ZoteroBindingRead,
    ZoteroCollectionRead,
    ZoteroMaterializeRead,
    ZoteroProbeRead,
    ZoteroSyncRequest,
    ZoteroSyncRunRead,
)
from app.services import libraries as libraries_service
from app.services import zotero_local as zotero_service

router = APIRouter(tags=["zotero-local"])


def _http_error(exc: zotero_service.ZoteroLocalError) -> HTTPException:
    if exc.code in {
        "ZOTERO_LOCAL_DESKTOP_ONLY",
        "ZOTERO_INSTANCE_MISMATCH",
        "ZOTERO_SYNC_IN_PROGRESS",
    }:
        code = status.HTTP_409_CONFLICT
    elif exc.code in {"ZOTERO_LOCAL_UNAVAILABLE", "ZOTERO_LOCAL_REQUEST_FAILED"}:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif exc.code in {
        "ZOTERO_COLLECTION_NOT_FOUND",
        "ZOTERO_BINDING_NOT_FOUND",
        "ZOTERO_ITEM_LINK_NOT_FOUND",
        "ZOTERO_PDF_ATTACHMENT_NOT_FOUND",
        "ZOTERO_SYNC_RUN_NOT_FOUND",
    }:
        code = status.HTTP_404_NOT_FOUND
    elif exc.code == "ZOTERO_LOCAL_FORBIDDEN":
        code = status.HTTP_403_FORBIDDEN
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, detail=exc.code)


def _require_desktop() -> None:
    try:
        zotero_service.require_desktop_profile()
    except zotero_service.ZoteroLocalError as exc:
        raise _http_error(exc) from exc


async def _managed_library(
    session: AsyncSession, *, library_id: uuid.UUID, user: User
) -> DirectionLibrary:
    library = await libraries_service.get_library(session, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    if not await libraries_service.can_manage_library(session, library=library, user=user):
        # Do not disclose that another tenant's private library exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    return library


@router.get("/zotero-local/probe", response_model=ZoteroProbeRead)
async def probe_zotero_local(
    _user: User = Depends(current_active_user),
) -> ZoteroProbeRead:
    _require_desktop()
    try:
        async with zotero_service.ZoteroLocalClient() as client:
            result = await client.probe()
    except zotero_service.ZoteroLocalError as exc:
        if exc.code == "ZOTERO_LOCAL_UNAVAILABLE":
            return ZoteroProbeRead(available=False)
        raise _http_error(exc) from exc
    return ZoteroProbeRead(
        available=result.available,
        api_version=result.api_version,
        zotero_version=result.zotero_version,
        instance_id=result.instance_id,
    )


@router.get("/zotero-local/collections", response_model=list[ZoteroCollectionRead])
async def list_zotero_collections(
    _user: User = Depends(current_active_user),
) -> list[ZoteroCollectionRead]:
    _require_desktop()
    try:
        async with zotero_service.ZoteroLocalClient() as client:
            collections = await client.collections()
    except zotero_service.ZoteroLocalError as exc:
        raise _http_error(exc) from exc
    return [ZoteroCollectionRead(**row.__dict__) for row in collections]


@router.get(
    "/libraries/{library_id}/zotero-local-binding",
    response_model=ZoteroBindingRead,
)
async def get_zotero_binding(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ZoteroBindingRead:
    _require_desktop()
    await _managed_library(session, library_id=library_id, user=user)
    binding = await zotero_service.get_binding(session, library_id=library_id)
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ZOTERO_BINDING_NOT_FOUND")
    return ZoteroBindingRead.model_validate(binding)


@router.put(
    "/libraries/{library_id}/zotero-local-binding",
    response_model=ZoteroBindingRead,
)
async def put_zotero_binding(
    library_id: uuid.UUID,
    data: ZoteroBindingCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ZoteroBindingRead:
    _require_desktop()
    library = await _managed_library(session, library_id=library_id, user=user)
    try:
        binding = await zotero_service.bind_library(
            session,
            library=library,
            collection_key=data.collection_key,
            user_id=user.id,
        )
    except zotero_service.ZoteroLocalError as exc:
        raise _http_error(exc) from exc
    return ZoteroBindingRead.model_validate(binding)


@router.delete(
    "/libraries/{library_id}/zotero-local-binding",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_zotero_binding(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    _require_desktop()
    await _managed_library(session, library_id=library_id, user=user)
    binding = await zotero_service.get_binding(session, library_id=library_id)
    if binding is not None:
        await zotero_service.delete_binding(session, binding=binding)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/libraries/{library_id}/zotero-local-sync",
    response_model=ZoteroSyncRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_zotero_sync(
    library_id: uuid.UUID,
    data: ZoteroSyncRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> ZoteroSyncRunRead:
    _require_desktop()
    await _managed_library(session, library_id=library_id, user=user)
    binding = await zotero_service.get_binding(session, library_id=library_id)
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ZOTERO_BINDING_NOT_FOUND")
    try:
        run = await zotero_service.prepare_sync_run(
            session, binding=binding, requested_by=user.id, full=data.full
        )
        await queue.enqueue(
            "zotero_local_sync_task",
            binding_id=str(binding.id),
            requested_by=str(user.id),
            full=data.full,
            run_id=str(run.id),
            _job_id=f"zotero-local-sync-{binding.id}",
        )
    except zotero_service.ZoteroLocalError as exc:
        raise _http_error(exc) from exc
    return ZoteroSyncRunRead.model_validate(run)


@router.get(
    "/libraries/{library_id}/zotero-local-sync/status",
    response_model=ZoteroSyncRunRead | None,
)
async def get_zotero_sync_status(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ZoteroSyncRunRead | None:
    _require_desktop()
    await _managed_library(session, library_id=library_id, user=user)
    binding = await zotero_service.get_binding(session, library_id=library_id)
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ZOTERO_BINDING_NOT_FOUND")
    run = await zotero_service.latest_sync_run(session, binding_id=binding.id)
    if run is None:
        return None
    return ZoteroSyncRunRead.model_validate(run)


@router.post(
    "/libraries/{library_id}/papers/{paper_id}/zotero-local-materialize",
    response_model=ZoteroMaterializeRead,
)
async def materialize_zotero_pdf(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ZoteroMaterializeRead:
    _require_desktop()
    await _managed_library(session, library_id=library_id, user=user)
    try:
        version = await zotero_service.materialize_paper_pdf(
            session,
            paper_id=paper_id,
            user_id=user.id,
            library_id=library_id,
        )
    except zotero_service.ZoteroLocalError as exc:
        raise _http_error(exc) from exc
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ZOTERO_PDF_ATTACHMENT_NOT_FOUND")
    asset = await session.get(PaperAsset, version.asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_ASSET_NOT_FOUND")
    blob = await session.get(PdfBlob, asset.blob_id)
    link = await session.scalar(
        select(ZoteroItemLink)
        .join(
            ZoteroLocalBinding,
            ZoteroLocalBinding.id == ZoteroItemLink.binding_id,
        )
        .where(
            ZoteroLocalBinding.library_id == library_id,
            ZoteroItemLink.paper_id == paper_id,
            ZoteroItemLink.status == "active",
        )
    )
    if blob is None or link is None or not link.attachment_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ZOTERO_PDF_ATTACHMENT_NOT_FOUND")
    return ZoteroMaterializeRead(
        paper_id=paper_id,
        asset_id=asset.id,
        attachment_key=link.attachment_key,
        attachment_version=link.attachment_version,
        byte_size=blob.byte_size,
        source_locator=asset.source_locator or "",
    )
