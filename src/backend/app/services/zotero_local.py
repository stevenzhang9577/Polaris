"""Read-only Zotero Desktop Local API adapter and collection reconciliation.

The adapter deliberately owns no FastAPI concepts.  HTTP routes, the CLI, scheduled
jobs, and the inline desktop queue all call this module so collection semantics cannot
drift between entry points.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper, new_paper
from app.models.paper_assets import PaperAsset
from app.models.paper_content import PaperContentVersion
from app.models.user import User
from app.models.zotero_local import (
    ZoteroItemLink,
    ZoteroLibraryImport,
    ZoteroLocalBinding,
    ZoteroSyncRun,
)
from app.services.dedup import pool_dedup_key
from app.services.libraries import ensure_membership, get_membership
from app.services.paper_assets import MAX_PDF_BYTES, AssetError, create_or_reuse_asset

ZOTERO_LOCAL_BASE_URL = "http://127.0.0.1:23119/api"
SUPPORTED_ITEM_TYPES = frozenset({"journalArticle", "conferencePaper", "preprint", "thesis"})
SYNC_INTERVAL = timedelta(minutes=15)
MAX_ERROR_SAMPLES = 25
_ITEM_BATCH_SIZE = 50
_ARXIV_RE = re.compile(
    r"(?i)(?:arxiv\s*[:=]?\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?"
)
_YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)")


class ZoteroLocalError(RuntimeError):
    """Expected Local API, validation, or synchronization error."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


class ZoteroDesktopOnlyError(ZoteroLocalError):
    def __init__(self) -> None:
        super().__init__("ZOTERO_LOCAL_DESKTOP_ONLY")


@dataclass(frozen=True)
class ZoteroProbe:
    available: bool
    api_version: int | None
    zotero_version: str | None
    instance_id: str | None


@dataclass(frozen=True)
class ZoteroCollection:
    key: str
    name: str
    parent_key: str | None
    version: int
    child_count: int = 0


@dataclass(frozen=True)
class ItemVersionSnapshot:
    versions: dict[str, int]
    library_version: int | None


@dataclass(frozen=True)
class MaterializedZoteroAsset:
    asset: PaperAsset
    attachment_key: str
    attachment_version: int | None
    byte_size: int


@dataclass(frozen=True)
class ZoteroAttachmentRef:
    key: str
    version: int


def require_desktop_profile() -> None:
    if not get_settings().is_desktop:
        raise ZoteroDesktopOnlyError()


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


