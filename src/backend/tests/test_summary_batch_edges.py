"""Adversarial cases: interrupted cleanup and cross-user queued revision reuse."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import PaperWikiRevision
from app.models.summary_batch import SummaryBatch, SummaryBatchItem, SummaryGenerationLease
from app.models.user import User
from app.schemas.summary_batch import SummaryBatchCreate, SummaryBatchRead
from app.services import summary_batches
from tests.conftest import StubQueue, register_and_login
from tests.test_summary_batches import _create_batch, _seed_library
from worker import tasks


async def test_dispatcher_cleanup_failure_does_not_poison_process_dedup(app, monkeypatch):
    user_id, library_id, papers = await _seed_library(count=1)
    batch_id = await _create_batch(user_id=user_id, library_id=library_id, paper_ids=papers)
    real_factory = get_sessionmaker()
    calls = 0

    def failing_cleanup_factory():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("temporary database connection failure during cleanup")
        return real_factory

    async def no_work(_batch_id):
        pass

    monkeypatch.setattr(summary_batches, "get_sessionmaker", failing_cleanup_factory)
    monkeypatch.setattr(summary_batches, "_run_batch", no_work)
    try:
        with pytest.raises(RuntimeError, match="temporary database"):
            await summary_batches.run_batch(batch_id)
        assert batch_id not in summary_batches._running_batches
    finally:
        summary_batches._running_batches.discard(batch_id)


async def _other_owner_revision(paper_id, library_id, user_id):
    async with get_sessionmaker()() as session:
        session.add(LibraryPaper(library_id=library_id, paper_id=paper_id, status="included"))
        revision = PaperWikiRevision(
            paper_id=paper_id, created_by=user_id, source_library_id=library_id,
            source_level="abstract", status="queued", stage="materialize",
        )
        session.add(revision)
        await session.commit()
        return revision.id


async def test_single_worker_uses_persisted_revision_owner_for_capacity(app, monkeypatch):
    caller_id, _caller_library, papers = await _seed_library(count=1)
    owner_id, owner_library, _ = await _seed_library(count=1, concurrency=1)
    revision_id = await _other_owner_revision(papers[0], owner_library, owner_id)
    acquired_for = []

    @asynccontextmanager
    async def capacity(user_id, paper_id):
        acquired_for.append((user_id, paper_id))
        yield

    async def fake_generate(*args, **kwargs):
        pass

    monkeypatch.setattr(summary_batches, "summary_capacity", capacity)
    monkeypatch.setattr(summary_batches.paper_summaries, "generate_queued_revision", fake_generate)
    # HTTP may reuse somebody else's global in-flight revision but passes the current caller.
    await tasks.generate_paper_summary_task({}, str(revision_id), str(caller_id))
    assert acquired_for == [(owner_id, papers[0])]


async def test_batch_never_generates_another_owners_revision_using_own_capacity(app, monkeypatch):
    caller_id, caller_library, papers = await _seed_library(count=1)
    owner_id, owner_library, owner_papers = await _seed_library(count=1, concurrency=1)
    revision_id = await _other_owner_revision(papers[0], owner_library, owner_id)
    # The actual revision owner's only configured slot is busy on a different paper.
    owner_busy = await summary_batches.try_acquire_capacity(owner_id, owner_papers[0])
    assert owner_busy is not None
    batch_id = await _create_batch(
        user_id=caller_id, library_id=caller_library, paper_ids=papers
    )
    async with get_sessionmaker()() as session:
        item_id = await session.scalar(select(SummaryBatchItem.id).where(
            SummaryBatchItem.batch_id == batch_id
        ))
    lease_id = await summary_batches.try_acquire_capacity(caller_id, papers[0])
    assert lease_id is not None
    generations = []

    async def fake_generate(session, *, revision_id, **kwargs):
        revision = await session.get(PaperWikiRevision, revision_id)
        generations.append(revision_id)
        revision.content = "# synthetic summary"
        revision.status = "ready"
        revision.stage = "complete"
        await session.commit()

    monkeypatch.setattr(summary_batches.paper_summaries, "generate_queued_revision", fake_generate)
    await summary_batches._process_item(item_id, lease_id)
    assert generations == [], (
        "A reused foreign queued revision must wait for its owner's worker, not generate for them",
        revision_id,
    )


async def test_cancellation_releases_children_and_recovers_unfinished_item(app, monkeypatch):
    user_id, library_id, papers = await _seed_library(count=3, concurrency=1)
    batch_id = await _create_batch(user_id=user_id, library_id=library_id, paper_ids=papers)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_generate(session, *, revision_id, **kwargs):
        revision = await session.get(PaperWikiRevision, revision_id)
        revision.status = "generating"
        await session.commit()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(summary_batches.paper_summaries, "generate_queued_revision", fake_generate)
    dispatcher = asyncio.create_task(summary_batches.run_batch(batch_id))
    await asyncio.wait_for(started.wait(), timeout=5)
    dispatcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(dispatcher, timeout=5)
    assert cancelled.is_set()
    assert batch_id not in summary_batches._running_batches
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(SummaryGenerationLease)) == 0
        batch = await session.get(SummaryBatch, batch_id)
        assert batch.runner_token is None and batch.runner_expires_at is None
        await summary_batches.recover_batch_items(session, batch_id)
        states = list(await session.scalars(select(SummaryBatchItem.status).where(
            SummaryBatchItem.batch_id == batch_id
        )))
        assert states == ["pending"] * 3


async def test_simultaneous_idempotent_requests_create_one_snapshot(app, monkeypatch):
    user_id, library_id, papers = await _seed_library(count=3)
    request = SummaryBatchCreate(request_id=uuid.uuid4(), paper_ids=papers)
    ready = asyncio.Barrier(3)
    original_execute = AsyncSession.execute
    snapshots = 0

    async def simultaneous_snapshot(self, statement, *args, **kwargs):
        nonlocal snapshots
        result = await original_execute(self, statement, *args, **kwargs)
        if (isinstance(statement, Select)
                and list(statement.selected_columns.keys()) == ["id", "title"]):
            snapshots += 1
            await ready.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", simultaneous_snapshot)

    async def create():
        async with get_sessionmaker()() as session:
            user = await session.get(User, user_id)
            batch = await summary_batches.create_batch(
                session, library_id=library_id, user=user, request=request
            )
            await session.commit()
            return batch.id

    results = await asyncio.wait_for(asyncio.gather(*(create() for _ in range(3))), timeout=10)
    assert snapshots == 3
    assert len(set(results)) == 1
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(SummaryBatch)) == 1
        assert await session.scalar(select(func.count()).select_from(SummaryBatchItem)) == 3


async def test_summary_settings_http_range_user_isolation_and_preserves_preferences(app, client):
    token = await register_and_login(client, email="summary-settings-a@example.com")
    other_token = await register_and_login(client, email="summary-settings-b@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    user_id = uuid.UUID((await client.get("/api/users/me", headers=headers)).json()["id"])
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        user.settings = {"ui.theme": "dark", "tts": {"enabled": True}}
        await session.commit()
    response = await client.get("/api/summary-settings", headers=headers)
    assert response.status_code == 200 and response.json() == {"concurrency": 3}
    for concurrency in range(1, 21):
        response = await client.put(
            "/api/summary-settings", headers=headers, json={"concurrency": concurrency}
        )
        assert response.status_code == 200, response.text
        persisted = await client.get("/api/summary-settings", headers=headers)
        assert persisted.json() == {"concurrency": concurrency}
        assert (await client.get("/api/summary-settings", headers=other_headers)).json() == {
            "concurrency": 3
        }
    for invalid in (0, -1, 21, 10000, 2.5, None, "invalid"):
        response = await client.put(
            "/api/summary-settings", headers=headers, json={"concurrency": invalid}
        )
        assert response.status_code == 422, (invalid, response.text)
    assert (await client.get("/api/summary-settings", headers=headers)).json() == {
        "concurrency": 20
    }
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user.settings == {
            "ui.theme": "dark", "tts": {"enabled": True}, "summary.concurrency": 20
        }


def test_batch_read_serializes_sqlite_naive_timestamps_as_utc():
    payload = SummaryBatchRead(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        status="queued",
        total=1,
        pending=1,
        running=0,
        completed=0,
        skipped=0,
        failed=0,
        created_at=datetime(2026, 9, 20, 22, 29, 7),
        updated_at=datetime(2026, 9, 20, 22, 30, 7),
        concurrency=10,
    ).model_dump(mode="json")
    assert payload["created_at"] == "2026-09-20T22:29:07Z"
    assert payload["updated_at"] == "2026-09-20T22:30:07Z"


async def test_twenty_capacity_slots_are_available_and_twenty_first_waits(app):
    user_id, _library_id, papers = await _seed_library(count=21, concurrency=20)
    leases = []
    for paper_id in papers[:20]:
        lease_id = await summary_batches.try_acquire_capacity(user_id, paper_id)
        assert lease_id is not None
        leases.append(lease_id)
    assert await summary_batches.try_acquire_capacity(user_id, papers[20]) is None

    async with get_sessionmaker()() as session:
        await session.execute(
            SummaryGenerationLease.__table__.delete().where(
                SummaryGenerationLease.id.in_(leases)
            )
        )
        await session.commit()


async def test_provider_outage_pauses_batch_and_keeps_remaining_items_pending(
    app, monkeypatch
):
    user_id, library_id, papers = await _seed_library(count=2, concurrency=10)
    batch_id = await _create_batch(
        user_id=user_id,
        library_id=library_id,
        paper_ids=papers,
        skip_existing=False,
    )
    async with get_sessionmaker()() as session:
        item = await session.scalar(
            select(SummaryBatchItem)
            .where(SummaryBatchItem.batch_id == batch_id)
            .order_by(SummaryBatchItem.created_at, SummaryBatchItem.id)
        )
        assert item is not None
        item_id = item.id
        paper_id = item.paper_id
    lease_id = await summary_batches.try_acquire_capacity(user_id, paper_id)
    assert lease_id is not None

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("responses request failed after 3 attempts: ConnectError")

    monkeypatch.setattr(
        summary_batches.paper_summaries, "generate_queued_revision", unavailable
    )
    await summary_batches._process_item(item_id, lease_id)

    async with get_sessionmaker()() as session:
        batch = await session.get(SummaryBatch, batch_id)
        items = list(
            await session.scalars(
                select(SummaryBatchItem)
                .where(SummaryBatchItem.batch_id == batch_id)
                .order_by(SummaryBatchItem.created_at, SummaryBatchItem.id)
            )
        )
        assert batch is not None and batch.status == "paused"
        assert [entry.status for entry in items] == ["failed", "pending"]
        assert items[0].error == "LLM_PROVIDER_UNAVAILABLE"


async def test_failed_delivery_remains_202_durable_and_recovery_redelivers(
    app, client, queue_stub, monkeypatch
):
    token = await register_and_login(client, email="summary-delivery@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    user_id = uuid.UUID((await client.get("/api/users/me", headers=headers)).json()["id"])
    _seed_user, library_id, papers = await _seed_library(count=2)
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.submitted_by = user_id
        await session.commit()

    async def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic queue unavailable")

    real_enqueue = queue_stub.enqueue
    monkeypatch.setattr(queue_stub, "enqueue", unavailable)
    payload = {"request_id": str(uuid.uuid4()), "paper_ids": [str(p) for p in papers]}
    response = await client.post(
        f"/api/libraries/{library_id}/summary-batches", headers=headers, json=payload
    )
    assert response.status_code == 202, response.text
    batch_id = uuid.UUID(response.json()["id"])
    assert response.json()["status"] == "queued" and response.json()["total"] == 2
    assert queue_stub.jobs == []
    repeated = await client.post(
        f"/api/libraries/{library_id}/summary-batches", headers=headers, json=payload
    )
    assert repeated.status_code == 202 and repeated.json()["id"] == str(batch_id)
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(SummaryBatch)) == 1
        assert await session.scalar(select(func.count()).select_from(SummaryBatchItem)) == 2
    monkeypatch.setattr(queue_stub, "enqueue", real_enqueue)
    assert await summary_batches.recover_batches(queue_stub) == 1
    assert len(queue_stub.jobs) == 1
    assert queue_stub.jobs[0][0] == "run_paper_summary_batch_task"
    assert queue_stub.jobs[0][1] == (str(batch_id),)


async def test_waiting_foreign_revision_recovers_owner_then_completes_without_llm(app, monkeypatch):
    caller_id, caller_library, papers = await _seed_library(count=1)
    owner_id, owner_library, _ = await _seed_library(count=1)
    revision_id = await _other_owner_revision(papers[0], owner_library, owner_id)
    batch_id = await _create_batch(
        user_id=caller_id, library_id=caller_library, paper_ids=papers, skip_existing=False
    )
    async with get_sessionmaker()() as session:
        item_id = await session.scalar(select(SummaryBatchItem.id).where(
            SummaryBatchItem.batch_id == batch_id
        ))
    generated = []

    async def must_not_generate(*args, **kwargs):
        generated.append(kwargs.get("revision_id"))
        raise AssertionError("A waiting foreign revision must not be generated by this batch")

    monkeypatch.setattr(
        summary_batches.paper_summaries, "generate_queued_revision", must_not_generate
    )
    lease_id = await summary_batches.try_acquire_capacity(caller_id, papers[0])
    assert lease_id is not None
    await summary_batches._process_item(item_id, lease_id)
    async with get_sessionmaker()() as session:
        item = await session.get(SummaryBatchItem, item_id)
        assert item.status == "pending" and item.revision_id == revision_id
        assert await session.scalar(select(func.count()).select_from(SummaryGenerationLease)) == 0

    class RecoveryQueue(StubQueue):
        enqueue_job = StubQueue.enqueue

    queue = RecoveryQueue()
    recovered = await tasks.recover_paper_summary_jobs_task({"redis": queue}, include_fresh=True)
    assert recovered == 1
    owner_jobs = [args for name, args, _ in queue.jobs if name == "generate_paper_summary_task"]
    assert len(owner_jobs) == 1
    assert owner_jobs[0][:3] == (str(revision_id), str(owner_id), str(owner_library))

    async with get_sessionmaker()() as session:
        revision = await session.get(PaperWikiRevision, revision_id)
        revision.status = "ready"
        revision.content = "# Owner completed the authorized generation"
        revision.stage = "complete"
        await session.commit()
    await asyncio.wait_for(summary_batches.run_batch(batch_id), timeout=5)
    assert generated == []
    async with get_sessionmaker()() as session:
        item = await session.get(SummaryBatchItem, item_id)
        batch = await session.get(SummaryBatch, batch_id)
        assert item.status == "completed" and item.revision_id == revision_id
        assert batch.status == "completed"
