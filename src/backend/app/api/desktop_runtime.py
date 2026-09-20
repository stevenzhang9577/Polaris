"""Authenticated Desktop runtime maintenance control; never exposed on Server."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import current_active_user
from app.core import desktop_runtime
from app.core.config import get_settings
from app.core.queue import InlineTaskQueue, get_task_queue
from app.models.user import User

router = APIRouter()


class DrainRequest(BaseModel):
    paused: bool


@router.post("/desktop-runtime/drain")
async def drain_runtime(data: DrainRequest, _user: User = Depends(current_active_user)):
    if not get_settings().is_desktop:
        raise HTTPException(404)
    desktop_runtime.set_paused(data.paused)
    queue = await get_task_queue()
    jobs = queue.active_count if isinstance(queue, InlineTaskQueue) else 0
    from app.services.obsidian_vault_bridge import active_vault_syncs

    return {
        "active": jobs + desktop_runtime.active_requests + active_vault_syncs(),
        "paused": data.paused,
    }
