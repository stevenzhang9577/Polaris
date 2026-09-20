"""ARQ 任务。

- M1：Voyage 引擎驱动任务（run/resume）
- M2：每日文献增量 ingest（cron，见 worker/settings.py）
- M3：Idea Forge / 评审锦标赛 voyage（kind=idea_forge / idea_review，仍走 run_voyage）
- M4：Experiment Lab voyage（kind=experiment，仍走 run_voyage；SSH 执行与轮询
  在 actions_experiment 内部，命令白名单见 app/services/ssh_exec.py）
- M5-B：论文撰写 voyage（kind=paper_writing，仍走 run_voyage；分节撰写/静态校验/
  tectonic 编译在 actions_writing + services/latex_compile 内部）
- M5-C：论文评审 voyage（kind=paper_review，仍走 run_voyage；引用核验/事实查错/
  评审员×3/聚合在 actions_review + services/paper_review 内部）
"""

import logging
import uuid
from typing import Any

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.events import EventBus
from app.core.redis import get_redis
from app.models.project import Project
from app.schemas.ingest import IngestKnobs
from app.services import ingest as ingest_service
from app.services import publications as publications_service

logger = logging.getLogger(__name__)


async def ping_task(ctx: dict[str, Any], message: str = "ping") -> str:
    """连通性验证用示例任务。"""
    return f"pong: {message}"


async def parse_paper_content_task(
    ctx: dict[str, Any], version_id: str, user_id: str | None = None, library_id: str | None = None
) -> None:
    """Parse one immutable content version; MinerU adapters can be injected later."""
    from app.models.paper_content import PaperContentVersion
    from app.services.paper_content import parse_content_version, vectorize_content_version

    async with get_sessionmaker()() as session:
        version = await session.get(PaperContentVersion, uuid.UUID(version_id))
        if version is None:
            return
        await parse_content_version(session, version=version)
        try:
            await vectorize_content_version(
                session,
                version=version,
                user_id=uuid.UUID(user_id) if user_id else None,
                library_id=uuid.UUID(library_id) if library_id else None,
            )
        except Exception:
            logger.exception("content vectorization failed for %s", version_id)


