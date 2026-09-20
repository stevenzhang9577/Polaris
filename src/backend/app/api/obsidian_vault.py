"""Desktop-only HTTP facade for the bidirectional Obsidian vault bridge."""

import uuid
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.config import get_settings
from app.core.db import get_session
from app.models.library_direction import DirectionLibrary
from app.models.obsidian_vault import (
    ObsidianVaultConnection,
    VaultConflict,
    VaultLibraryBinding,
)
from app.models.user import User
from app.schemas.obsidian_vault import (
    ObsidianVaultConfigure,
    ObsidianVaultStateRead,
    ObsidianVaultSyncRead,
    ObsidianVaultSyncRequest,
    VaultConflictRead,
    VaultConflictResolve,
    VaultLibraryBindingRead,
    VaultLibraryBindingUpdate,
)
from app.services import libraries as libraries_service
from app.services import obsidian_vault_bridge as bridge

router = APIRouter(prefix="/obsidian-vault", tags=["obsidian-vault"])


def _require_desktop() -> None:
    if not get_settings().is_desktop:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="OBSIDIAN_VAULT_DESKTOP_ONLY"
        )


def _bridge_error(exc: bridge.VaultBridgeError) -> HTTPException:
    if exc.code in {
        "OBSIDIAN_CONFLICT_CHANGED", "OBSIDIAN_CONFLICT_ALREADY_RESOLVED",
        "OBSIDIAN_DESTINATION_ALREADY_EXISTS",
    }:
        return HTTPException(status.HTTP_409_CONFLICT, detail=exc.code)
    if exc.code == "OBSIDIAN_CONFLICT_NOT_FOUND":
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=exc.code)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.code)


async def _my_connection(
    session: AsyncSession, user: User, *, required: bool = True
) -> ObsidianVaultConnection | None:
    connection = await bridge.get_connection(session, user_id=user.id)
    if required and connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="OBSIDIAN_VAULT_NOT_CONFIGURED")
    return connection


async def _state_response(
    session: AsyncSession, connection: ObsidianVaultConnection | None
) -> ObsidianVaultStateRead:
    if connection is None:
        return ObsidianVaultStateRead(connection=None, bindings=[], conflict_count=0)
    rows = (
        await session.execute(
            select(VaultLibraryBinding, DirectionLibrary.name)
            .join(DirectionLibrary, DirectionLibrary.id == VaultLibraryBinding.library_id)
            .where(
                VaultLibraryBinding.connection_id == connection.id,
                or_(
                    DirectionLibrary.submitted_by.is_(None),
                    DirectionLibrary.submitted_by == connection.user_id,
                ),
            )
            .order_by(VaultLibraryBinding.created_at)
        )
    ).all()
    bindings = [
        VaultLibraryBindingRead.model_validate(binding).model_copy(
            update={"library_name": library_name}
        )
        for binding, library_name in rows
    ]
    conflict_count = int(
        await session.scalar(
            select(func.count())
            .select_from(VaultConflict)
            .join(DirectionLibrary, DirectionLibrary.id == VaultConflict.library_id)
            .where(
                VaultConflict.connection_id == connection.id,
                VaultConflict.status == "open",
                or_(
                    DirectionLibrary.submitted_by.is_(None),
                    DirectionLibrary.submitted_by == connection.user_id,
                ),
            )
        )
        or 0
    )
    return ObsidianVaultStateRead(
        connection={
            "id": connection.id,
            "vault_path": connection.vault_path,
            "managed_directory": connection.managed_directory,
            "status": connection.status,
            "watching": bridge.watcher_running(connection.id),
            "last_synced_at": connection.last_synced_at,
            "last_error": connection.last_error,
            "created_at": connection.created_at,
            "updated_at": connection.updated_at,
        },
        bindings=bindings,
        conflict_count=conflict_count,
    )


