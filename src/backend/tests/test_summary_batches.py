"""Durable summary-batch selection, control, recovery, and capacity regressions."""

import asyncio
import uuid
from collections import Counter, defaultdict
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, insert, select

from app.core.db import get_sessionmaker
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, PaperWikiRevision
from app.models.summary_batch import SummaryBatchItem, SummaryGenerationLease
from app.models.user import User
from app.schemas.summary_batch import SummaryBatchCreate, SummarySelectionFilters
from app.services import summary_batches
from tests.conftest import register_and_login


async def _seed_library(
    *,
    count: int,
    target_count: int = 0,
    concurrency: int = 3,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Bulk seed one owned library without making large tests pay ORM N+1 costs."""
    user_id = uuid.uuid4()
    now = utcnow()
    async with get_sessionmaker()() as session:
        user = User(
            id=user_id,
            email=f"summary-batch-{user_id}@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
            display_name="Batch owner",
            settings={"summary.concurrency": concurrency},
        )
        session.add(user)
        await session.flush()
        library = DirectionLibrary(name="Summary batch library", submitted_by=user_id)
        session.add(library)
        await session.flush()
        paper_ids = [uuid.uuid4() for _ in range(count)]
        for start in range(0, count, 250):
            await session.execute(
                insert(Paper),
                [
                    {
                        "id": paper_id,
                        "source": "manual",
                        "title": (
                            f"Target snapshot paper {index:05d}"
                            if index < target_count
                            else f"Other snapshot paper {index:05d}"
                        ),
                        "created_at": now,
                        "updated_at": now,
                    }
                    for index, paper_id in enumerate(
                        paper_ids[start : start + 250], start=start
                    )
                ],
            )
            await session.execute(
                insert(LibraryPaper),
                [
                    {
                        "id": uuid.uuid4(),
                        "library_id": library.id,
                        "paper_id": paper_id,
                        "status": "included",
                        "created_at": now,
                        "updated_at": now,
                    }
                    for paper_id in paper_ids[start : start + 250]
                ],
            )
        await session.commit()
        return user_id, library.id, paper_ids


async def _create_batch(
    *,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    paper_ids: list[uuid.UUID],
    skip_existing: bool = False,
):
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        batch = await summary_batches.create_batch(
            session,
            library_id=library_id,
            user=user,
            request=SummaryBatchCreate(
                request_id=uuid.uuid4(),
                paper_ids=paper_ids,
                skip_existing=skip_existing,
            ),
        )
        await session.commit()
        return batch.id


async def test_create_batch_snapshots_7000_rows_filters_exclusions_and_idempotency(app):
    user_id, library_id, paper_ids = await _seed_library(count=7000, target_count=4000)
    all_request_id = uuid.uuid4()
    all_request = SummaryBatchCreate(
        request_id=all_request_id,
        filters=SummarySelectionFilters(status="library"),
    )

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        all_batch = await summary_batches.create_batch(
            session, library_id=library_id, user=user, request=all_request
        )
        await session.commit()
        all_batch_id = all_batch.id
        assert await session.scalar(
            select(func.count()).where(SummaryBatchItem.batch_id == all_batch_id)
        ) == 7000

        # Same receipt is a pure replay: no second batch and no duplicate snapshot items.
        replay = await summary_batches.create_batch(
            session, library_id=library_id, user=user, request=all_request
        )
        assert replay.id == all_batch_id
        assert await session.scalar(
            select(func.count()).where(SummaryBatchItem.batch_id == all_batch_id)
        ) == 7000

        conflict = SummaryBatchCreate(
            request_id=all_request_id,
            filters=SummarySelectionFilters(status="library", q="Target snapshot"),
        )
        with pytest.raises(summary_batches.SummaryBatchError) as exc:
            await summary_batches.create_batch(
                session, library_id=library_id, user=user, request=conflict
            )
        assert str(exc.value) == "SUMMARY_BATCH_REQUEST_CONFLICT"

        # 4,000 server-side matches - 949 exclusions = 3,051 immutable items.  This is well
        # beyond the UI page size and catches accidental "loaded page only" implementations.
        excluded = paper_ids[:949]
        filtered_request = SummaryBatchCreate(
            request_id=uuid.uuid4(),
            filters=SummarySelectionFilters(status="library", q="Target snapshot"),
            excluded_ids=excluded,
        )
        filtered_batch = await summary_batches.create_batch(
            session, library_id=library_id, user=user, request=filtered_request
        )
        await session.commit()
        filtered_batch_id = filtered_batch.id
        selected = set(
            await session.scalars(
                select(SummaryBatchItem.paper_id).where(
                    SummaryBatchItem.batch_id == filtered_batch_id
                )
            )
        )
        assert selected == set(paper_ids[949:4000])
        assert len(selected) == 3051

        snapshotted = await session.scalar(
            select(SummaryBatchItem).where(
                SummaryBatchItem.batch_id == filtered_batch_id,
                SummaryBatchItem.paper_id == paper_ids[949],
            )
        )
        assert snapshotted is not None
        original_title = snapshotted.title

        # Later library/Paper mutations do not silently change the durable selection or title.
        new_paper_id = uuid.uuid4()
        now = utcnow()
        session.add(
            Paper(
                id=new_paper_id,
                source="manual",
                title="Target snapshot paper added later",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            LibraryPaper(
                library_id=library_id,
                paper_id=new_paper_id,
                status="included",
                created_at=now,
                updated_at=now,
            )
        )
        source_paper = await session.get(Paper, paper_ids[949])
        assert source_paper is not None
        source_paper.title = "Renamed after snapshot"
        await session.commit()
        assert await session.scalar(
            select(func.count()).where(SummaryBatchItem.batch_id == filtered_batch_id)
        ) == 3051
        await session.refresh(snapshotted)
        assert snapshotted.title == original_title


async def test_batch_library_and_owner_checks_use_not_found_semantics(app):
    owner_id, library_id, paper_ids = await _seed_library(count=1)
    outsider_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        outsider = User(
            id=outsider_id,
            email="summary-batch-outsider@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
            display_name="Outsider",
        )
        session.add(outsider)
        await session.commit()

        with pytest.raises(summary_batches.SummaryBatchError) as exc:
            await summary_batches.create_batch(
                session,
                library_id=library_id,
                user=outsider,
                request=SummaryBatchCreate(
                    request_id=uuid.uuid4(), paper_ids=paper_ids
                ),
            )
        assert str(exc.value) == "LIBRARY_NOT_FOUND"

        owner = await session.get(User, owner_id)
        assert owner is not None
        batch = await summary_batches.create_batch(
            session,
            library_id=library_id,
            user=owner,
            request=SummaryBatchCreate(request_id=uuid.uuid4(), paper_ids=paper_ids),
        )
        await session.commit()
        with pytest.raises(summary_batches.SummaryBatchError) as exc:
            await summary_batches.owned_batch(session, library_id, batch.id, outsider)
        assert str(exc.value) == "LIBRARY_NOT_FOUND"


async def test_batch_http_scope_and_pause_resume(client, queue_stub):
    owner_token = await register_and_login(client, email="summary-http-owner@example.com")
    outsider_token = await register_and_login(client, email="summary-http-outsider@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}
    created = await client.post(
        "/api/libraries",
        json={
            "name": "HTTP summary batch library",
            "statement": "Batch summary permission regression",
        },
        headers=owner_headers,
    )
    assert created.status_code == 201, created.text
    library_id = uuid.UUID(created.json()["id"])
    paper_id = uuid.uuid4()
    now = utcnow()
    async with get_sessionmaker()() as session:
        paper = Paper(
            id=paper_id,
            source="manual",
            title="HTTP summary batch paper",
            created_at=now,
            updated_at=now,
        )
        session.add(paper)
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=library_id,
                paper_id=paper_id,
                status="included",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    response = await client.post(
        f"/api/libraries/{library_id}/summary-batches",
        json={
            "request_id": str(uuid.uuid4()),
            "paper_ids": [str(paper_id)],
            "skip_existing": False,
        },
        headers=owner_headers,
    )
    assert response.status_code == 202, response.text
    batch_id = response.json()["id"]
    assert response.json()["total"] == 1
    assert queue_stub.jobs[-1][0] == "run_paper_summary_batch_task"

    for method, url in (
        ("POST", f"/api/libraries/{library_id}/summary-batches"),
        ("GET", f"/api/libraries/{library_id}/summary-batches"),
        ("GET", f"/api/libraries/{library_id}/summary-batches/{batch_id}"),
        ("POST", f"/api/libraries/{library_id}/summary-batches/{batch_id}/pause"),
    ):
        payload = (
            {
                "request_id": str(uuid.uuid4()),
                "paper_ids": [str(paper_id)],
            }
            if method == "POST" and url.endswith("summary-batches")
            else None
        )
        denied = await client.request(method, url, json=payload, headers=outsider_headers)
        assert denied.status_code == 404, (url, denied.text)

    paused = await client.post(
        f"/api/libraries/{library_id}/summary-batches/{batch_id}/pause",
        headers=owner_headers,
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    page = await client.get(
        f"/api/libraries/{library_id}/summary-batches/{batch_id}",
        headers=owner_headers,
    )
    assert page.status_code == 200, page.text
    assert page.json()["batch"]["status"] == "paused"
    assert page.json()["items"][0]["paper_id"] == str(paper_id)

    jobs_before_resume = len(queue_stub.jobs)
    resumed = await client.post(
        f"/api/libraries/{library_id}/summary-batches/{batch_id}/resume",
        headers=owner_headers,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "queued"
    assert len(queue_stub.jobs) == jobs_before_resume + 1
    busy_retry = await client.post(
        f"/api/libraries/{library_id}/summary-batches/{batch_id}/retry",
        headers=owner_headers,
    )
    assert busy_retry.status_code == 409
    assert busy_retry.json()["detail"] == "SUMMARY_BATCH_BUSY"

async def test_capacity_is_user_wide_and_one_paper_has_only_one_generator(app):
    user_id, _library_id, paper_ids = await _seed_library(count=5, concurrency=3)
    other_user_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            User(
                id=other_user_id,
                email="summary-capacity-other@example.com",
                hashed_password="test",
                is_active=True,
                is_superuser=False,
                is_verified=True,
                display_name="Other generator",
            )
        )
        await session.commit()

    batch_lease = await summary_batches.try_acquire_capacity(user_id, paper_ids[0])
    assert batch_lease is not None
    # The single-paper generation path uses the same lease table as batches.
    async with summary_batches.summary_capacity(user_id, paper_ids[1]):
        third_lease = await summary_batches.try_acquire_capacity(user_id, paper_ids[2])
        assert third_lease is not None
        assert await summary_batches.try_acquire_capacity(user_id, paper_ids[3]) is None
        assert await summary_batches.try_acquire_capacity(user_id, paper_ids[0]) is None
        # Per-paper fencing is global, not merely per-user.
        assert await summary_batches.try_acquire_capacity(other_user_id, paper_ids[0]) is None
        async with get_sessionmaker()() as session:
            leases = list(
                await session.scalars(
                    select(SummaryGenerationLease).where(
                        SummaryGenerationLease.user_id == user_id
                    )
                )
            )
            assert len(leases) == 3
            assert {lease.slot for lease in leases} == {0, 1, 2}

    # Releasing the single-item lease immediately opens one of the same three global slots.
    replacement = await summary_batches.try_acquire_capacity(user_id, paper_ids[3])
    assert replacement is not None
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(SummaryGenerationLease).where(
                SummaryGenerationLease.id.in_(
                    [batch_lease, third_lease, replacement]
                )
            )
        )
        await session.commit()


async def test_pause_resume_retry_and_recover_unleased_items(app):
    user_id, library_id, paper_ids = await _seed_library(count=7)
    batch_id = await _create_batch(
        user_id=user_id, library_id=library_id, paper_ids=paper_ids
    )
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        batch = await summary_batches.owned_batch(
            session, library_id, batch_id, user
        )
        await summary_batches.control_batch(session, batch=batch, action="pause")
        assert batch.status == "paused"
        await summary_batches.control_batch(session, batch=batch, action="resume")
        assert batch.status == "queued"

        items = list(
            await session.scalars(
                select(SummaryBatchItem)
                .where(SummaryBatchItem.batch_id == batch_id)
                .order_by(SummaryBatchItem.paper_id)
            )
        )
        failed_for_retry, completed_for_retry = items[:2]
        failed_for_retry.status = "failed"
        failed_for_retry.error = "TRANSIENT"
        completed_for_retry.status = "completed"
        batch.status = "completed_with_errors"
        await session.flush()
        await summary_batches.control_batch(session, batch=batch, action="retry")
        assert batch.status == "queued"
        assert failed_for_retry.status == "pending"
        assert failed_for_retry.error is None
        assert completed_for_retry.status == "completed"
        batch.status = "running"
        with pytest.raises(summary_batches.SummaryBatchError) as exc:
            await summary_batches.control_batch(session, batch=batch, action="retry")
        assert str(exc.value) == "SUMMARY_BATCH_BUSY"

        # Five independent recovery outcomes: no lease, live lease, ready revision, failed
        # revision, and an expired lease.  The completed retry fixture remains untouched.
        recover_items = items[2:7]
        for item in recover_items:
            item.status = "running"
        ready_revision = PaperWikiRevision(
            paper_id=recover_items[2].paper_id,
            source_level="abstract",
            content="# ready",
            status="ready",
            stage="complete",
        )
        failed_revision = PaperWikiRevision(
            paper_id=recover_items[3].paper_id,
            source_level="abstract",
            status="failed",
            stage="compile",
            error_code="MODEL_FAILED",
        )
        session.add_all([ready_revision, failed_revision])
        await session.flush()
        recover_items[2].revision_id = ready_revision.id
        recover_items[3].revision_id = failed_revision.id
        session.add_all(
            [
                SummaryGenerationLease(
                    user_id=user_id,
                    paper_id=recover_items[1].paper_id,
                    slot=0,
                    expires_at=utcnow() + timedelta(minutes=5),
                ),
                SummaryGenerationLease(
                    user_id=user_id,
                    paper_id=recover_items[4].paper_id,
                    slot=1,
                    expires_at=utcnow() - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()

        await summary_batches.recover_batch_items(session, batch_id)
        statuses = {
            item.id: (item.status, item.error)
            for item in await session.scalars(
                select(SummaryBatchItem).where(
                    SummaryBatchItem.id.in_([item.id for item in recover_items])
                )
            )
        }
        assert statuses[recover_items[0].id][0] == "pending"
        assert statuses[recover_items[1].id][0] == "running"
        assert statuses[recover_items[2].id][0] == "completed"
        assert statuses[recover_items[3].id] == ("failed", "MODEL_FAILED")
        assert statuses[recover_items[4].id][0] == "pending"


async def test_two_batches_share_three_fake_generation_slots_and_paper_fence(
    app, monkeypatch
):
    user_id, library_id, paper_ids = await _seed_library(count=7, concurrency=3)
    first_batch_id = await _create_batch(
        user_id=user_id,
        library_id=library_id,
        paper_ids=paper_ids[:4],
        skip_existing=False,
    )
    second_batch_id = await _create_batch(
        user_id=user_id,
        library_id=library_id,
        paper_ids=[paper_ids[0], *paper_ids[4:]],
        skip_existing=False,
    )

    active = 0
    peak = 0
    active_by_paper: Counter[uuid.UUID] = Counter()
    peak_by_paper: defaultdict[uuid.UUID, int] = defaultdict(int)
    calls: Counter[uuid.UUID] = Counter()

    async def fake_queue_summary_revision(
        session, *, paper, created_by, library_id, project_id
    ):
        revision = PaperWikiRevision(
            paper_id=paper.id,
            source_level="abstract",
            status="queued",
            stage="compile",
            created_by=created_by,
            source_library_id=library_id,
            source_project_id=project_id,
        )
        session.add(revision)
        await session.flush()
        return revision

    async def fake_generate_queued_revision(
        session, *, revision_id, user_id, library_id, project_id
    ):
        del user_id, library_id, project_id
        nonlocal active, peak
        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        active += 1
        peak = max(peak, active)
        active_by_paper[revision.paper_id] += 1
        peak_by_paper[revision.paper_id] = max(
            peak_by_paper[revision.paper_id], active_by_paper[revision.paper_id]
        )
        calls[revision.paper_id] += 1
        try:
            await asyncio.sleep(0.03)
            revision.content = "# fake summary"
            revision.status = "ready"
            revision.stage = "complete"
            await session.commit()
        finally:
            active -= 1
            active_by_paper[revision.paper_id] -= 1

    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "queue_summary_revision",
        fake_queue_summary_revision,
    )
    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "generate_queued_revision",
        fake_generate_queued_revision,
    )

    await asyncio.gather(
        summary_batches.run_batch(first_batch_id),
        summary_batches.run_batch(second_batch_id),
    )

    async with get_sessionmaker()() as session:
        batches = [
            await session.get(summary_batches.SummaryBatch, batch_id)
            for batch_id in (first_batch_id, second_batch_id)
        ]
        assert [batch.status for batch in batches] == ["completed", "completed"]
        statuses = list(
            await session.scalars(
                select(SummaryBatchItem.status).where(
                    SummaryBatchItem.batch_id.in_((first_batch_id, second_batch_id))
                )
            )
        )
        assert statuses == ["completed"] * 8
        assert await session.scalar(select(func.count()).select_from(SummaryGenerationLease)) == 0

    assert peak == 3
    assert max(peak_by_paper.values()) == 1
    assert calls[paper_ids[0]] == 2


async def test_running_batch_pauses_after_inflight_items_and_resumes_pending(
    app, monkeypatch
):
    user_id, library_id, paper_ids = await _seed_library(count=4, concurrency=2)
    batch_id = await _create_batch(
        user_id=user_id,
        library_id=library_id,
        paper_ids=paper_ids,
        skip_existing=False,
    )
    two_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def fake_queue_summary_revision(
        session, *, paper, created_by, library_id, project_id
    ):
        revision = PaperWikiRevision(
            paper_id=paper.id,
            source_level="abstract",
            status="queued",
            stage="compile",
            created_by=created_by,
            source_library_id=library_id,
            source_project_id=project_id,
        )
        session.add(revision)
        await session.flush()
        return revision

    async def gated_generate(
        session, *, revision_id, user_id, library_id, project_id
    ):
        del user_id, library_id, project_id
        nonlocal started
        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        started += 1
        if started >= 2:
            two_started.set()
        await release.wait()
        revision.content = "# fake summary"
        revision.status = "ready"
        revision.stage = "complete"
        await session.commit()

    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "queue_summary_revision",
        fake_queue_summary_revision,
    )
    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "generate_queued_revision",
        gated_generate,
    )

    delivery = asyncio.create_task(summary_batches.run_batch(batch_id))
    await asyncio.wait_for(two_started.wait(), timeout=5)
    async with get_sessionmaker()() as session:
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None
        await summary_batches.control_batch(session, batch=batch, action="pause")
        await session.commit()
    release.set()
    await asyncio.wait_for(delivery, timeout=5)

    async with get_sessionmaker()() as session:
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None and batch.status == "paused"
        counts = dict(
            (
                await session.execute(
                    select(SummaryBatchItem.status, func.count())
                    .where(SummaryBatchItem.batch_id == batch_id)
                    .group_by(SummaryBatchItem.status)
                )
            ).all()
        )
        assert counts == {"completed": 2, "pending": 2}
        assert batch.runner_token is None
        await summary_batches.control_batch(session, batch=batch, action="resume")
        await session.commit()

    await asyncio.wait_for(summary_batches.run_batch(batch_id), timeout=5)
    async with get_sessionmaker()() as session:
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None and batch.status == "completed"
        statuses = list(
            await session.scalars(
                select(SummaryBatchItem.status).where(
                    SummaryBatchItem.batch_id == batch_id
                )
            )
        )
        assert statuses == ["completed"] * 4


async def test_live_batch_runner_lease_blocks_duplicate_and_expiry_allows_takeover(
    app, monkeypatch
):
    user_id, library_id, paper_ids = await _seed_library(count=1)
    batch_id = await _create_batch(
        user_id=user_id,
        library_id=library_id,
        paper_ids=paper_ids,
        skip_existing=False,
    )
    generated = 0

    async def fake_queue_summary_revision(
        session, *, paper, created_by, library_id, project_id
    ):
        revision = PaperWikiRevision(
            paper_id=paper.id,
            source_level="abstract",
            status="queued",
            stage="compile",
            created_by=created_by,
            source_library_id=library_id,
            source_project_id=project_id,
        )
        session.add(revision)
        await session.flush()
        return revision

    async def fake_generate(
        session, *, revision_id, user_id, library_id, project_id
    ):
        del user_id, library_id, project_id
        nonlocal generated
        revision = await session.get(PaperWikiRevision, revision_id)
        assert revision is not None
        generated += 1
        revision.content = "# fake summary"
        revision.status = "ready"
        revision.stage = "complete"
        await session.commit()

    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "queue_summary_revision",
        fake_queue_summary_revision,
    )
    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "generate_queued_revision",
        fake_generate,
    )

    live_token = uuid.uuid4()
    async with get_sessionmaker()() as session:
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None
        batch.runner_token = live_token
        batch.runner_expires_at = utcnow() + timedelta(minutes=5)
        await session.commit()
    await summary_batches.run_batch(batch_id)
    assert generated == 0

    async with get_sessionmaker()() as session:
        item = await session.scalar(
            select(SummaryBatchItem).where(SummaryBatchItem.batch_id == batch_id)
        )
        assert item is not None and item.status == "pending"
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None
        assert batch.runner_token == live_token
        batch.runner_expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    await asyncio.wait_for(summary_batches.run_batch(batch_id), timeout=5)
    assert generated == 1
    async with get_sessionmaker()() as session:
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None and batch.status == "completed"
        assert batch.runner_token is None
        assert batch.runner_expires_at is None


async def test_cancelling_dispatch_releases_leases_and_recovery_requeues_item(
    app, monkeypatch
):
    user_id, library_id, paper_ids = await _seed_library(count=1)
    batch_id = await _create_batch(
        user_id=user_id,
        library_id=library_id,
        paper_ids=paper_ids,
        skip_existing=False,
    )
    generation_started = asyncio.Event()
    never_release = asyncio.Event()

    async def fake_queue_summary_revision(
        session, *, paper, created_by, library_id, project_id
    ):
        revision = PaperWikiRevision(
            paper_id=paper.id,
            source_level="abstract",
            status="queued",
            stage="compile",
            created_by=created_by,
            source_library_id=library_id,
            source_project_id=project_id,
        )
        session.add(revision)
        await session.flush()
        return revision

    async def blocked_generate(
        session, *, revision_id, user_id, library_id, project_id
    ):
        del session, revision_id, user_id, library_id, project_id
        generation_started.set()
        await never_release.wait()

    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "queue_summary_revision",
        fake_queue_summary_revision,
    )
    monkeypatch.setattr(
        summary_batches.paper_summaries,
        "generate_queued_revision",
        blocked_generate,
    )

    delivery = asyncio.create_task(summary_batches.run_batch(batch_id))
    await asyncio.wait_for(generation_started.wait(), timeout=5)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery

    async with get_sessionmaker()() as session:
        batch = await session.get(summary_batches.SummaryBatch, batch_id)
        assert batch is not None
        assert batch.runner_token is None and batch.runner_expires_at is None
        assert await session.scalar(select(func.count()).select_from(SummaryGenerationLease)) == 0
        item = await session.scalar(
            select(SummaryBatchItem).where(SummaryBatchItem.batch_id == batch_id)
        )
        assert item is not None and item.status == "running"
        await summary_batches.recover_batch_items(session, batch_id)
        await session.refresh(item)
        assert item.status == "pending"