async def zotero_local_sync_task(
    ctx: dict[str, Any],
    *,
    binding_id: str | None = None,
    requested_by: str | None = None,
    full: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run one durable Zotero collection sync, or dispatch all bindings currently due.

    The no-argument form is suitable for desktop startup and a 15-minute cron.  It is
    an intentional no-op in Server profile; Server exposes only the existing file import.
    """
    from app.core.config import get_settings
    from app.models.zotero_local import ZoteroLocalBinding
    from app.services.zotero_local import (
        due_binding_ids,
        execute_sync_run,
        prepare_sync_run,
    )

    if not get_settings().is_desktop:
        return {"status": "disabled", "reason": "ZOTERO_LOCAL_DESKTOP_ONLY"}
    if binding_id is None:
        async with get_sessionmaker()() as session:
            ids = await due_binding_ids(session)
        for due_id in ids:
            await ctx["redis"].enqueue_job(
                "zotero_local_sync_task",
                binding_id=str(due_id),
                _job_id=f"zotero-local-sync-{due_id}",
            )
        return {"status": "dispatched", "count": len(ids)}

    binding_uuid = uuid.UUID(binding_id)
    async with get_sessionmaker()() as session:
        binding = await session.get(ZoteroLocalBinding, binding_uuid)
        if binding is None:
            return {"status": "missing", "binding_id": binding_id}
        if run_id is None:
            run = await prepare_sync_run(
                session,
                binding=binding,
                requested_by=uuid.UUID(requested_by) if requested_by else binding.created_by,
                full=full,
            )
            run_uuid = run.id
        else:
            run_uuid = uuid.UUID(run_id)
        result = await execute_sync_run(session, run_id=run_uuid)
        if result.created or result.updated or result.missing:
            try:
                from app.services.obsidian_vault_bridge import sync_library_to_vaults

                await sync_library_to_vaults(session, library_id=binding.library_id)
                await session.commit()
            except Exception:  # noqa: BLE001 - Zotero metadata remains authoritative
                await session.rollback()
                logger.warning(
                    "Obsidian projection failed after Zotero sync for library %s",
                    binding.library_id,
                )
        return {
            "status": result.status,
            "run_id": str(result.id),
            "binding_id": str(result.binding_id),
            "total": result.total,
            "created": result.created,
            "updated": result.updated,
            "existing": result.existing,
            "ignored": result.ignored,
            "missing": result.missing,
            "failed": result.failed,
        }


async def generate_paper_summary_task(
    ctx: dict[str, Any],
    revision_id: str,
    user_id: str | None = None,
    library_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Generate one persisted summary revision and switch it current only after success."""
    from app.models.paper import PaperWikiRevision
    from app.services.paper_summaries import generate_queued_revision
    from app.services.summary_batches import summary_capacity

    async with get_sessionmaker()() as session:
        revision = await session.get(PaperWikiRevision, uuid.UUID(revision_id))
        if revision is None or revision.status not in {"queued", "generating"}:
            return
        owner_id = revision.created_by or (uuid.UUID(user_id) if user_id else None)
        paper_id = revision.paper_id

    async def generate():
        async with get_sessionmaker()() as session:
            await generate_queued_revision(
                session, revision_id=uuid.UUID(revision_id), user_id=owner_id,
                library_id=uuid.UUID(library_id) if library_id else None,
                project_id=uuid.UUID(project_id) if project_id else None,
            )

    if owner_id is None:  # Legacy system-owned jobs predate user attribution.
        await generate()
    else:
        async with summary_capacity(owner_id, paper_id):
            await generate()


async def run_paper_summary_batch_task(ctx: dict[str, Any], batch_id: str) -> None:
    from app.services.summary_batches import run_batch

    await run_batch(uuid.UUID(batch_id))


async def recover_paper_summary_jobs_task(
    ctx: dict[str, Any], *, include_fresh: bool = False
) -> int:
    """Requeue durable summary revisions orphaned by an API/worker restart.

    Startup owns every pre-existing in-flight row and can reclaim it immediately. Periodic
    reconciliation is conservative: queued rows wait two minutes and generating rows wait past
    the worker's two-hour timeout before being reset.
    """
    import time
    from datetime import timedelta

    from sqlalchemy import or_, select

    from app.models.base import utcnow
    from app.models.library_direction import DirectionLibrary, LibraryPaper
    from app.models.paper import PaperWikiRevision
    from app.models.summary_batch import SummaryBatch, SummaryBatchItem, SummaryGenerationLease
    from app.models.zotero_local import ZoteroLocalBinding
    from app.services.summary_batches import recover_batches

    now = utcnow()
    stmt = select(PaperWikiRevision).where(
        ~select(SummaryGenerationLease.id).where(
            SummaryGenerationLease.paper_id == PaperWikiRevision.paper_id,
            SummaryGenerationLease.expires_at > now,
        ).exists(),
        ~select(SummaryBatchItem.id).join(
            SummaryBatch, SummaryBatch.id == SummaryBatchItem.batch_id
        ).where(
            SummaryBatchItem.revision_id == PaperWikiRevision.id,
            SummaryBatchItem.status.in_(("pending", "running")),
            SummaryBatch.user_id == PaperWikiRevision.created_by,
        ).exists(),
        or_(
            PaperWikiRevision.status.in_(("queued", "generating")),
            (
                (PaperWikiRevision.status == "ready")
                & (PaperWikiRevision.stage == "project")
            ),
        )
    )
    if not include_fresh:
        stmt = stmt.where(
            or_(
                (
                    (PaperWikiRevision.status == "queued")
                    & (PaperWikiRevision.updated_at < now - timedelta(minutes=2))
                ),
                (
                    (PaperWikiRevision.status == "generating")
                    & (
                        (PaperWikiRevision.updated_at < now - timedelta(hours=2))
                        | (
                            PaperWikiRevision.created_by.is_not(None)
                            & (PaperWikiRevision.updated_at < now - timedelta(minutes=5))
                        )
                    )
                ),
                (
                    (PaperWikiRevision.status == "ready")
                    & (PaperWikiRevision.stage == "project")
                ),
            )
        )

    jobs: list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]] = []
    async with get_sessionmaker()() as session:
        revisions = list((await session.execute(stmt)).scalars())
        for revision in revisions:
            if revision.status == "ready":
                # The projection and ready revision were committed together; only the cosmetic
                # final stage commit was interrupted, so no LLM work needs to be repeated.
                revision.stage = "complete"
                continue
            if revision.status == "generating":
                revision.status = "queued"
                revision.stage = "materialize"
                revision.error_code = None
                revision.error_detail = None
            context = None
            if revision.source_library_id is None:
                context = (
                    await session.execute(
                        select(LibraryPaper.library_id, DirectionLibrary.project_id)
                        .join(
                            DirectionLibrary,
                            DirectionLibrary.id == LibraryPaper.library_id,
                        )
                        .outerjoin(
                            ZoteroLocalBinding,
                            ZoteroLocalBinding.library_id == LibraryPaper.library_id,
                        )
                        .where(
                            LibraryPaper.paper_id == revision.paper_id,
                            LibraryPaper.trash_reason.is_(None),
                        )
                        .order_by(
                            (ZoteroLocalBinding.id.is_not(None)).desc(),
                            LibraryPaper.created_at,
                        )
                        .limit(1)
                    )
                ).first()
            jobs.append(
                (
                    revision.id,
                    revision.created_by,
                    revision.source_library_id
                    or (context.library_id if context is not None else None),
                    revision.source_project_id
                    or (context.project_id if context is not None else None),
                )
            )
        await session.commit()

    bucket = int(time.time() // 600)
    for revision_id, user_id, library_id, project_id in jobs:
        await ctx["redis"].enqueue_job(
            "generate_paper_summary_task",
            str(revision_id),
            str(user_id) if user_id else None,
            str(library_id) if library_id else None,
            str(project_id) if project_id else None,
            _job_id=f"paper-summary-recovery-{revision_id}-{bucket}",
        )
    await recover_batches(ctx["redis"])
    return len(revisions)


async def purge_deleted_paper_summaries_task(ctx: dict[str, Any]) -> int:
    """Permanently remove summary revisions after the 30-day soft-delete window."""
    from app.services.paper_summaries import purge_expired_summaries

    async with get_sessionmaker()() as session:
        purged = await purge_expired_summaries(session)
        await session.commit()
    return purged


async def purge_obsidian_vault_tombstones_task(ctx: dict[str, Any]) -> int:
    """Remove expired bridge bookkeeping without deleting papers, PDFs, or notes."""
    from app.services.obsidian_vault_bridge import purge_expired_tombstones

    async with get_sessionmaker()() as session:
        purged = await purge_expired_tombstones(session)
        await session.commit()
    return purged


async def purge_deleted_paper_notes_task(ctx: dict[str, Any]) -> int:
    """Permanently remove private note tombstones after the 30-day recovery window."""
    from app.services.notes import purge_expired_notes

    async with get_sessionmaker()() as session:
        return await purge_expired_notes(session)


async def sync_obsidian_vault_paper_task(
    ctx: dict[str, Any],
    paper_id: str,
    user_id: str | None = None,
    entity_type: str | None = None,
) -> dict[str, Any]:
    """Incrementally project one changed paper without rescanning every paper in a library."""
    from app.core.config import get_settings
    from app.services.obsidian_vault_bridge import sync_paper_to_vaults

    if not get_settings().is_desktop:
        return {"status": "disabled", "reason": "OBSIDIAN_VAULT_DESKTOP_ONLY"}
    entity_types = frozenset({entity_type}) if entity_type else None
    async with get_sessionmaker()() as session:
        stats = await sync_paper_to_vaults(
            session,
            paper_id=uuid.UUID(paper_id),
            user_id=uuid.UUID(user_id) if user_id else None,
            entity_types=entity_types,
        )
        await session.commit()
    return stats.as_dict()


async def sync_obsidian_vault_library_task(
    ctx: dict[str, Any], library_id: str, entity_type: str | None = None
) -> dict[str, Any]:
    """Project one changed library to all enabled local Vault connections."""
    from app.core.config import get_settings
    from app.services.obsidian_vault_bridge import sync_library_to_vaults

    if not get_settings().is_desktop:
        return {"status": "disabled", "reason": "OBSIDIAN_VAULT_DESKTOP_ONLY"}
    entity_types = frozenset({entity_type}) if entity_type else None
    async with get_sessionmaker()() as session:
        stats = await sync_library_to_vaults(
            session,
            library_id=uuid.UUID(library_id),
            entity_types=entity_types,
        )
        await session.commit()
    return stats.as_dict()


def _make_engine() -> VoyageEngine:
    return VoyageEngine(event_bus=EventBus(get_redis()))


async def run_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """驱动一次新航程（POST /voyages 入队）。"""
    await _make_engine().run(uuid.UUID(run_id))


async def resume_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """闸门批准后从断点恢复航程（gates approve 入队）。"""
    await _make_engine().resume(uuid.UUID(run_id))


RECONCILE_DEDUP_WINDOW_SECONDS = 900  # 同一 voyage 15 分钟内只入队一次 resume
RECONCILE_STALE_MINUTES = 30  # 周期回收：终端无动静超过这个时长才算僵死


def _reconcile_job_id(vid: object, now: float) -> str:
    """时间分桶的去重键。arq 对已有同 id 的任务（排队/在跑/**结果保留期内**）会静默
    去重——keep_result 默认 1 小时，固定 id 意味着重启后的对账 enqueue 可能被一小时前
    的旧结果吞掉（线上实测：voyage 卡 verifying 45 分钟无人认领）。分桶让去重只在
    短窗口内生效。"""
    return f"reconcile-resume-{vid}-{int(now // RECONCILE_DEDUP_WINDOW_SECONDS)}"


async def reconcile_stuck_voyages(ctx: dict[str, Any]) -> None:
    """worker 启动对账：认领无人执行的在途航程（见 IN_FLIGHT_STATUSES）。

    被 SIGTERM/超时打断的 ARQ 任务按任务年龄指数延迟重试，长航程会被晾数小时
    （实测：远端 run.sh 已 exit=0，平台侧 50 分钟无人收尾）。启动时把在途
    状态的 voyage 重新入队 resume——引擎幂等（setup/run 都会重挂在跑的远端进程，
    checkpoint 断点恢复）。"""
    import time

    from sqlalchemy import select

    from app.models.voyage import IN_FLIGHT_STATUSES, VoyageRun

    async with get_sessionmaker()() as session:
        ids = (
            (
                await session.execute(
                    select(VoyageRun.id).where(
                        VoyageRun.status.in_(tuple(IN_FLIGHT_STATUSES))
                    )
                )
            )
            .scalars()
            .all()
        )
    now = time.time()
    for vid in ids:
        await ctx["redis"].enqueue_job(
            "resume_voyage", str(vid), _job_id=_reconcile_job_id(vid, now)
        )
    await recover_paper_summary_jobs_task(ctx, include_fresh=True)


async def reconcile_stale_voyages(
    ctx: dict[str, Any], stale_minutes: int = RECONCILE_STALE_MINUTES
) -> None:
    """周期回收（cron）：在途但终端长时间无动静的 voyage 重新入队 resume。

    启动对账只救 worker 重启这一种孤儿；任务在运行中途丢失（LLM 调用悬死后被杀、
    ARQ 指数延迟重试晾着）产生的僵死靠这里兜底。判据保守：距最后一条终端日志
    （无日志则取创建时间）超过 ``stale_minutes`` 才认领——活着的长步骤会持续产生
    日志/轮询输出，短暂静默不会被误抢；引擎本身幂等，偶发的并发 resume 可容忍
    （arq 中断重试与启动对账本就可能重叠，线上已验证无碍）。"""
    import time
    from datetime import timedelta

    from sqlalchemy import func as sa_func
    from sqlalchemy import or_, select

    from app.models.base import utcnow
    from app.models.voyage import IN_FLIGHT_STATUSES, VoyageRun, VoyageTerminalLog

    cutoff = utcnow() - timedelta(minutes=stale_minutes)
    last_log = (
        select(
            VoyageTerminalLog.run_id.label("run_id"),
            sa_func.max(VoyageTerminalLog.at).label("last_at"),
        )
        .group_by(VoyageTerminalLog.run_id)
        .subquery()
    )
    async with get_sessionmaker()() as session:
        ids = (
            (
                await session.execute(
                    select(VoyageRun.id)
                    .outerjoin(last_log, last_log.c.run_id == VoyageRun.id)
                    .where(
                        VoyageRun.status.in_(tuple(IN_FLIGHT_STATUSES)),
                        or_(
                            last_log.c.last_at < cutoff,
                            (last_log.c.last_at.is_(None)) & (VoyageRun.created_at < cutoff),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    if not ids:
        return
    logger.warning("reclaiming %d stale in-flight voyage(s): %s", len(ids), ids)
    now = time.time()
    for vid in ids:
        await ctx["redis"].enqueue_job(
            "resume_voyage", str(vid), _job_id=_reconcile_job_id(vid, now)
        )


async def watch_unanswered_managed_commands(ctx: dict[str, Any]) -> int:
    """Enforce the unattended GPU wait policy for open managed-command asks."""
    from app.core.events import EventBus
    from app.services.managed_command_watchdog import check_unanswered_managed_commands

    async with get_sessionmaker()() as session:
        events = await check_unanswered_managed_commands(session)
    bus = EventBus(ctx["redis"])
    for event in events:
        await bus.publish_voyage_event(
            event.voyage_id,
            "ask.updated",
            {"message": event.message, "action": event.action},
        )
        if event.user_id is not None:
            await bus.publish_notify(
                event.user_id,
                {
                    "type": "voyage.ask.updated",
                    "voyage_id": str(event.voyage_id),
                    "action": event.action,
                    "used_memory_mib": event.used_memory_mib,
                },
            )
    return len(events)


async def daily_wiki_ingest(ctx: dict[str, Any]) -> list[str]:
    """给已建库的**文献库**入队同步。由每日论文抓取跑完后触发（daily.sync_libraries）。

    不再自己定时：定时就意味着赌抓取已经跑完，抓取慢一点或失败重试，同步就会在旧池子
    上空跑一整轮，而界面上只显示「0 篇新论文」。每天只跑一轮（同一天重复触发会被挡）。

    按库选而不是按课题选：独立库（project_id 为空）在按课题遍历的老写法里一个都进不来。

    每个库单独兜异常：一个库启动失败不能打断整个循环，否则排在它后面的库当天
    全都不同步，而且只在 arq 日志里留个异常，界面上完全无感（月度预算硬限额
    时代实测发生过；硬限额已随 #734 移除，这条纪律保留防其他异常复现同样的坑）。

    返回本次入队的 voyage id 列表（arq 结果可查）。
    """
    enqueued: list[str] = []
    async with get_sessionmaker()() as session:
        # 「今天只跑一轮」的判据在 find_due_daily_libraries 里，且是**按库**算的。
        # 这里以前是一道全局闸门（今天有任意一条 wiki_ingest 就整轮返回），于是
        # 任何人手动同步任何一个库，当天其余所有库的自动同步全部被吞掉。
        # 先回收长期卡住的 paused_error：它们会把所在库一直挡在互斥判定外面
        reclaimed = await ingest_service.reclaim_stale_paused_ingests(session)
        if reclaimed:
            logger.warning("reclaimed %d stale paused ingest run(s)", len(reclaimed))

        libraries = await ingest_service.find_due_daily_libraries(session)
        for library in libraries:
            project = (
                await session.get(Project, library.project_id)
                if library.project_id is not None
                else None
            )
            try:
                run = await ingest_service.create_ingest_voyage(
                    session,
                    library=library,
                    project=project,
                    mode="incremental",
                    knobs=IngestKnobs(),
                    created_by=None,
                )
            except ingest_service.IngestConflictError:
                continue  # 并发保护：查表与建 run 之间有人手动触发
            except Exception:  # noqa: BLE001 — 单个库出问题不能拖垮当天其余的库
                logger.exception("daily ingest failed to start for library %s", library.id)
                await session.rollback()
                continue
            await ctx["redis"].enqueue_job("run_voyage", str(run.id))
            enqueued.append(str(run.id))
    return enqueued


async def match_user_publications(ctx: dict[str, Any], user_id: str) -> int:
    """扫描文献库为某用户匹配发表候选（姓名+机构命中 → pending）；返回新增数。"""
    async with get_sessionmaker()() as session:
        return await publications_service.match_from_library(session, user_id=uuid.UUID(user_id))


async def index_papers_fulltext_task(
    ctx: dict[str, Any], scope: str, user_id: str, project_id: str | None = None
) -> dict[str, Any]:
    """可选全文索引：按 scope 解析论文集合，批量抓 PDF→分段→嵌入（文献对话检索底座）。

    scope=="shelf" → 课题相关研究书架论文（需 project_id）；
    scope=="personal" → 本人收藏的个人库论文。
    """
    from app.core.llm.router import get_llm_router
    from app.services.fulltext_index import index_papers_fulltext
    from app.services.topic_shelf import shelf_paper_ids
    from app.services.user_library import personal_paper_ids

    uid = uuid.UUID(user_id)
    async with get_sessionmaker()() as session:
        if scope == "shelf":
            if project_id is None:
                raise ValueError("shelf scope requires project_id")
            paper_ids = await shelf_paper_ids(session, project_id=uuid.UUID(project_id))
        elif scope == "personal":
            paper_ids = await personal_paper_ids(session, user_id=uid, tab="saved")
        else:
            raise ValueError(f"unknown scope: {scope}")
        return await index_papers_fulltext(
            session, paper_ids=paper_ids, llm=get_llm_router(), user_id=uid
        )


async def daily_feed_sync(ctx: dict[str, Any]) -> str | None:
    """检查点任务（每 15 分钟一次）：**探到今天那批公告出来了**才抓。

    起始时刻可配置（默认 UTC 01:30 = 北京 09:30），从那时起每 15 分钟探一次，直到
    arXiv 放出当天批次。不定死"几点抓"是因为发布时刻会飘，赌一个固定点赌输的表现
    是拿到上一批、去重后一条不进、每一步却都报成功。

    先探再建任务，不是建了任务让它失败——否则从早到晚会攒一堆 paused_error。

    arq 的 cron 时刻在 worker 启动时固定，改设置得重启才生效，所以让 cron 空转、
    由设置决定是否动手。空转一次只是一条查询加一次 RSS 探测。

    返回入队的 voyage id；未到点 / 今天已跑过 / 今天那批还没出来，都返回 None。
    """
    import datetime as dt

    from app.services import daily_feed as daily_feed_service

    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        if not await daily_feed_service.due_now(session, now=now):
            return None
        if await daily_feed_service.already_ran_today(
            session, daily_feed_service.DAILY_FEED_VOYAGE_KIND, now=now
        ):
            return None
        # 探测有次数上限：「今天 arXiv 就是没发」是正常情况（周末、节假日、发布故障），
        # 不该从早探到晚每 15 分钟敲一次。但**探满不等于当天收工**：/new 只带当天那批，
        # 今天的公告错过了就永久没有了，所以探满之后转成一小时一次的复查。
        max_attempts = await daily_feed_service.get_max_probe_attempts(session)
        state = await daily_feed_service.probe_state(session, now=now)
        if not daily_feed_service.should_probe_now(state, now=now, max_attempts=max_attempts):
            return None
        fresh, batch_date = await daily_feed_service.todays_batch_available(session)
        if not fresh:
            state = await daily_feed_service.record_probe(
                session,
                now=now,
                batch_date=batch_date,
                exhausted=state["attempts"] + 1 >= max_attempts,
            )
            if state["exhausted"]:
                logger.info(
                    "daily feed probe exhausted after %s attempts (latest batch %s); "
                    "no new announcement today",
                    state["attempts"],
                    batch_date,
                )
            else:
                logger.info(
                    "daily feed not published yet (latest batch %s), probe %s/%s",
                    batch_date,
                    state["attempts"],
                    max_attempts,
                )
            return None
        try:
            run = await daily_feed_service.create_daily_feed_voyage(session, created_by=None)
        except daily_feed_service.DailyFeedConflictError:
            return None  # 并发保护：查表与建 run 之间有 admin 手动触发
    await ctx["redis"].enqueue_job("run_voyage", str(run.id))
    return str(run.id)


#: 发表匹配排在库同步之后多久（新入库论文要先落库，匹配才有东西可匹）
_PUBLICATION_MATCH_DELAY_MINUTES = 150


async def daily_publication_match(ctx: dict[str, Any]) -> int:
    """检查点任务：库同步之后再跑发表匹配（新入库论文 → 姓名+机构命中进待确认）。

    时刻同样跟随每日池抓取时间派生，不写死。
    """
    import datetime as dt

    from app.services import daily_feed as daily_feed_service

    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        if not await daily_feed_service.due_now(
            session, now=now, delay_minutes=_PUBLICATION_MATCH_DELAY_MINUTES
        ):
            return 0
        if not await daily_feed_service.claim_today(
            session, "daily_publication_match_last_run", now=now
        ):
            return 0
        user_ids = await publications_service.profiles_for_daily_match(session)
    total = 0
    for uid in user_ids:
        async with get_sessionmaker()() as session:
            total += await publications_service.match_from_library(session, user_id=uid)
    return total


async def run_literature_discovery(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Execute one persisted library literature-discovery run."""
    from app.services.literature.runtime import run_discovery

    async with get_sessionmaker()() as session:
        run = await run_discovery(session, uuid.UUID(run_id))
        return {
            "run_id": str(run.id),
            "status": run.status,
            "returned_count": (run.progress or {}).get("returned_count", 0),
        }


async def dispatch_literature_discovery_schedules(ctx: dict[str, Any]) -> list[str]:
    """Create due incremental runs and idempotently dispatch them to ARQ."""

    from datetime import UTC, datetime

    from app.services.literature import discovery_schedules

    current = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        run_ids = await discovery_schedules.claim_due_schedules(session, now=current)
    dispatched: list[str] = []
    bucket = int(current.timestamp() // (15 * 60))
    for run_id in run_ids:
        ok = False
        try:
            await ctx["redis"].enqueue_job(
                "run_literature_discovery",
                str(run_id),
                _job_id=f"scheduled-literature-{run_id}-{bucket}",
            )
            ok = True
            dispatched.append(str(run_id))
        except Exception:
            logger.exception("scheduled literature dispatch failed for %s", run_id)
        async with get_sessionmaker()() as session:
            await discovery_schedules.record_dispatch_result(
                session,
                run_id=run_id,
                ok=ok,
                now=current,
            )
    return dispatched


async def translate_literature_hit(ctx: dict[str, Any], translation_id: str) -> dict[str, Any]:
    """Translate one discovery hit through the dedicated versioned LLM route."""

    from sqlalchemy import select

    from app.core.llm.router import get_llm_router
    from app.models.literature_discovery import (
        LiteratureHitTranslation,
        LiteratureSearchHit,
        LiteratureSearchRun,
    )
    from app.services.literature.translations import execute_translation

    del ctx
    async with get_sessionmaker()() as session:
        identity = await session.execute(
            select(LiteratureHitTranslation.requested_by, LiteratureSearchRun.library_id)
            .join(LiteratureSearchHit, LiteratureSearchHit.run_id == LiteratureSearchRun.id)
            .join(
                LiteratureHitTranslation,
                LiteratureHitTranslation.hit_id == LiteratureSearchHit.id,
            )
            .where(LiteratureHitTranslation.id == uuid.UUID(translation_id))
        )
        owner = identity.one_or_none()
        row = await execute_translation(
            session,
            translation_id=uuid.UUID(translation_id),
            llm=get_llm_router(),
            user_id=owner.requested_by if owner else None,
            library_id=owner.library_id if owner else None,
        )
        return {
            "translation_id": translation_id,
            "status": row.status if row is not None else "missing",
        }


async def zotero_import(
    ctx: dict[str, Any],
    *,
    task_id: str,
    bib_path: str,
    zip_path: str | None,
    library_id: str,
    user_id: str,
    project_id: str | None = None,
) -> dict[str, int]:
    """Zotero 库导入（#638）：API 侧把上传暂存到 data_dir 后入队，这里逐条落库。

    文件走共享数据卷（api 与 worker 同挂 /srv/data），进度与结果按 paper-task
    事件口径发（任务归属在 API 入队前已注册），前端订阅
    /paper-tasks/{task_id}/events。返回汇总计数（arq 结果可查）。
    """
    from app.services.zotero_import import run_zotero_import

    return await run_zotero_import(
        ctx["redis"],
        task_id=task_id,
        bib_path=bib_path,
        zip_path=zip_path or None,
        library_id=uuid.UUID(library_id),
        user_id=uuid.UUID(user_id),
        project_id=uuid.UUID(project_id) if project_id else None,
    )


async def full_export(ctx: dict[str, Any], *, task_id: str, user_id: str) -> dict[str, Any]:
    """一键全量导出（#690）：把用户全部数据面打包成 zip 供下载。

    zip 落共享数据卷 data_dir/exports/<task_id>.zip（api 侧下载端点直接读），
    进度与完成事件走 paper-task 通道（归属在 API 入队前已注册），并发锁
    （每用户同时一个）由任务结束时释放。
    """
    from app.services.full_export import run_full_export_task

    return await run_full_export_task(ctx["redis"], task_id=task_id, user_id=user_id)