@router.get("", response_model=ObsidianVaultStateRead)
async def get_vault_state(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ObsidianVaultStateRead:
    _require_desktop()
    connection = await _my_connection(session, user, required=False)
    if connection is not None and not bridge.watcher_running(connection.id):
        with suppress(bridge.VaultBridgeError):
            await bridge.start_connection_watcher(
                connection_id=connection.id,
                user_id=user.id,
                vault_path=connection.vault_path,
                managed_directory=connection.managed_directory,
            )
        # State response still exposes the persisted error/status; an unavailable removable
        # drive must not make the settings page itself unreachable.
    return await _state_response(session, connection)


@router.put("", response_model=ObsidianVaultStateRead)
async def configure_vault(
    data: ObsidianVaultConfigure,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ObsidianVaultStateRead:
    _require_desktop()
    try:
        connection = await bridge.configure_connection(
            session, user_id=user.id, vault_path=data.vault_path,
            managed_directory=data.managed_directory,
        )
    except bridge.VaultBridgeError as exc:
        raise _bridge_error(exc) from exc
    await session.commit()
    await session.refresh(connection)
    await bridge.start_connection_watcher(
        connection_id=connection.id, user_id=user.id, vault_path=connection.vault_path,
        managed_directory=connection.managed_directory,
    )
    for binding in await bridge.list_bindings(session, connection_id=connection.id):
        if binding.enabled:
            await bridge.enqueue_library_projection(library_id=binding.library_id)
    return await _state_response(session, connection)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_vault(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    _require_desktop()
    connection = await _my_connection(session, user)
    assert connection is not None
    await bridge.stop_connection_watcher(connection.id)
    await session.delete(connection)
    await session.commit()


@router.put(
    "/libraries/{library_id}", response_model=VaultLibraryBindingRead
)
async def update_library_binding(
    library_id: uuid.UUID,
    data: VaultLibraryBindingUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> VaultLibraryBindingRead:
    _require_desktop()
    connection = await _my_connection(session, user)
    assert connection is not None
    library = await libraries_service.get_library(session, library_id)
    if library is None or not await libraries_service.can_manage_library(
        session, library=library, user=user
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    binding = await bridge.set_library_binding(
        session,
        connection_id=connection.id,
        library_id=library.id,
        enabled=data.enabled,
    )
    await session.commit()
    await session.refresh(binding)
    if binding.enabled:
        await bridge.enqueue_library_projection(library_id=binding.library_id)
    return VaultLibraryBindingRead.model_validate(binding).model_copy(
        update={"library_name": library.name}
    )


@router.delete("/libraries/{library_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library_binding(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    _require_desktop()
    connection = await _my_connection(session, user)
    assert connection is not None
    binding = (
        await session.execute(
            select(VaultLibraryBinding).where(
                VaultLibraryBinding.connection_id == connection.id,
                VaultLibraryBinding.library_id == library_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="OBSIDIAN_BINDING_NOT_FOUND")
    await session.delete(binding)
    await session.commit()


@router.post("/sync", response_model=ObsidianVaultSyncRead)
async def sync_vault(
    data: ObsidianVaultSyncRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ObsidianVaultSyncRead:
    _require_desktop()
    connection = await _my_connection(session, user)
    assert connection is not None
    if data.library_id is not None:
        exists = await session.scalar(
            select(func.count())
            .select_from(VaultLibraryBinding)
            .where(
                VaultLibraryBinding.connection_id == connection.id,
                VaultLibraryBinding.library_id == data.library_id,
                VaultLibraryBinding.enabled.is_(True),
            )
        )
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="OBSIDIAN_BINDING_NOT_FOUND")
    stats = await bridge.sync_connection(
        session,
        connection=connection,
        user=user,
        library_id=data.library_id,
    )
    await session.commit()
    return ObsidianVaultSyncRead(**stats.as_dict())


@router.get("/conflicts", response_model=list[VaultConflictRead])
async def list_conflicts(
    conflict_status: str = Query(default="open", alias="status", pattern="^(open|resolved)$"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[VaultConflictRead]:
    _require_desktop()
    connection = await _my_connection(session, user)
    assert connection is not None
    conflicts = await bridge.open_conflicts(
        session, connection_id=connection.id, user=user, status=conflict_status
    )
    return [VaultConflictRead.model_validate(conflict) for conflict in conflicts]


@router.post("/conflicts/{conflict_id}/resolve", response_model=VaultConflictRead)
async def resolve_conflict(
    conflict_id: uuid.UUID,
    data: VaultConflictResolve,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> VaultConflictRead:
    _require_desktop()
    connection = await _my_connection(session, user)
    assert connection is not None
    conflict = (
        await session.execute(
            select(VaultConflict).where(
                VaultConflict.id == conflict_id,
                VaultConflict.connection_id == connection.id,
            )
        )
    ).scalar_one_or_none()
    if conflict is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="OBSIDIAN_CONFLICT_NOT_FOUND")
    library = await session.get(DirectionLibrary, conflict.library_id)
    if library is None or not await libraries_service.can_manage_library(
        session, user=user, library=library
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="OBSIDIAN_CONFLICT_NOT_FOUND")
    try:
        resolved = await bridge.resolve_conflict(
            session,
            conflict=conflict,
            strategy=data.strategy,
            content=data.content,
            expected_version=data.expected_version,
            user=user,
        )
    except bridge.VaultBridgeError as exc:
        raise _bridge_error(exc) from exc
    await session.commit()
    await session.refresh(resolved)
    return VaultConflictRead.model_validate(resolved)
