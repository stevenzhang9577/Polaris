"""Creation receipts and runtime switching must not lose work or create duplicate libraries."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.api.auth import current_active_user
from app.core import desktop_runtime
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.queue import InlineTaskQueue, get_task_queue
from app.models.library_direction import DirectionLibrary
from app.models.user import User
from app.models.zotero_local import ZoteroLibraryImport, ZoteroLocalBinding, ZoteroSyncRun
from app.services import zotero_local as zotero


class FakeZotero:
    async def probe(self):
        return zotero.ZoteroProbe(True, 3, "test", "test-instance")

    async def collections(self):
        return [zotero.ZoteroCollection("ROOT", "研究", None, 1, 0)]

    async def aclose(self):
        pass


async def setup_user(app, monkeypatch):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    monkeypatch.setattr(zotero, "ZoteroLocalClient", FakeZotero)
    async with get_sessionmaker()() as session:
        user = User(email="setup@example.test", hashed_password="unused", is_active=True)
        session.add(user)
        await session.commit()
    app.dependency_overrides[current_active_user] = lambda: user
    return user


async def test_import_replay_conflict_and_dispatch_recovery(app, client, monkeypatch):
    user = await setup_user(app, monkeypatch)

    class OfflineQueue:
        async def enqueue(self, *args, **kwargs):
            raise RuntimeError("queue offline")

    app.dependency_overrides[get_task_queue] = OfflineQueue
    body = {"request_id": str(uuid.uuid4()), "collection_key": "ROOT", "name": "研究"}
    first = await client.post("/api/zotero-local/import-library", json=body)
    assert first.status_code == 202, first.text
    assert first.json()["dispatch_pending"]
    repeat = await client.post("/api/zotero-local/import-library", json=body)
    assert repeat.json() == first.json()
    conflict = await client.post(
        "/api/zotero-local/import-library", json={**body, "name": "changed"}
    )
    assert conflict.status_code == 409
    async with get_sessionmaker()() as session:
        for model in (DirectionLibrary, ZoteroLocalBinding, ZoteroSyncRun, ZoteroLibraryImport):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert uuid.UUID(first.json()["binding_id"]) in await zotero.due_binding_ids(session)
        lib = await session.get(DirectionLibrary, uuid.UUID(first.json()["library_id"]))
        assert lib.submitted_by == user.id and not lib.is_public
    assert len((await client.get("/api/zotero-local/bindings")).json()) == 1
    app.dependency_overrides[current_active_user] = lambda: User(
        id=uuid.uuid4(), email="other@example.test"
    )
    assert (await client.get("/api/zotero-local/bindings")).json() == []


async def test_invalid_collection_rolls_back_library_and_receipt(app, client, monkeypatch):
    await setup_user(app, monkeypatch)
    response = await client.post(
        "/api/zotero-local/import-library",
        json={
            "request_id": str(uuid.uuid4()),
            "collection_key": "MISSING",
            "name": "No orphan",
        },
    )
    assert response.status_code == 404
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(DirectionLibrary)) == 0
        assert await session.scalar(select(func.count()).select_from(ZoteroLibraryImport)) == 0


async def test_concurrent_import_reuses_receipt(app, monkeypatch):
    user = await setup_user(app, monkeypatch)
    request_id = uuid.uuid4()

    async def create():
        async with get_sessionmaker()() as session:
            binding, run = await zotero.import_collection_library(
                session,
                user_id=user.id,
                request_id=request_id,
                name="Concurrent",
                collection_key="ROOT",
                statement=None,
                discipline=None,
            )
            return binding.library_id, run.id

    assert len(set(await asyncio.gather(create(), create(), create()))) == 1


async def test_runtime_barrier_blocks_new_requests_but_can_resume(app, client, monkeypatch):
    await setup_user(app, monkeypatch)
    app.dependency_overrides[get_task_queue] = lambda: InlineTaskQueue()
    try:
        response = await client.post("/api/desktop-runtime/drain", json={"paused": True})
        assert response.json() == {"active": 0, "paused": True}
        assert (await client.get("/api/libraries")).status_code == 503
        assert (await client.get("/api/health")).status_code == 200
        assert (
            await client.post("/api/desktop-runtime/drain", json={"paused": False})
        ).status_code == 200
        assert (await client.get("/api/libraries")).status_code == 200
    finally:
        desktop_runtime.set_paused(False)


async def test_runtime_barrier_counts_stream_until_last_body(monkeypatch):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    started, finish = asyncio.Event(), asyncio.Event()

    async def stream(scope, receive, send):
        started.set()
        await finish.wait()

    middleware = desktop_runtime.DesktopRuntimeBarrier(stream)
    task = asyncio.create_task(
        middleware({"type": "http", "path": "/api/chat", "method": "POST"}, None, None)
    )
    await started.wait()
    assert desktop_runtime.active_requests == 1
    finish.set()
    await task
    assert desktop_runtime.active_requests == 0


async def test_inline_children_finish_during_drain(monkeypatch):
    from worker import tasks

    monkeypatch.setattr(get_settings(), "profile", "desktop")
    started, finish = asyncio.Event(), asyncio.Event()
    completed = []
    queue = InlineTaskQueue()

    async def worker(ctx, child=False):
        if not child:
            started.set()
            await finish.wait()
            await ctx["redis"].enqueue_job("ping_task", child=True)
        completed.append(child)

    monkeypatch.setattr(tasks, "ping_task", worker)
    await queue.enqueue("ping_task")
    await started.wait()
    try:
        desktop_runtime.set_paused(True)
        with pytest.raises(RuntimeError, match="SWITCH_PENDING"):
            await queue.enqueue("ping_task")
        finish.set()
        await queue.drain()
        assert completed == [False, True]
        assert queue.active_count == 0
    finally:
        desktop_runtime.set_paused(False)


async def test_server_hides_desktop_control(app, client, monkeypatch):
    await setup_user(app, monkeypatch)
    monkeypatch.setattr(get_settings(), "profile", "server")
    assert (
        await client.post("/api/desktop-runtime/drain", json={"paused": True})
    ).status_code == 404
    assert (
        await client.post(
            "/api/zotero-local/import-library",
            json={
                "request_id": str(uuid.uuid4()),
                "collection_key": "ROOT",
                "name": "test",
            },
        )
    ).status_code == 409