class ZoteroLocalClient:
    """Small typed facade over Zotero's official localhost API."""

    def __init__(
        self,
        *,
        base_url: str = ZOTERO_LOCAL_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Zotero Local API URL must be loopback HTTP")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Zotero-API-Version": "3"},
        )

    async def __aenter__(self) -> ZoteroLocalClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}" if path else f"{self.base_url}/"

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        allow_redirect_response: bool = False,
    ) -> httpx.Response:
        try:
            response = await self._client.get(self._url(path), params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ZoteroLocalError("ZOTERO_LOCAL_UNAVAILABLE", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ZoteroLocalError("ZOTERO_LOCAL_REQUEST_FAILED", str(exc)) from exc
        if response.status_code == 403:
            raise ZoteroLocalError("ZOTERO_LOCAL_FORBIDDEN")
        if response.status_code == 404:
            raise ZoteroLocalError("ZOTERO_LOCAL_NOT_FOUND")
        if allow_redirect_response and 300 <= response.status_code < 400:
            return response
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ZoteroLocalError(
                "ZOTERO_LOCAL_REQUEST_FAILED", f"HTTP {response.status_code}"
            ) from exc
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ZoteroLocalError("ZOTERO_LOCAL_INVALID_JSON") from exc

    @staticmethod
    def _header_int(response: httpx.Response, name: str) -> int | None:
        raw = response.headers.get(name)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    async def probe(self) -> ZoteroProbe:
        response = await self._request("")
        # Zotero 10 returns a deliberately plain-text root body ("Nothing to see
        # here."); version and instance identity are response headers.  Older builds
        # may return JSON, so retain that as a compatibility fallback.
        payload: dict[str, Any] = {}
        if "json" in response.headers.get("content-type", ""):
            parsed = self._json(response)
            if isinstance(parsed, dict):
                payload = parsed
        api_version = (
            response.headers.get("Zotero-API-Version")
            or payload.get("apiVersion")
            or payload.get("api_version")
        )
        try:
            parsed_api_version = int(api_version) if api_version is not None else None
        except (TypeError, ValueError):
            parsed_api_version = None
        return ZoteroProbe(
            available=True,
            api_version=parsed_api_version,
            zotero_version=_text(
                response.headers.get("X-Zotero-Version")
                or payload.get("zoteroVersion")
                or payload.get("version")
            ),
            instance_id=_text(
                response.headers.get("Zotero-Server-ID")
                or response.headers.get("Zotero-Instance")
                or payload.get("instanceID")
                or payload.get("instanceId")
            ),
        )

    async def collections(self) -> list[ZoteroCollection]:
        rows: list[dict[str, Any]] = []
        start = 0
        limit = 100
        while True:
            response = await self._request(
                "users/0/collections", params={"start": start, "limit": limit}
            )
            payload = self._json(response)
            if not isinstance(payload, list):
                raise ZoteroLocalError("ZOTERO_LOCAL_INVALID_RESPONSE")
            page = [row for row in payload if isinstance(row, dict)]
            rows.extend(page)
            total = self._header_int(response, "Total-Results")
            if not page or (total is not None and len(rows) >= total) or len(page) < limit:
                break
            start += len(page)

        child_counts: dict[str, int] = {}
        normalized: list[ZoteroCollection] = []
        for row in rows:
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            key = _text(row.get("key") or data.get("key"))
            name = _text(data.get("name"))
            if not key or not name:
                continue
            raw_parent = data.get("parentCollection")
            # Zotero 10 serializes a root collection's parent as JSON ``false`` rather than
            # null/empty. Treat it as the absence of a parent, not the literal string "False".
            parent = None if raw_parent is False else _text(raw_parent)
            if parent:
                child_counts[parent] = child_counts.get(parent, 0) + 1
            normalized.append(
                ZoteroCollection(
                    key=key,
                    name=name,
                    parent_key=parent,
                    version=_int(row.get("version") or data.get("version")),
                )
            )
        return [
            ZoteroCollection(
                key=row.key,
                name=row.name,
                parent_key=row.parent_key,
                version=row.version,
                child_count=child_counts.get(row.key, 0),
            )
            for row in normalized
        ]

    async def collection_item_versions(self, collection_key: str) -> ItemVersionSnapshot:
        versions: dict[str, int] = {}
        library_version: int | None = None
        start = 0
        limit = 100
        while True:
            response = await self._request(
                f"users/0/collections/{collection_key}/items/top",
                params={
                    "format": "versions",
                    "itemType": "-attachment",
                    "start": start,
                    "limit": limit,
                },
            )
            payload = self._json(response)
            if not isinstance(payload, dict):
                raise ZoteroLocalError("ZOTERO_LOCAL_INVALID_RESPONSE")
            page = {str(key): _int(value) for key, value in payload.items()}
            versions.update(page)
            page_version = self._header_int(response, "Last-Modified-Version")
            if page_version is not None:
                library_version = max(library_version or 0, page_version)
            total = self._header_int(response, "Total-Results")
            if not page or (total is not None and len(versions) >= total) or len(page) < limit:
                break
            start += len(page)
        return ItemVersionSnapshot(versions=versions, library_version=library_version)

    async def items_by_keys(self, item_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for batch in _chunks(list(dict.fromkeys(item_keys)), _ITEM_BATCH_SIZE):
            # Local Zotero can expand a requested parent into additional records. A
            # 50-key request is not necessarily a 50-result response.
            for item in await self._item_pages(
                "users/0/items", {"itemKey": ",".join(batch), "itemType": "-attachment"}
            ):
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                key = _text(item.get("key") or data.get("key"))
                if key in batch:
                    found[key] = item
            for key in batch:
                if key in found:
                    continue
                try:
                    response = await self._request(f"users/0/items/{key}")
                except ZoteroLocalError:
                    continue  # snapshot races remain explicit per-item failures
                item = self._json(response)
                if isinstance(item, dict) and item.get("key") == key:
                    found[key] = item
        return found

    async def _item_pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        previous: list[Any] | None = None
        while True:
            response = await self._request(
                path, params={**(params or {}), "start": start, "limit": 100}
            )
            payload = self._json(response)
            if not isinstance(payload, list):
                raise ZoteroLocalError("ZOTERO_LOCAL_INVALID_RESPONSE")
            if not payload:
                break
            if payload == previous:
                raise ZoteroLocalError("ZOTERO_LOCAL_PAGINATION_STALLED")
            previous = payload
            rows.extend(item for item in payload if isinstance(item, dict))
            start += len(payload)
            total = self._header_int(response, "Total-Results")
            if (total is not None and start >= int(total)) or (
                total is None and len(payload) < 100
            ):
                break
        return rows

    async def children(self, item_key: str) -> list[dict[str, Any]]:
        return await self._item_pages(f"users/0/items/{item_key}/children")

    async def attachment_file_url(self, attachment_key: str) -> str:
        response = await self._request(
            f"users/0/items/{attachment_key}/file/view/url",
            allow_redirect_response=True,
        )
        location = response.headers.get("location")
        if location:
            return location
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            payload = self._json(response)
            if isinstance(payload, dict) and _text(payload.get("url")):
                return str(payload["url"])
        value = response.text.strip().strip('"')
        if not value:
            raise ZoteroLocalError("ZOTERO_ATTACHMENT_PATH_MISSING")
        return value


def collection_keys_for_binding(
    collections: Sequence[ZoteroCollection], root_key: str, *, include_descendants: bool = True
) -> list[str]:
    by_parent: dict[str | None, list[str]] = {}
    known = {collection.key for collection in collections}
    if root_key not in known:
        raise ZoteroLocalError("ZOTERO_COLLECTION_NOT_FOUND")
    for collection in collections:
        by_parent.setdefault(collection.parent_key, []).append(collection.key)
    result = [root_key]
    if not include_descendants:
        return result
    cursor = 0
    while cursor < len(result):
        result.extend(key for key in by_parent.get(result[cursor], []) if key not in result)
        cursor += 1
    return result


async def get_binding(session: AsyncSession, *, library_id: uuid.UUID) -> ZoteroLocalBinding | None:
    return await session.scalar(
        select(ZoteroLocalBinding).where(ZoteroLocalBinding.library_id == library_id)
    )


async def bind_library(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    collection_key: str,
    user_id: uuid.UUID,
    client: ZoteroLocalClient | None = None,
    commit: bool = True,
    verified: tuple[ZoteroProbe, Sequence[ZoteroCollection]] | None = None,
) -> ZoteroLocalBinding:
    require_desktop_profile()
    binding = await get_binding(session, library_id=library.id)
    if binding is not None:
        active = await session.scalar(
            select(func.count())
            .select_from(ZoteroSyncRun)
            .where(
                ZoteroSyncRun.binding_id == binding.id,
                ZoteroSyncRun.status.in_(("queued", "running")),
            )
        )
        if active:
            raise ZoteroLocalError("ZOTERO_SYNC_IN_PROGRESS")
    if verified is None:
        own_client = client is None
        local = client or ZoteroLocalClient()
        try:
            probe, collections = await asyncio.gather(local.probe(), local.collections())
        finally:
            if own_client:
                await local.aclose()
    else:
        probe, collections = verified
    collection = next((item for item in collections if item.key == collection_key), None)
    if collection is None:
        raise ZoteroLocalError("ZOTERO_COLLECTION_NOT_FOUND")
    if binding is None:
        binding = ZoteroLocalBinding(
            library_id=library.id,
            created_by=user_id,
            collection_key=collection.key,
            collection_name=collection.name,
            include_descendants=True,
        )
        session.add(binding)
    elif binding.collection_key != collection.key:
        binding.collection_key = collection.key
        binding.collection_name = collection.name
        binding.last_library_version = None
        binding.status = "idle"
        binding.last_error = None
    else:
        binding.collection_name = collection.name
    binding.zotero_instance_id = probe.instance_id
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(binding)
    return binding


async def import_collection_library(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    request_id: uuid.UUID,
    collection_key: str,
    name: str,
    statement: str | None,
    discipline: str | None,
    client: ZoteroLocalClient | None = None,
) -> tuple[ZoteroLocalBinding, ZoteroSyncRun]:
    """Create the library, binding, run and receipt in one transaction."""
    from app.services.libraries import create_library

    require_desktop_profile()
    fingerprint = hashlib.sha256(
        json.dumps(
            [collection_key, name.strip(), statement, discipline], ensure_ascii=False
        ).encode()
    ).hexdigest()
    lookup = select(ZoteroLibraryImport).where(
        ZoteroLibraryImport.user_id == user_id,
        ZoteroLibraryImport.request_id == request_id,
    )

    async def replay(receipt: ZoteroLibraryImport):
        if receipt.fingerprint != fingerprint:
            raise ZoteroLocalError("ZOTERO_IMPORT_REQUEST_CONFLICT")
        return (
            await session.get(ZoteroLocalBinding, receipt.binding_id),
            await session.get(ZoteroSyncRun, receipt.run_id),
        )

    receipt = await session.scalar(lookup)
    if receipt is not None:
        return await replay(receipt)
    if not name.strip():
        raise ZoteroLocalError("ZOTERO_LIBRARY_NAME_REQUIRED")
    local = client or ZoteroLocalClient()
    try:
        probe, collections = await asyncio.gather(local.probe(), local.collections())
    finally:
        if client is None:
            await local.aclose()
    if not any(item.key == collection_key for item in collections):
        raise ZoteroLocalError("ZOTERO_COLLECTION_NOT_FOUND")
    try:
        library = await create_library(
            session,
            name=name.strip(),
            statement=statement,
            discipline=discipline,
            created_by=user_id,
        )
        binding = await bind_library(
            session,
            library=library,
            collection_key=collection_key,
            user_id=user_id,
            client=client,
            commit=False,
            verified=(probe, collections),
        )
        run = ZoteroSyncRun(
            binding_id=binding.id,
            requested_by=user_id,
            full=True,
            status="queued",
            error_samples=[],
        )
        session.add(run)
        await session.flush()
        session.add(
            ZoteroLibraryImport(
                user_id=user_id,
                request_id=request_id,
                fingerprint=fingerprint,
                binding_id=binding.id,
                run_id=run.id,
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        receipt = await session.scalar(lookup)
        if receipt is None:
            raise
        return await replay(receipt)
    except Exception:
        await session.rollback()
        raise
    return binding, run


async def delete_binding(session: AsyncSession, *, binding: ZoteroLocalBinding) -> None:
    """Unlink Zotero without deleting any paper, PDF, summary, or membership."""
    await session.delete(binding)
    await session.commit()


async def prepare_sync_run(
    session: AsyncSession,
    *,
    binding: ZoteroLocalBinding,
    requested_by: uuid.UUID | None,
    full: bool,
) -> ZoteroSyncRun:
    require_desktop_profile()
    # Serialize the active-run check per binding on PostgreSQL.  The queue job id prevents
    # duplicate delivery; this lock also prevents two API replicas from persisting two runs.
    await session.execute(
        select(ZoteroLocalBinding.id).where(ZoteroLocalBinding.id == binding.id).with_for_update()
    )
    active = await session.scalar(
        select(ZoteroSyncRun)
        .where(
            ZoteroSyncRun.binding_id == binding.id,
            ZoteroSyncRun.status.in_(("queued", "running")),
        )
        .order_by(ZoteroSyncRun.created_at.desc())
        .limit(1)
    )
    if active is not None:
        return active
    binding_id = binding.id
    run = ZoteroSyncRun(
        binding_id=binding_id,
        requested_by=requested_by,
        full=full,
        status="queued",
        error_samples=[],
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        active = await session.scalar(
            select(ZoteroSyncRun)
            .where(
                ZoteroSyncRun.binding_id == binding_id,
                ZoteroSyncRun.status.in_(("queued", "running")),
            )
            .order_by(ZoteroSyncRun.created_at.desc())
            .limit(1)
        )
        if active is None:
            raise
        return active
    await session.refresh(run)
    return run


async def latest_sync_run(session: AsyncSession, *, binding_id: uuid.UUID) -> ZoteroSyncRun | None:
    return await session.scalar(
        select(ZoteroSyncRun)
        .where(ZoteroSyncRun.binding_id == binding_id)
        .order_by(ZoteroSyncRun.created_at.desc())
        .limit(1)
    )


async def due_binding_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Bindings due for the scheduler, including durable runs left in-flight by a restart."""
    active_binding_ids = select(ZoteroSyncRun.binding_id).where(
        ZoteroSyncRun.status.in_(("queued", "running"))
    )
    return list(
        (
            await session.execute(
                select(ZoteroLocalBinding.id).where(
                    or_(
                        ZoteroLocalBinding.id.in_(active_binding_ids),
                        (
                            (ZoteroLocalBinding.status != "syncing")
                            & (
                                (ZoteroLocalBinding.next_sync_at.is_(None))
                                | (ZoteroLocalBinding.next_sync_at <= utcnow())
                            )
                        ),
                    )
                )
            )
        )
        .scalars()
        .all()
    )


async def execute_sync_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    client: ZoteroLocalClient | None = None,
) -> ZoteroSyncRun:
    """Reconcile a queued run. Item errors are isolated; transport errors fail the run."""
    require_desktop_profile()
    run = await session.scalar(
        select(ZoteroSyncRun).where(ZoteroSyncRun.id == run_id).with_for_update()
    )
    if run is None:
        raise ZoteroLocalError("ZOTERO_SYNC_RUN_NOT_FOUND")
    binding = await session.get(ZoteroLocalBinding, run.binding_id)
    if binding is None:
        raise ZoteroLocalError("ZOTERO_BINDING_NOT_FOUND")
    if run.status in {"completed", "completed_with_errors", "failed", "running"}:
        return run

    run.status = "running"
    run.started_at = run.started_at or utcnow()
    run.finished_at = None
    run.total = 0
    run.processed = 0
    run.created = 0
    run.updated = 0
    run.existing = 0
    run.ignored = 0
    run.missing = 0
    run.failed = 0
    run.error_samples = []
    binding.status = "syncing"
    binding.last_error = None
    await session.commit()

    own_client = client is None
    local = client or ZoteroLocalClient()
    errors: list[dict[str, Any]] = list(run.error_samples or [])
    try:
        probe = await local.probe()
        if (
            binding.zotero_instance_id
            and probe.instance_id
            and binding.zotero_instance_id != probe.instance_id
        ):
            raise ZoteroLocalError("ZOTERO_INSTANCE_MISMATCH")
        if binding.zotero_instance_id is None and probe.instance_id:
            binding.zotero_instance_id = probe.instance_id
        collections = await local.collections()
        collection_keys = collection_keys_for_binding(
            collections,
            binding.collection_key,
            include_descendants=binding.include_descendants,
        )
        versions: dict[str, int] = {}
        library_version: int | None = None
        for collection_key in collection_keys:
            snapshot = await local.collection_item_versions(collection_key)
            for item_key, version in snapshot.versions.items():
                versions[item_key] = max(versions.get(item_key, 0), version)
            if snapshot.library_version is not None:
                library_version = max(library_version or 0, snapshot.library_version)

        run.total = len(versions)
        existing_links = {
            link.item_key: link
            for link in (
                (
                    await session.execute(
                        select(ZoteroItemLink).where(ZoteroItemLink.binding_id == binding.id)
                    )
                )
                .scalars()
                .all()
            )
        }
        changed_keys = [
            key
            for key, version in versions.items()
            if run.full
            or key not in existing_links
            or existing_links[key].item_version != version
            or existing_links[key].status in {"missing", "error"}
        ]
        # The query above autoflushes run/binding updates. Release SQLite's
        # writer lock before waiting on Zotero's paginated network responses.
        await session.commit()
        fetched = await local.items_by_keys(changed_keys)
        changed_key_set = set(changed_keys)
        title_index = await _title_index(session) if changed_keys else {}

        for item_key, item_version in versions.items():
            # Include unchanged and missing items, whose early continues used
            # to bypass the commit boundary for arbitrarily large collections.
            if run.processed and run.processed % 50 == 0:
                run.error_samples = list(errors)
                await session.commit()
            link = existing_links.get(item_key)
            if item_key not in changed_key_set:
                assert link is not None
                link.last_seen_run_id = run.id
                run.processed += 1
                continue
            item = fetched.get(item_key)
            if item is None:
                run.failed += 1
                run.processed += 1
                _append_error(errors, item_key, "ZOTERO_ITEM_NOT_RETURNED")
                if link is None:
                    link = ZoteroItemLink(
                        binding_id=binding.id,
                        item_key=item_key,
                        item_version=item_version,
                        status="error",
                    )
                    session.add(link)
                    existing_links[item_key] = link
                link.status = "error"
                link.last_error = "ZOTERO_ITEM_NOT_RETURNED"
                link.last_seen_run_id = run.id
                continue
            try:
                # A malformed record must not roll back already reconciled siblings.
                async with session.begin_nested():
                    outcome, conflicts = await _sync_item(
                        session,
                        binding=binding,
                        run=run,
                        link=link,
                        item=item,
                        item_key=item_key,
                        item_version=item_version,
                        title_index=title_index,
                    )
                setattr(run, outcome, getattr(run, outcome) + 1)
                for conflict in conflicts:
                    _append_error(errors, item_key, conflict, kind="identifier_conflict")
            except Exception as exc:  # noqa: BLE001 - isolate malformed individual records
                run.failed += 1
                _append_error(errors, item_key, f"{type(exc).__name__}: {exc}")
                failed_link = await session.scalar(
                    select(ZoteroItemLink).where(
                        ZoteroItemLink.binding_id == binding.id,
                        ZoteroItemLink.item_key == item_key,
                    )
                )
                if failed_link is None:
                    failed_link = ZoteroItemLink(
                        binding_id=binding.id,
                        item_key=item_key,
                        item_version=item_version,
                    )
                    session.add(failed_link)
                failed_link.status = "error"
                failed_link.last_error = str(exc)[:4000]
                failed_link.last_seen_run_id = run.id
            run.processed += 1

        # Items no longer in the recursively-bound collection are archived only when
        # this binding originally created the membership.
        links = (
            (
                await session.execute(
                    select(ZoteroItemLink).where(ZoteroItemLink.binding_id == binding.id)
                )
            )
            .scalars()
            .all()
        )
        for link in links:
            if link.item_key in versions or link.status == "missing":
                continue
            link.status = "missing"
            link.last_error = None
            run.missing += 1

        # Resolve references even for unchanged parent records: moving/replacing an
        # attachment does not reliably bump its parent's version in Local Zotero.
        # Bound concurrency/memory; do not read/hash/parse thousands of PDFs here.
        active_links = [link for link in links if link.status == "active"]
        run.error_samples = list(errors)
        await session.commit()
        for batch in _chunks(active_links, 8):
            await asyncio.gather(*(refresh_pdf_reference(local, link) for link in batch))
            await session.commit()

        # Archive at Paper granularity, after every disappeared link has been marked. Two Zotero
        # records may deduplicate onto one global Paper: only the first link records that this
        # binding created the membership. If that record disappears before its duplicate, the
        # historical flag must still take effect when the final record later disappears.
        sync_created_paper_ids = {
            link.paper_id
            for link in links
            if link.paper_id is not None and link.membership_created_by_sync
        }
        present_paper_ids = {link.paper_id for link in links if link.item_key in versions}
        for paper_id in sync_created_paper_ids:
            # Presence in the current version snapshot is authoritative even when fetching that
            # item failed and its link is temporarily in ``error`` state.
            if paper_id in present_paper_ids:
                continue
            membership = await get_membership(
                session, library_id=binding.library_id, paper_id=paper_id
            )
            if membership is not None and membership.status != "excluded":
                membership.status = "excluded"
                membership.trash_reason = "zotero_removed"

        now = utcnow()
        run.error_samples = list(errors)
        run.status = "completed_with_errors" if run.failed else "completed"
        run.finished_at = now
        binding.status = "idle"
        binding.last_synced_at = now
        binding.next_sync_at = now + SYNC_INTERVAL
        binding.last_library_version = library_version
        binding.last_error = None
        await session.commit()
        await session.refresh(run)
        return run
    except Exception as exc:
        await session.rollback()
        failed_run = await session.get(ZoteroSyncRun, run_id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.finished_at = utcnow()
            failed_run.error_samples = [
                *(failed_run.error_samples or []),
                {"kind": "sync", "error": f"{type(exc).__name__}: {exc}"[:1000]},
            ][-MAX_ERROR_SAMPLES:]
            failed_binding = await session.get(ZoteroLocalBinding, failed_run.binding_id)
            if failed_binding is not None:
                failed_binding.status = "error"
                failed_binding.last_error = f"{type(exc).__name__}: {exc}"[:4000]
                failed_binding.next_sync_at = utcnow() + SYNC_INTERVAL
            await session.commit()
        raise
    finally:
        if own_client:
            await local.aclose()


async def sync_binding(
    session: AsyncSession,
    *,
    binding: ZoteroLocalBinding,
    requested_by: uuid.UUID | None,
    full: bool = False,
    client: ZoteroLocalClient | None = None,
) -> ZoteroSyncRun:
    """Convenience entry point used by the CLI and direct service callers."""
    run = await prepare_sync_run(session, binding=binding, requested_by=requested_by, full=full)
    return await execute_sync_run(session, run_id=run.id, client=client)


async def recover_interrupted_sync_runs(session: AsyncSession) -> int:
    """Reset runs owned by a previous Desktop process before the local scheduler starts."""
    runs = list(
        (
            await session.execute(select(ZoteroSyncRun).where(ZoteroSyncRun.status == "running"))
        ).scalars()
    )
    binding_ids: set[uuid.UUID] = set()
    for run in runs:
        run.status = "queued"
        run.finished_at = None
        binding_ids.add(run.binding_id)
    if binding_ids:
        bindings = list(
            (
                await session.execute(
                    select(ZoteroLocalBinding).where(ZoteroLocalBinding.id.in_(binding_ids))
                )
            ).scalars()
        )
        for binding in bindings:
            binding.status = "idle"
            binding.last_error = None
    await session.commit()
    return len(runs)


async def _sync_item(
    session: AsyncSession,
    *,
    binding: ZoteroLocalBinding,
    run: ZoteroSyncRun,
    link: ZoteroItemLink | None,
    item: dict[str, Any],
    item_key: str,
    item_version: int,
    title_index: dict[str, set[uuid.UUID]] | None = None,
) -> tuple[str, list[str]]:
    had_link = link is not None
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    item_type = _text(data.get("itemType"))
    if item_type not in SUPPORTED_ITEM_TYPES:
        if link is None:
            link = ZoteroItemLink(binding_id=binding.id, item_key=item_key)
            session.add(link)
        link.item_version = item_version
        link.item_type = item_type
        link.metadata_snapshot = data
        link.status = "ignored"
        link.last_seen_run_id = run.id
        link.last_error = None
        await session.flush()
        return "ignored", []

    fields = _paper_fields(data)
    if not fields["title"]:
        raise ZoteroLocalError("ZOTERO_ITEM_TITLE_MISSING")

    paper = await session.get(Paper, link.paper_id) if link and link.paper_id else None
    created = False
    if paper is None:
        paper = await _find_paper_for_zotero(session, fields, title_index=title_index)
        if paper is None:
            paper = new_paper(
                source="zotero",
                dedup_key=pool_dedup_key(
                    arxiv_id=fields["arxiv_id"],
                    doi=fields["doi"],
                    title=fields["title"],
                    year=fields["year"],
                    authors=fields["authors"],
                ),
                **fields,
            )
            session.add(paper)
            await session.flush()
            created = True

    old_title = _normalized_identity_text(paper.title)
    conflicts = _merge_paper_fields(paper, fields, replace=paper.source == "zotero")
    membership, membership_created = await ensure_membership(
        session, library_id=binding.library_id, paper_id=paper.id, status="included"
    )
    if membership.status == "excluded" and membership.trash_reason == "zotero_removed":
        membership.status = "included"
        membership.trash_reason = None

    if link is None:
        link = ZoteroItemLink(binding_id=binding.id, item_key=item_key)
        session.add(link)
        link.membership_created_by_sync = membership_created
    elif membership_created:
        link.membership_created_by_sync = True
    link.paper_id = paper.id
    link.item_version = item_version
    link.item_type = item_type
    link.metadata_snapshot = data
    link.status = "active"
    link.last_seen_run_id = run.id
    link.last_error = None
    await session.flush()

    if title_index is not None:
        title_index.get(old_title, set()).discard(paper.id)
        title_index.setdefault(_normalized_identity_text(paper.title), set()).add(paper.id)

    if created:
        return "created", conflicts
    return ("updated" if had_link else "existing"), conflicts


def _normalized_identity_text(value: str) -> str:
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


async def _title_index(session: AsyncSession) -> dict[str, set[uuid.UUID]]:
    """One lightweight, Unicode-safe index per run, independent of the primary DOI key."""
    index: dict[str, set[uuid.UUID]] = {}
    for paper_id, title in await session.execute(select(Paper.id, Paper.title)):
        index.setdefault(_normalized_identity_text(title), set()).add(paper_id)
    return index


def _first_author_identity(authors: list[Any] | None) -> str:
    first = (authors or [""])[0]
    name = first.get("name", "") if isinstance(first, dict) else str(first)
    return _normalized_identity_text(name if isinstance(name, str) else "")


async def _find_paper_for_zotero(
    session: AsyncSession,
    fields: dict[str, Any],
    *,
    title_index: dict[str, set[uuid.UUID]] | None = None,
) -> Paper | None:
    # Product contract is DOI -> arXiv -> normalized title, even though the global pool's
    # creation key remains its historical arXiv -> DOI -> title convention.
    if fields["doi"]:
        paper = await session.scalar(
            select(Paper).where(func.lower(Paper.doi) == fields["doi"].lower()).limit(1)
        )
        if paper is not None:
            return paper
    if fields["arxiv_id"]:
        paper = await session.scalar(
            select(Paper).where(func.lower(Paper.arxiv_id) == fields["arxiv_id"].lower()).limit(1)
        )
        if paper is not None:
            return paper
    index = title_index if title_index is not None else await _title_index(session)
    key = _normalized_identity_text(fields["title"])
    matches: list[Paper] = []
    for paper_id in index.get(key, ()) if key else ():
        paper = await session.get(Paper, paper_id)
        if paper is None:
            continue
        if any(
            fields[field]
            and getattr(paper, field)
            and str(fields[field]).casefold() != str(getattr(paper, field)).casefold()
            for field in ("doi", "arxiv_id", "year")
        ):
            continue
        author = _first_author_identity(paper.authors)
        incoming = _first_author_identity(fields["authors"])
        if author and incoming and author != incoming:
            continue
        matches.append(paper)
    # A title collision is not enough evidence to merge two different papers.
    return matches[0] if len(matches) == 1 else None


def _merge_paper_fields(paper: Paper, incoming: dict[str, Any], *, replace: bool) -> list[str]:
    conflicts: list[str] = []
    for field in ("doi", "arxiv_id"):
        old = _text(getattr(paper, field))
        new = _text(incoming.get(field))
        if old and new and old.lower() != new.lower():
            conflicts.append(f"{field}:{old}!={new}")
        elif not old and new:
            setattr(paper, field, new)
    for field in ("title", "authors", "abstract", "year", "venue", "url"):
        value = incoming.get(field)
        if value in (None, "", []):
            continue
        if replace or getattr(paper, field) in (None, "", []):
            setattr(paper, field, value)
    return conflicts


def _paper_fields(data: dict[str, Any]) -> dict[str, Any]:
    creators = data.get("creators") if isinstance(data.get("creators"), list) else []
    authors: list[dict[str, str]] = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        if creator.get("creatorType") not in {None, "author", "contributor"}:
            continue
        name = _text(creator.get("name"))
        if not name:
            name = " ".join(
                part
                for part in (
                    _text(creator.get("firstName")),
                    _text(creator.get("lastName")),
                )
                if part
            )
        if name:
            authors.append({"name": name})
    date = _text(data.get("date"))
    year_match = _YEAR_RE.search(date or "")
    venue = _text(
        data.get("publicationTitle")
        or data.get("conferenceName")
        or data.get("university")
        or data.get("publisher")
    )
    doi = _normalize_doi(_text(data.get("DOI")))
    arxiv_id = _extract_arxiv(data)
    return {
        "title": _text(data.get("title")) or "",
        "authors": authors,
        "abstract": _text(data.get("abstractNote")),
        "year": int(year_match.group()) if year_match else None,
        "venue": venue,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "url": _text(data.get("url")),
    }


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", value.strip())
    return normalized or None


def _extract_arxiv(data: dict[str, Any]) -> str | None:
    haystack = "\n".join(
        str(data.get(field) or "") for field in ("archiveID", "extra", "url", "repository")
    )
    match = _ARXIV_RE.search(haystack)
    return match.group(1).lower() if match else None


def _append_error(
    errors: list[dict[str, Any]], item_key: str, error: str, *, kind: str = "item"
) -> None:
    if len(errors) < MAX_ERROR_SAMPLES:
        errors.append({"kind": kind, "item_key": item_key, "error": error[:1000]})


async def materialize_pdf(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    paper: Paper,
    user: User,
    client: ZoteroLocalClient | None = None,
    attachment_ref: ZoteroAttachmentRef | None = None,
) -> MaterializedZoteroAsset:
    """Register the original Zotero file and its digest without copying PDF bytes."""
    require_desktop_profile()
    binding = await get_binding(session, library_id=library.id)
    if binding is None:
        raise ZoteroLocalError("ZOTERO_BINDING_NOT_FOUND")
    link = await session.scalar(
        select(ZoteroItemLink).where(
            ZoteroItemLink.binding_id == binding.id,
            ZoteroItemLink.paper_id == paper.id,
            ZoteroItemLink.status == "active",
        )
    )
    if link is None:
        raise ZoteroLocalError("ZOTERO_ITEM_LINK_NOT_FOUND")

    own_client = client is None
    local = client or ZoteroLocalClient()
    try:
        resolved = attachment_ref or await _pdf_attachment_ref(local, link.item_key)
        file_url = await local.attachment_file_url(resolved.key)
        path = _path_from_file_url(file_url)
        content = await asyncio.to_thread(_read_pdf_bytes, path)
        locator = f"zotero://{binding.zotero_library_id}/{resolved.key}"
        try:
            asset = await create_or_reuse_asset(
                session,
                paper=paper,
                library=library,
                content=content,
                user=user,
                source="zotero",
                source_locator=locator,
                identity_key=paper.dedup_key,
                identity_status="verified" if paper.dedup_key else "unverified",
                sharing_scope="private",
                external_path=path,
            )
        except AssetError:
            raise ZoteroLocalError("ZOTERO_ATTACHMENT_INVALID") from None
        await session.execute(
            update(PaperAsset)
            .where(
                PaperAsset.paper_id == paper.id,
                PaperAsset.source == "zotero",
                PaperAsset.id != asset.id,
            )
            .values(is_preferred=False)
        )
        link.attachment_key = resolved.key
        link.attachment_version = resolved.version
        link.local_pdf_path = str(path)
        link.pdf_status = "linked"
        link.pdf_error = None
        await session.commit()
        await session.refresh(asset)
        return MaterializedZoteroAsset(
            asset=asset,
            attachment_key=resolved.key,
            attachment_version=resolved.version,
            byte_size=len(content),
        )
    finally:
        if own_client:
            await local.aclose()


async def materialize_paper_pdf(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
    library_id: uuid.UUID,
    client: ZoteroLocalClient | None = None,
) -> PaperContentVersion | None:
    """Ensure a Zotero-backed paper has a queued immutable content version.

    This is the service-level seam used by summary generation.  It intentionally does
    not enqueue or execute parsing: callers can use the existing
    ``parse_paper_content_task`` and expose its progress in their own workflow.
    """
    from app.services.paper_content import (
        create_content_version,
        latest_readable_content_version,
    )

    require_desktop_profile()
    current = await latest_readable_content_version(
        session,
        paper_id=paper_id,
        library_id=library_id,
        require_process=True,
    )
    if current is not None and current.status in {
        "queued",
        "mineru_uploading",
        "parsing",
        "fallback_parsing",
    }:
        return current
    library = await session.get(DirectionLibrary, library_id)
    paper = await session.get(Paper, paper_id)
    user = await session.get(User, user_id)
    if library is None or paper is None or user is None:
        return None
    membership = await get_membership(session, library_id=library.id, paper_id=paper.id)
    if membership is None:
        return None
    current_asset = await session.get(PaperAsset, current.asset_id) if current is not None else None
    if (
        current is not None
        and current.status in {"ready", "ready_fallback", "vector_ready"}
        and current_asset is not None
        and current_asset.source != "zotero"
    ):
        return current

    binding = await get_binding(session, library_id=library.id)
    if binding is None:
        return None
    link = await session.scalar(
        select(ZoteroItemLink).where(
            ZoteroItemLink.binding_id == binding.id,
            ZoteroItemLink.paper_id == paper.id,
            ZoteroItemLink.status == "active",
        )
    )
    if link is None:
        return None
    own_client = client is None
    local = client or ZoteroLocalClient()
    try:
        attachment_ref = await _pdf_attachment_ref(local, link.item_key)
        # Hash on demand, not just key/version: in-place PDF edits may leave the
        # Zotero version unchanged. Identical bytes still reuse the existing version.
        materialized = await materialize_pdf(
            session,
            library=library,
            paper=paper,
            user=user,
            client=local,
            attachment_ref=attachment_ref,
        )
    except ZoteroLocalError as exc:
        if exc.code in {
            "ZOTERO_BINDING_NOT_FOUND",
            "ZOTERO_ITEM_LINK_NOT_FOUND",
            "ZOTERO_PDF_ATTACHMENT_NOT_FOUND",
        }:
            return None
        raise
    finally:
        if own_client:
            await local.aclose()
    existing = await session.scalar(
        select(PaperContentVersion)
        .where(PaperContentVersion.asset_id == materialized.asset.id)
        .order_by(PaperContentVersion.version_no.desc())
        .limit(1)
    )
    if existing is not None and existing.status != "failed":
        return existing
    version = await create_content_version(session, asset=materialized.asset)
    await session.commit()
    await session.refresh(version)
    return version


def _select_pdf_attachment(items: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in items:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if data.get("itemType") != "attachment":
            continue
        content_type = str(data.get("contentType") or "").lower()
        filename = str(data.get("filename") or data.get("title") or "").lower()
        if content_type == "application/pdf" or filename.endswith(".pdf"):
            candidates.append(item)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            bool((item.get("data") or {}).get("path")),
            _int(item.get("version") or (item.get("data") or {}).get("version")),
            str(item.get("key") or ""),
        ),
    )


async def _pdf_attachment_ref(client: ZoteroLocalClient, item_key: str) -> ZoteroAttachmentRef:
    attachment = _select_pdf_attachment(await client.children(item_key))
    if attachment is None:
        raise ZoteroLocalError("ZOTERO_PDF_ATTACHMENT_NOT_FOUND")
    data = attachment.get("data") if isinstance(attachment.get("data"), dict) else {}
    key = _text(attachment.get("key") or data.get("key"))
    if not key:
        raise ZoteroLocalError("ZOTERO_PDF_ATTACHMENT_NOT_FOUND")
    return ZoteroAttachmentRef(
        key=key,
        version=_int(attachment.get("version") or data.get("version")),
    )


def _path_from_file_url(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ZoteroLocalError("ZOTERO_ATTACHMENT_URL_INVALID")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ZoteroLocalError("ZOTERO_ATTACHMENT_URL_INVALID")
    return path


def _check_pdf_path(path: Path, *, check_header: bool = True) -> Path:
    """Cheap import-time check; never copy or parse the PDF."""
    try:
        if path.is_symlink() or not path.is_file():
            raise ZoteroLocalError("ZOTERO_ATTACHMENT_FILE_MISSING")
        if not 0 < path.stat().st_size <= MAX_PDF_BYTES:
            raise ZoteroLocalError("ZOTERO_ATTACHMENT_SIZE_INVALID")
        if check_header:
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise ZoteroLocalError("ZOTERO_ATTACHMENT_NOT_PDF")
        return path.resolve(strict=True)
    except OSError:
        raise ZoteroLocalError("ZOTERO_ATTACHMENT_FILE_UNREADABLE") from None


async def refresh_pdf_reference(local: ZoteroLocalClient, link: ZoteroItemLink) -> None:
    try:
        ref = await _pdf_attachment_ref(local, link.item_key)
        link.attachment_key, link.attachment_version = ref.key, ref.version
        path = _path_from_file_url(await local.attachment_file_url(ref.key))
        # Retain the trusted API path even when an offline/cloud file is unavailable.
        link.local_pdf_path = str(path)
        await asyncio.to_thread(_check_pdf_path, path, check_header=False)
        link.pdf_status, link.pdf_error = "linked", None
    except ZoteroLocalError as exc:
        link.pdf_status = (
            "missing" if exc.code == "ZOTERO_PDF_ATTACHMENT_NOT_FOUND" else "unavailable"
        )
        link.pdf_error = exc.code
        if link.pdf_status == "missing":
            link.attachment_key = None
            link.attachment_version = None
            link.local_pdf_path = None


async def original_pdf_path(
    session: AsyncSession, *, library_id: uuid.UUID, paper_id: uuid.UUID
) -> Path:
    """Return an original file after caller authorization; never expose its path on the wire."""
    require_desktop_profile()
    link = await session.scalar(
        select(ZoteroItemLink)
        .join(ZoteroLocalBinding, ZoteroLocalBinding.id == ZoteroItemLink.binding_id)
        .where(
            ZoteroLocalBinding.library_id == library_id,
            ZoteroItemLink.paper_id == paper_id,
            ZoteroItemLink.status == "active",
        )
    )
    if (
        link is None
        or await get_membership(session, library_id=library_id, paper_id=paper_id) is None
    ):
        raise ZoteroLocalError("ZOTERO_ITEM_LINK_NOT_FOUND")
    async with ZoteroLocalClient() as local:
        await refresh_pdf_reference(local, link)
    await session.commit()
    if link.pdf_error not in {None, "ZOTERO_LOCAL_UNAVAILABLE", "ZOTERO_LOCAL_TIMEOUT"}:
        raise ZoteroLocalError(link.pdf_error)
    if not link.local_pdf_path:
        raise ZoteroLocalError("ZOTERO_PDF_ATTACHMENT_NOT_FOUND")
    return await asyncio.to_thread(_check_pdf_path, Path(link.local_pdf_path))


def _read_pdf_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise ZoteroLocalError("ZOTERO_ATTACHMENT_FILE_MISSING")
        size = path.stat().st_size
        if size <= 0 or size > MAX_PDF_BYTES:
            raise ZoteroLocalError("ZOTERO_ATTACHMENT_SIZE_INVALID")
        with path.open("rb") as handle:
            content = handle.read(MAX_PDF_BYTES + 1)
    except ZoteroLocalError:
        raise
    except OSError:
        # Do not put the absolute local attachment path into an API error or log record.
        raise ZoteroLocalError("ZOTERO_ATTACHMENT_FILE_UNREADABLE") from None
    if len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
        raise ZoteroLocalError("ZOTERO_ATTACHMENT_NOT_PDF")
    return content


def _text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
