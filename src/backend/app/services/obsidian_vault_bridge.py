"""Desktop-only bidirectional bridge for an existing Obsidian vault.

Only the configured managed subfolder (default ``<vault>/Polaris``) is managed.
Database content and Markdown bodies share a persisted
merge base; generated YAML frontmatter is deliberately excluded from the merge.  The module
does not import FastAPI and can therefore be used by HTTP, CLI, startup reconciliation, and a
filesystem watcher without duplicating business rules.

The historical :mod:`app.services.obsidian_vault_sync` module imports an auto-idea vault in a
different layout and remains intentionally separate.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import yaml
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.obsidian_vault import (
    ObsidianVaultConnection,
    VaultConflict,
    VaultFileState,
    VaultLibraryBinding,
)
from app.models.paper import Paper, PaperNote, PaperWiki, PaperWikiRevision
from app.models.user import User
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
from app.services import libraries as libraries_service
from app.services import paper_wiki

logger = logging.getLogger(__name__)

MANAGED_DIRECTORY = "Polaris"
MANIFEST_FILENAME = ".polaris-manifest.json"
BRIDGE_VERSION = 1
DELETION_RETENTION_DAYS = 30

_NOTE_BLOCK_RE = re.compile(
    r"<!--\s*polaris-note:([0-9a-fA-F-]{36}):start\s*-->\s*\n"
    r"(.*?)\n<!--\s*polaris-note:\1:end\s*-->",
    re.DOTALL,
)
_NEW_NOTE_RE = re.compile(
    r"<!--\s*polaris-new:start\s*-->\s*\n(.*?)\n<!--\s*polaris-new:end\s*-->",
    re.DOTALL,
)


class VaultBridgeError(ValueError):
    """A stable, user-safe validation error (never includes a local absolute path)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(slots=True, frozen=True)
class MarkdownDocument:
    metadata: dict[str, Any]
    body: str


@dataclass(slots=True, frozen=True)
class MergeOutcome:
    status: Literal["unchanged", "polaris", "vault", "merged", "conflict"]
    content: str | None


@dataclass(slots=True)
class VaultSyncStats:
    files_written: int = 0
    files_imported: int = 0
    files_unchanged: int = 0
    files_deleted: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_written": self.files_written,
            "files_imported": self.files_imported,
            "files_unchanged": self.files_unchanged,
            "files_deleted": self.files_deleted,
            "conflicts": self.conflicts,
            "errors": self.errors[:20],
        }


@dataclass(slots=True, frozen=True)
class SummarySnapshot:
    content: str
    revision_id: uuid.UUID | None
    content_version_id: uuid.UUID | None
    model: str | None
    source_level: str


@dataclass(slots=True, frozen=True)
class ProjectedEntity:
    entity_type: Literal["summary", "notes", "library_index"]
    entity_id: uuid.UUID
    library: DirectionLibrary
    paper: Paper | None
    relative_path: str
    body: str
    metadata: dict[str, Any]
    editable: bool = True
    domain_deleted: bool = False


class VaultDomainAdapter(Protocol):
    """Domain mutation seam used by the bridge.

    The default implementation uses the existing wiki/note services.  Versioned-summary or
    soft-delete services can implement this protocol without teaching filesystem code about
    their tables.
    """

    async def apply_summary(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None: ...

    async def soft_delete_summary(
        self, session: AsyncSession, *, paper_id: uuid.UUID, user: User
    ) -> bool: ...

    async def restore_summary(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None: ...

    async def apply_notes(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None: ...

    async def soft_delete_notes(
        self, session: AsyncSession, *, paper_id: uuid.UUID, user: User
    ) -> bool: ...

    async def restore_notes(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None: ...


class DefaultVaultDomainAdapter:
    """Map editable Vault bodies onto versioned summaries and private note tombstones."""

    async def apply_summary(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None:
        current_wiki = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == paper.id)
        )
        current_revision = (
            await session.get(PaperWikiRevision, current_wiki.current_revision_id)
            if current_wiki is not None and current_wiki.current_revision_id is not None
            else None
        )
        from app.services import paper_summaries

        wiki = await paper_wiki.upsert_wiki(
            session,
            paper=paper,
            content=content,
            model="obsidian",
            compiled_by=user.id,
            source_level="obsidian",
            content_version_id=(
                current_revision.content_version_id if current_revision is not None else None
            ),
            source_fingerprint=(
                current_revision.source_fingerprint if current_revision is not None else None
            ),
            evidence_manifest=(
                paper_summaries.extract_evidence_manifest(content)
                or (
                    current_revision.evidence_manifest
                    if current_revision is not None
                    else None
                )
            ),
            source_library_id=(
                current_revision.source_library_id if current_revision is not None else None
            ),
            source_project_id=(
                current_revision.source_project_id if current_revision is not None else None
            ),
        )
        wiki.deleted_at = None
        await session.flush()

    async def soft_delete_summary(
        self, session: AsyncSession, *, paper_id: uuid.UUID, user: User
    ) -> bool:
        paper = await session.get(Paper, paper_id)
        if paper is None:
            return True
        from app.services import paper_summaries

        try:
            await paper_summaries.soft_delete_summary(session, paper=paper)
        except paper_summaries.SummaryNotFoundError:
            return True
        return True

    async def restore_summary(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None:
        from app.services.paper_summaries import SummaryRestoreExpiredError, restore_summary

        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        if wiki is not None and wiki.deleted_at is not None:
            try:
                await restore_summary(session, paper=paper)
            except SummaryRestoreExpiredError as exc:
                raise VaultBridgeError("OBSIDIAN_RESTORE_EXPIRED") from exc
        await self.apply_summary(session, paper=paper, user=user, content=content)

    async def apply_notes(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None:
        parsed, new_note = parse_notes_body(content)
        retention_cutoff = utcnow() - timedelta(days=DELETION_RETENTION_DAYS)
        rows = list(
            (
                await session.execute(
                    select(PaperNote).where(
                        PaperNote.paper_id == paper.id,
                        PaperNote.author_id == user.id,
                        or_(
                            PaperNote.deleted_at.is_(None),
                            PaperNote.deleted_at >= retention_cutoff,
                        ),
                    )
                )
            ).scalars()
        )
        existing = {note.id: note for note in rows}
        if parsed.keys() - existing.keys():
            # An expired/foreign marker cannot be silently discarded by canonical rendering.
            # Keep the file intact so its author can recover the text explicitly as a new note.
            raise VaultBridgeError("OBSIDIAN_NOTE_ID_UNKNOWN_OR_EXPIRED")
        for note_id, note_content in parsed.items():
            note = existing.get(note_id)
            if note is None:
                continue
            if note.content != note_content:
                note.content = note_content
            # Re-adding a valid managed file (or a previously removed stable block) within the
            # retention period restores the same private note identity.
            note.deleted_at = None
        if new_note:
            session.add(
                PaperNote(paper_id=paper.id, author_id=user.id, content=new_note)
            )
        # Missing blocks are deletion requests.  Never hard-delete as a compatibility fallback.
        now = utcnow()
        for note_id in existing.keys() - parsed.keys():
            note = existing[note_id]
            if note.deleted_at is None:
                note.deleted_at = now
        await session.flush()

    async def soft_delete_notes(
        self, session: AsyncSession, *, paper_id: uuid.UUID, user: User
    ) -> bool:
        notes = list(
            (
                await session.execute(
                    select(PaperNote).where(
                        PaperNote.paper_id == paper_id,
                        PaperNote.author_id == user.id,
                        PaperNote.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        if not notes:
            return True
        now = utcnow()
        for note in notes:
            note.deleted_at = now
        await session.flush()
        return True

    async def restore_notes(
        self, session: AsyncSession, *, paper: Paper, user: User, content: str
    ) -> None:
        parsed, _new_note = parse_notes_body(content)
        if parsed:
            retention_cutoff = utcnow() - timedelta(days=DELETION_RETENTION_DAYS)
            rows = list(
                (
                    await session.execute(
                        select(PaperNote).where(
                            PaperNote.paper_id == paper.id,
                            PaperNote.author_id == user.id,
                            or_(
                                PaperNote.deleted_at.is_(None),
                                PaperNote.deleted_at >= retention_cutoff,
                            ),
                        )
                    )
                ).scalars()
            )
            for note in rows:
                if note.id in parsed:
                    note.deleted_at = None
        await self.apply_notes(session, paper=paper, user=user, content=content)


DEFAULT_DOMAIN_ADAPTER = DefaultVaultDomainAdapter()
_SYNC_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}
_ACTIVE_RECONCILIATIONS = 0


def active_vault_syncs() -> int:
    return _ACTIVE_RECONCILIATIONS + sum(lock.locked() for lock in _SYNC_LOCKS.values())


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_markdown(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def parse_markdown_document(raw: str) -> MarkdownDocument:
    """Parse strict YAML frontmatter and a normalized Markdown body."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise VaultBridgeError("OBSIDIAN_FRONTMATTER_REQUIRED")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise VaultBridgeError("OBSIDIAN_FRONTMATTER_INVALID")
    try:
        loaded = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError as exc:
        raise VaultBridgeError("OBSIDIAN_FRONTMATTER_INVALID") from exc
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise VaultBridgeError("OBSIDIAN_FRONTMATTER_INVALID")
    body = text[marker + 5 :]
    if body.startswith("\n"):
        body = body[1:]
    return MarkdownDocument(metadata=loaded, body=normalize_markdown(body))


def render_markdown_document(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{normalize_markdown(body)}"


def _safe_slug(value: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[/\\\x00-\x1f\x7f]+", "-", text)
    text = re.sub(r"[^\w\- ]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "-", text).strip("-.")
    return (text[:80].rstrip("-.") or fallback).lower()


def library_directory(library: DirectionLibrary) -> str:
    return f"{_safe_slug(library.name, 'library')}-{library.id.hex[:8]}"


def paper_filename(paper: Paper) -> str:
    return f"{_safe_slug(paper.title, 'paper')}-{paper.id.hex[:8]}.md"


def validate_vault_root(raw_path: str | Path) -> Path:
    raw = Path(raw_path).expanduser()
    if not raw.is_absolute():
        raise VaultBridgeError("OBSIDIAN_VAULT_PATH_MUST_BE_ABSOLUTE")
    try:
        root = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise VaultBridgeError("OBSIDIAN_VAULT_NOT_FOUND") from exc
    if not root.is_dir():
        raise VaultBridgeError("OBSIDIAN_VAULT_NOT_FOUND")
    marker = root / ".obsidian"
    if not marker.is_dir() or marker.is_symlink():
        raise VaultBridgeError("OBSIDIAN_VAULT_MARKER_MISSING")
    return root


def validate_managed_directory(value: str) -> str:
    """One visible folder directly inside the vault, never an arbitrary path."""
    if (
        not value or len(value) > 128 or value != value.strip() or value.startswith(".")
        or value.endswith(".") or re.search(r'[/\\:<>"|?*\x00-\x1f\x7f]', value)
    ):
        raise VaultBridgeError("OBSIDIAN_MANAGED_DIRECTORY_INVALID")
    return value


def managed_root(
    vault_root: Path, managed_directory: str = MANAGED_DIRECTORY, *, create: bool = False
) -> Path:
    root = vault_root.resolve(strict=True)
    target = root / validate_managed_directory(managed_directory)
    if target.is_symlink():
        raise VaultBridgeError("OBSIDIAN_MANAGED_PATH_SYMLINK")
    if target.exists():
        if not target.is_dir():
            raise VaultBridgeError("OBSIDIAN_MANAGED_PATH_INVALID")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise VaultBridgeError("OBSIDIAN_PATH_OUTSIDE_MANAGED_ROOT")
        return resolved
    if create:
        target.mkdir(mode=0o700)
    return target


def safe_managed_path(root: Path, relative_path: str, *, create_parent: bool = False) -> Path:
    """Resolve a POSIX relative path below the managed root without following symlinks."""
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise VaultBridgeError("OBSIDIAN_PATH_OUTSIDE_MANAGED_ROOT")
    base = root.resolve(strict=True)
    cursor = base
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise VaultBridgeError("OBSIDIAN_MANAGED_PATH_SYMLINK")
        if cursor.exists() and not cursor.is_dir():
            raise VaultBridgeError("OBSIDIAN_MANAGED_PATH_INVALID")
        if create_parent and not cursor.exists():
            cursor.mkdir(mode=0o700)
    target = cursor / relative.parts[-1]
    if target.is_symlink():
        raise VaultBridgeError("OBSIDIAN_MANAGED_PATH_SYMLINK")
    if create_parent:
        parent = target.parent.resolve(strict=True)
        if not parent.is_relative_to(base):
            raise VaultBridgeError("OBSIDIAN_PATH_OUTSIDE_MANAGED_ROOT")
    if target.exists() and not target.resolve(strict=True).is_relative_to(base):
        raise VaultBridgeError("OBSIDIAN_PATH_OUTSIDE_MANAGED_ROOT")
    return target


def atomic_write_text(root: Path, relative_path: str, content: str) -> Path:
    target = safe_managed_path(root, relative_path, create_parent=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except Exception:
        with suppress(OSError):
            os.unlink(temporary)
        raise
    return target


def _edit_script(base: list[str], value: list[str]) -> list[tuple[int, int, list[str]]]:
    matcher = SequenceMatcher(a=base, b=value, autojunk=False)
    return [
        (i1, i2, value[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _edits_overlap(
    left: tuple[int, int, list[str]], right: tuple[int, int, list[str]]
) -> bool:
    left_start, left_end, _ = left
    right_start, right_end, _ = right
    if left_start == left_end and right_start == right_end:
        return left_start == right_start
    if left_start == left_end:
        return right_start <= left_start <= right_end
    if right_start == right_end:
        return left_start <= right_start <= left_end
    return max(left_start, right_start) < min(left_end, right_end)


def three_way_merge(base: str, polaris: str, vault: str) -> MergeOutcome:
    """Line-oriented three-way merge with conservative overlap detection."""
    base = normalize_markdown(base)
    polaris = normalize_markdown(polaris)
    vault = normalize_markdown(vault)
    if polaris == vault:
        return MergeOutcome("unchanged", polaris)
    if polaris == base:
        return MergeOutcome("vault", vault)
    if vault == base:
        return MergeOutcome("polaris", polaris)

    base_lines = base.splitlines(keepends=True)
    polaris_edits = _edit_script(base_lines, polaris.splitlines(keepends=True))
    vault_edits = _edit_script(base_lines, vault.splitlines(keepends=True))
    combined = list(polaris_edits)
    for vault_edit in vault_edits:
        duplicate = False
        for polaris_edit in polaris_edits:
            if not _edits_overlap(polaris_edit, vault_edit):
                continue
            if polaris_edit == vault_edit:
                duplicate = True
                break
            return MergeOutcome("conflict", None)
        if not duplicate:
            combined.append(vault_edit)
    merged = list(base_lines)
    for start, end, replacement in sorted(
        combined, key=lambda item: (item[0], item[1]), reverse=True
    ):
        merged[start:end] = replacement
    return MergeOutcome("merged", normalize_markdown("".join(merged)))


def render_notes_body(paper: Paper, notes: list[PaperNote]) -> str:
    lines = [f"# Notes — {paper.title}", ""]
    for note in sorted(notes, key=lambda row: (row.created_at, str(row.id))):
        if note.deleted_at is not None:
            continue
        lines.extend(
            [
                f"<!-- polaris-note:{note.id}:start -->",
                note.content.rstrip(),
                f"<!-- polaris-note:{note.id}:end -->",
                "",
            ]
        )
    lines.extend(
        [
            "<!-- polaris-new:start -->",
            "",
            "<!-- Add one new note here. Polaris clears this block after importing it. -->",
            "",
            "<!-- polaris-new:end -->",
            "",
        ]
    )
    return normalize_markdown("\n".join(lines))


def parse_notes_body(content: str) -> tuple[dict[uuid.UUID, str], str | None]:
    body = normalize_markdown(content)
    parsed: dict[uuid.UUID, str] = {}
    for raw_id, raw_content in _NOTE_BLOCK_RE.findall(body):
        note_content = raw_content.strip()
        if note_content:
            parsed[uuid.UUID(raw_id)] = note_content
    new_match = _NEW_NOTE_RE.search(body)
    new_note: str | None = None
    if new_match:
        candidate = re.sub(r"<!--.*?-->", "", new_match.group(1), flags=re.DOTALL).strip()
        if candidate:
            new_note = candidate
    return parsed, new_note


async def _summary_snapshot(session: AsyncSession, wiki: PaperWiki) -> SummarySnapshot:
    revision = (
        await session.get(PaperWikiRevision, wiki.current_revision_id)
        if wiki.current_revision_id is not None
        else None
    )
    return SummarySnapshot(
        content=normalize_markdown(wiki.content),
        revision_id=getattr(wiki, "current_revision_id", None),
        content_version_id=getattr(revision, "content_version_id", None),
        model=getattr(revision, "model", None) or wiki.model,
        source_level=getattr(revision, "source_level", None) or "legacy",
    )


def _authors(paper: Paper) -> list[str]:
    result: list[str] = []
    for author in paper.authors or []:
        if isinstance(author, dict) and author.get("name"):
            result.append(str(author["name"]))
        elif isinstance(author, str):
            result.append(author)
    return result


def _entity_metadata(entity: ProjectedEntity, *, baseline_hash: str) -> dict[str, Any]:
    metadata = dict(entity.metadata)
    metadata.update(
        {
            "polaris_type": entity.entity_type,
            "polaris_library_id": str(entity.library.id),
            "polaris_entity_id": str(entity.entity_id),
            "polaris_baseline_hash": baseline_hash,
        }
    )
    return metadata


async def _refresh_entity_metadata(
    session: AsyncSession, entity: ProjectedEntity
) -> None:
    """Refresh generated/read-only frontmatter after a Vault edit creates a new revision."""
    if entity.paper is None:
        return
    metadata: dict[str, Any] = {
        "title": entity.paper.title,
        "authors": _authors(entity.paper),
    }
    zotero_key = await session.scalar(
        select(ZoteroItemLink.item_key)
        .join(ZoteroLocalBinding, ZoteroLocalBinding.id == ZoteroItemLink.binding_id)
        .where(
            ZoteroLocalBinding.library_id == entity.library.id,
            ZoteroItemLink.paper_id == entity.paper.id,
            ZoteroItemLink.status == "active",
        )
        .order_by(ZoteroItemLink.item_key)
        .limit(1)
    )
    metadata["zotero_key"] = zotero_key
    if entity.entity_type == "summary":
        wiki = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == entity.paper.id)
        )
        if wiki is not None:
            snapshot = await _summary_snapshot(session, wiki)
            metadata.update(
                {
                    "polaris_revision_id": (
                        str(snapshot.revision_id) if snapshot.revision_id else None
                    ),
                    "polaris_content_version_id": (
                        str(snapshot.content_version_id)
                        if snapshot.content_version_id
                        else None
                    ),
                    "polaris_model": snapshot.model,
                    "polaris_source_level": snapshot.source_level,
                }
            )
    entity.metadata.clear()
    entity.metadata.update(metadata)


def _document_matches_entity(document: MarkdownDocument, entity: ProjectedEntity) -> bool:
    metadata = document.metadata
    return (
        metadata.get("polaris_type") == entity.entity_type
        and metadata.get("polaris_library_id") == str(entity.library.id)
        and metadata.get("polaris_entity_id") == str(entity.entity_id)
    )


async def configure_connection(
    session: AsyncSession, *, user_id: uuid.UUID, vault_path: str,
    managed_directory: str = MANAGED_DIRECTORY,
) -> ObsidianVaultConnection:
    """Commit a location change while preserving all files, merge bases and conflicts.

    A subfolder rename moves the whole managed tree atomically. Switching Vaults copies it
    into a new, unused directory and leaves the original untouched. Never adopt or overwrite
    an existing destination on a location change. The commit stays inside the sync lock;
    failures compensate the filesystem operation and leave the old location authoritative.
    """
    root = validate_vault_root(vault_path)
    directory = validate_managed_directory(managed_directory)
    managed = managed_root(root, directory)
    connection = (
        await session.execute(
            select(ObsidianVaultConnection).where(ObsidianVaultConnection.user_id == user_id)
        )
    ).scalar_one_or_none()
    if connection is None:
        connection = ObsidianVaultConnection(
            user_id=user_id, vault_path=str(root), managed_directory=directory
        )
        session.add(connection)
        await session.flush()
        managed = managed_root(root, directory, create=True)
        atomic_write_text(managed, MANIFEST_FILENAME, render_manifest(connection))
        await session.commit()
        return connection

    connection_id = connection.id
    was_watching = watcher_running(connection_id)
    # Stop outside the lock: an in-flight watcher callback may itself be holding the lock.
    await stop_connection_watcher(connection_id)
    lock = _SYNC_LOCKS.setdefault(connection_id, asyncio.Lock())
    async with lock:
        await session.refresh(connection)
        old_vault = connection.vault_path
        old_directory = connection.managed_directory
        old_root: Path | None = None
        moved = copied = False
        try:
            if old_vault != str(root) or old_directory != directory:
                old_root = managed_root(validate_vault_root(old_vault), old_directory)
                if managed.exists():
                    raise VaultBridgeError("OBSIDIAN_DESTINATION_ALREADY_EXISTS")
                if not old_root.is_dir():
                    raise VaultBridgeError("OBSIDIAN_MANAGED_SOURCE_MISSING")
                # Reject symlinks anywhere, including non-Markdown personal files. A copy
                # must never dereference an external file; a rename must not inherit one.
                for current, directories, filenames in os.walk(old_root, followlinks=False):
                    if any((Path(current) / name).is_symlink() for name in directories + filenames):
                        raise VaultBridgeError("OBSIDIAN_MANAGED_PATH_SYMLINK")
                if old_vault == str(root):
                    old_root.rename(managed)
                    moved = True
                else:
                    # Publish only a complete copy. The temporary directory is owned solely
                    # by this operation and can be removed safely on failure.
                    staging = Path(tempfile.mkdtemp(prefix=".polaris-relocate-", dir=root))
                    try:
                        shutil.copytree(old_root, staging, dirs_exist_ok=True, symlinks=True)
                        staging.rename(managed)
                        copied = True
                    finally:
                        if staging.exists():
                            shutil.rmtree(staging)
            connection.vault_path = str(root)
            connection.managed_directory = directory
            connection.status = "ready"
            connection.last_error = None
            atomic_write_text(managed, MANIFEST_FILENAME, render_manifest(connection))
            await session.commit()
        except BaseException as exc:
            await session.rollback()
            if moved and old_root is not None:
                managed.rename(old_root)
                await session.refresh(connection)
                atomic_write_text(old_root, MANIFEST_FILENAME, render_manifest(connection))
            elif copied:
                # No user edits can have entered via Polaris before the commit/lock release.
                # Keep the copy on disk on rollback rather than risking external editor edits.
                pass
            if was_watching:
                await start_connection_watcher(
                    connection_id=connection_id, user_id=user_id, vault_path=old_vault,
                    managed_directory=old_directory,
                )
            if isinstance(exc, OSError):
                raise VaultBridgeError("OBSIDIAN_DIRECTORY_CHANGE_FAILED") from None
            raise
    return connection


def render_manifest(connection: ObsidianVaultConnection) -> str:
    import json

    return json.dumps(
        {
            "format": "polaris-obsidian-vault",
            "version": BRIDGE_VERSION,
            "connection_id": str(connection.id),
            "managed_directory": connection.managed_directory,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


async def get_connection(
    session: AsyncSession, *, user_id: uuid.UUID
) -> ObsidianVaultConnection | None:
    return (
        await session.execute(
            select(ObsidianVaultConnection).where(ObsidianVaultConnection.user_id == user_id)
        )
    ).scalar_one_or_none()


async def list_bindings(
    session: AsyncSession, *, connection_id: uuid.UUID
) -> list[VaultLibraryBinding]:
    return list(
        (
            await session.execute(
                select(VaultLibraryBinding)
                .where(VaultLibraryBinding.connection_id == connection_id)
                .order_by(VaultLibraryBinding.created_at)
            )
        ).scalars()
    )


async def set_library_binding(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    library_id: uuid.UUID,
    enabled: bool,
) -> VaultLibraryBinding:
    binding = (
        await session.execute(
            select(VaultLibraryBinding).where(
                VaultLibraryBinding.connection_id == connection_id,
                VaultLibraryBinding.library_id == library_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        binding = VaultLibraryBinding(
            connection_id=connection_id, library_id=library_id, enabled=enabled
        )
        session.add(binding)
    else:
        binding.enabled = enabled
    await session.flush()
    return binding


async def _projected_entities(
    session: AsyncSession,
    *,
    binding: VaultLibraryBinding,
    user: User,
    paper_id: uuid.UUID | None = None,
    include_index: bool = True,
) -> list[ProjectedEntity]:
    library = await session.get(DirectionLibrary, binding.library_id)
    if library is None:
        return []
    paper_stmt = (
        select(Paper, PaperWiki)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        # Deleted wiki rows remain part of the bridge projection as tombstones.  This is the
        # one read path that intentionally sees them so a removed Vault file stays removed and
        # a valid file re-created inside the retention window can restore a new revision.
        .outerjoin(PaperWiki, PaperWiki.paper_id == Paper.id)
        .where(
            LibraryPaper.library_id == library.id,
            LibraryPaper.trash_reason.is_(None),
        )
        .order_by(Paper.title, Paper.id)
    )
    if paper_id is not None:
        paper_stmt = paper_stmt.where(Paper.id == paper_id)
    rows = (await session.execute(paper_stmt)).all()
    paper_ids = [paper.id for paper, _wiki in rows]
    note_rows = []
    if paper_ids:
        note_rows = list(
            (
                await session.execute(
                    select(PaperNote)
                    .where(
                        PaperNote.paper_id.in_(paper_ids),
                        PaperNote.author_id == user.id,
                        PaperNote.deleted_at.is_(None),
                    )
                    .order_by(PaperNote.created_at, PaperNote.id)
                )
            ).scalars()
        )
    notes_by_paper: dict[uuid.UUID, list[PaperNote]] = {}
    for note in note_rows:
        notes_by_paper.setdefault(note.paper_id, []).append(note)

    zotero_keys: dict[uuid.UUID, str] = {}
    if paper_ids:
        zotero_rows = (
            await session.execute(
                select(ZoteroItemLink.paper_id, ZoteroItemLink.item_key)
                .join(
                    ZoteroLocalBinding,
                    ZoteroLocalBinding.id == ZoteroItemLink.binding_id,
                )
                .where(
                    ZoteroLocalBinding.library_id == library.id,
                    ZoteroItemLink.paper_id.in_(paper_ids),
                    ZoteroItemLink.status == "active",
                )
                .order_by(ZoteroItemLink.item_key)
            )
        ).all()
        for linked_paper_id, item_key in zotero_rows:
            if linked_paper_id is not None:
                zotero_keys.setdefault(linked_paper_id, item_key)

    directory = library_directory(library)
    entities: list[ProjectedEntity] = []
    index_lines = [f"# {library.name}", "", "## Papers", ""]
    for paper, wiki in rows:
        filename = paper_filename(paper)
        index_lines.append(f"- [[papers/{filename[:-3]}|{paper.title}]]")
        if wiki is not None:
            snapshot = await _summary_snapshot(session, wiki)
            entities.append(
                ProjectedEntity(
                    entity_type="summary",
                    entity_id=paper.id,
                    library=library,
                    paper=paper,
                    relative_path=f"{directory}/papers/{filename}",
                    body=snapshot.content,
                    metadata={
                        "title": paper.title,
                        "authors": _authors(paper),
                        "zotero_key": zotero_keys.get(paper.id),
                        "polaris_revision_id": (
                            str(snapshot.revision_id) if snapshot.revision_id else None
                        ),
                        "polaris_content_version_id": (
                            str(snapshot.content_version_id)
                            if snapshot.content_version_id
                            else None
                        ),
                        "polaris_model": snapshot.model,
                        "polaris_source_level": snapshot.source_level,
                    },
                    domain_deleted=wiki.deleted_at is not None,
                )
            )
        notes = notes_by_paper.get(paper.id, [])
        # Empty notes files are intentional: their ``polaris-new`` block is the Obsidian-side
        # entry point for creating a paper's first personal note.
        entities.append(
            ProjectedEntity(
                entity_type="notes",
                entity_id=paper.id,
                library=library,
                paper=paper,
                relative_path=f"{directory}/notes/{filename}",
                body=render_notes_body(paper, notes),
                metadata={"title": paper.title, "authors": _authors(paper)},
            )
        )
    if include_index:
        index_lines.extend(
            ["", "Generated by Polaris. Paper summaries and notes are editable.", ""]
        )
        entities.append(
            ProjectedEntity(
                entity_type="library_index",
                entity_id=library.id,
                library=library,
                paper=None,
                relative_path=f"{directory}/index.md",
                body=normalize_markdown("\n".join(index_lines)),
                metadata={"title": library.name},
                editable=False,
            )
        )
    return entities


def _scan_identity_index(root: Path) -> dict[tuple[str, str, str], str]:
    """Index managed documents by stable frontmatter ids so user renames are preserved."""
    index: dict[tuple[str, str, str], str] = {}
    for path in root.rglob("*.md"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            safe_managed_path(root, relative)
            document = parse_markdown_document(path.read_text(encoding="utf-8"))
            key = (
                str(document.metadata.get("polaris_type", "")),
                str(document.metadata.get("polaris_library_id", "")),
                str(document.metadata.get("polaris_entity_id", "")),
            )
            if all(key) and key not in index:
                index[key] = relative
        except (OSError, UnicodeError, VaultBridgeError):
            continue
    return index


async def _find_state(
    session: AsyncSession, *, connection_id: uuid.UUID, entity: ProjectedEntity
) -> VaultFileState | None:
    return (
        await session.execute(
            select(VaultFileState).where(
                VaultFileState.connection_id == connection_id,
                VaultFileState.library_id == entity.library.id,
                VaultFileState.entity_type == entity.entity_type,
                VaultFileState.entity_id == entity.entity_id,
            )
        )
    ).scalar_one_or_none()


async def _apply_entity_content(
    adapter: VaultDomainAdapter,
    session: AsyncSession,
    *,
    entity: ProjectedEntity,
    user: User,
    content: str,
    restore: bool = False,
) -> str:
    if entity.paper is None:
        return content
    if entity.entity_type == "summary":
        method = adapter.restore_summary if restore else adapter.apply_summary
        await method(session, paper=entity.paper, user=user, content=content)
    elif entity.entity_type == "notes":
        method = adapter.restore_notes if restore else adapter.apply_notes
        await method(session, paper=entity.paper, user=user, content=content)
        notes = list((await session.scalars(
            select(PaperNote).where(
                PaperNote.paper_id == entity.paper.id,
                PaperNote.author_id == user.id,
                PaperNote.deleted_at.is_(None),
            )
        )).all())
        # The merge base must contain the newly assigned identities, not the consumed new slot.
        return render_notes_body(entity.paper, notes)
    return content


async def _soft_delete_entity(
    adapter: VaultDomainAdapter,
    session: AsyncSession,
    *,
    entity: ProjectedEntity,
    user: User,
) -> bool:
    if entity.entity_type == "summary":
        return await adapter.soft_delete_summary(
            session, paper_id=entity.entity_id, user=user
        )
    if entity.entity_type == "notes":
        return await adapter.soft_delete_notes(session, paper_id=entity.entity_id, user=user)
    return True


def _set_state_content(state: VaultFileState, content: str, *, status: str = "synced") -> None:
    normalized = normalize_markdown(content)
    digest = content_hash(normalized)
    state.base_content = normalized
    state.base_hash = digest
    state.polaris_hash = digest
    state.vault_hash = digest
    state.status = status
    state.deleted_at = None


def _write_entity(root: Path, entity: ProjectedEntity, state: VaultFileState, content: str) -> None:
    normalized = normalize_markdown(content)
    metadata = _entity_metadata(entity, baseline_hash=content_hash(normalized))
    atomic_write_text(root, state.relative_path, render_markdown_document(metadata, normalized))


def _conflict_companion_path(
    library: DirectionLibrary, state: VaultFileState, conflict_id: uuid.UUID
) -> str:
    return (
        f"{library_directory(library)}/conflicts/"
        f"{Path(state.relative_path).stem}-conflict-{conflict_id.hex[:8]}.md"
    )


async def _record_conflict(
    session: AsyncSession,
    *,
    root: Path,
    connection: ObsidianVaultConnection,
    state: VaultFileState,
    entity: ProjectedEntity,
    base: str,
    polaris: str,
    vault: str,
) -> VaultConflict:
    existing = (
        await session.execute(
            select(VaultConflict).where(
                VaultConflict.file_state_id == state.id, VaultConflict.status == "open"
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        conflict = VaultConflict(
            connection_id=connection.id,
            file_state_id=state.id,
            library_id=entity.library.id,
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            relative_path=state.relative_path,
            base_content=base,
            polaris_content=polaris,
            vault_content=vault,
        )
        session.add(conflict)
    else:
        # Keep an open conflict current while either side continues editing. Resolution must
        # never apply a stale snapshot over newer work.
        conflict = existing
        conflict.relative_path = state.relative_path
        conflict.polaris_content = polaris
        conflict.vault_content = vault
    await session.flush()
    state.status = "conflict"
    companion = _conflict_companion_path(entity.library, state, conflict.id)
    metadata = {
        "polaris_type": "conflict",
        "polaris_conflict_id": str(conflict.id),
        "polaris_entity_type": entity.entity_type,
        "polaris_entity_id": str(entity.entity_id),
        "status": "open",
    }
    body = (
        "# Polaris sync conflict\n\n"
        "Resolve this conflict in Polaris. Both versions are preserved below.\n\n"
        "## Polaris\n\n"
        f"{polaris.rstrip()}\n\n"
        "## Obsidian\n\n"
        f"{vault.rstrip()}\n"
    )
    rendered = render_markdown_document(metadata, body)
    target = safe_managed_path(root, companion)
    if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
        atomic_write_text(root, companion, rendered)
    return conflict


async def _sync_entity(
    session: AsyncSession,
    *,
    root: Path,
    connection: ObsidianVaultConnection,
    entity: ProjectedEntity,
    identity_index: dict[tuple[str, str, str], str],
    user: User,
    adapter: VaultDomainAdapter,
    stats: VaultSyncStats,
) -> None:
    polaris = normalize_markdown(entity.body)
    state = await _find_state(session, connection_id=connection.id, entity=entity)
    is_new_state = state is None
    identity = (entity.entity_type, str(entity.library.id), str(entity.entity_id))
    discovered_path = identity_index.get(identity)
    relative_path = discovered_path or (state.relative_path if state else entity.relative_path)
    if state is None:
        digest = content_hash(polaris)
        state = VaultFileState(
            connection_id=connection.id,
            library_id=entity.library.id,
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            relative_path=relative_path,
            base_content=polaris,
            base_hash=digest,
            polaris_hash=digest,
            vault_hash=digest,
        )
        session.add(state)
        await session.flush()
    elif discovered_path and discovered_path != state.relative_path:
        state.relative_path = discovered_path

    path = safe_managed_path(root, state.relative_path)
    # A deletion initiated inside Polaris is projected outward once.  Do not read the still
    # existing Vault copy as a user edit and immediately resurrect it.  A later re-created file
    # enters the explicit tombstone restore branch below and produces a fresh revision.
    if entity.domain_deleted and state.status not in {"deleted", "delete_pending"}:
        if path.is_file() and not path.is_symlink():
            try:
                document = parse_markdown_document(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, VaultBridgeError) as exc:
                stats.errors.append(getattr(exc, "code", "OBSIDIAN_FILE_READ_FAILED"))
                return
            if not _document_matches_entity(document, entity):
                stats.errors.append("OBSIDIAN_FILE_IDENTITY_MISMATCH")
                return
            if content_hash(document.body) != state.base_hash:
                await _record_conflict(
                    session,
                    root=root,
                    connection=connection,
                    state=state,
                    entity=entity,
                    base=state.base_content,
                    polaris="",
                    vault=document.body,
                )
                stats.conflicts += 1
                return
            path.unlink()
            stats.files_deleted += 1
        state.status = "deleted"
        state.deleted_at = utcnow()
        state.polaris_hash = content_hash(polaris)
        state.vault_hash = content_hash("")
        return
    if state.status == "conflict":
        open_count = await session.scalar(
            select(func.count())
            .select_from(VaultConflict)
            .where(VaultConflict.file_state_id == state.id, VaultConflict.status == "open")
        )
        if open_count:
            vault = ""
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    stats.errors.append("OBSIDIAN_MANAGED_PATH_SYMLINK")
                    return
                try:
                    document = parse_markdown_document(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, VaultBridgeError) as exc:
                    stats.errors.append(getattr(exc, "code", "OBSIDIAN_FILE_READ_FAILED"))
                    return
                if not _document_matches_entity(document, entity):
                    stats.errors.append("OBSIDIAN_FILE_IDENTITY_MISMATCH")
                    return
                vault = document.body
            await _record_conflict(
                session,
                root=root,
                connection=connection,
                state=state,
                entity=entity,
                base=state.base_content,
                polaris=polaris,
                vault=vault,
            )
            stats.conflicts += 1
            return
        state.status = "synced"

    if not path.exists():
        if is_new_state:
            if entity.domain_deleted:
                state.status = "deleted"
                state.deleted_at = utcnow()
                state.vault_hash = content_hash("")
                stats.files_unchanged += 1
                return
            _write_entity(root, entity, state, polaris)
            _set_state_content(state, polaris)
            stats.files_written += 1
            return
        if state.deleted_at is not None or state.status in {"deleted", "delete_pending"}:
            restored_in_polaris = entity.entity_type == "summary" and not entity.domain_deleted
            if entity.entity_type == "notes":
                restored_in_polaris = bool(parse_notes_body(polaris)[0])
            if restored_in_polaris:
                _set_state_content(state, polaris)
                _write_entity(root, entity, state, polaris)
                stats.files_written += 1
                return
            stats.files_unchanged += 1
            return
        if content_hash(polaris) != state.base_hash and entity.editable:
            await _record_conflict(
                session,
                root=root,
                connection=connection,
                state=state,
                entity=entity,
                base=state.base_content,
                polaris=polaris,
                vault="",
            )
            stats.conflicts += 1
            return
        supported = await _soft_delete_entity(
            adapter, session, entity=entity, user=user
        )
        state.status = "deleted" if supported else "delete_pending"
        state.deleted_at = utcnow()
        state.polaris_hash = content_hash(polaris)
        state.vault_hash = content_hash("")
        stats.files_deleted += 1
        return

    try:
        document = parse_markdown_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, VaultBridgeError) as exc:
        stats.errors.append(getattr(exc, "code", "OBSIDIAN_FILE_READ_FAILED"))
        return
    if not _document_matches_entity(document, entity):
        stats.errors.append("OBSIDIAN_FILE_IDENTITY_MISMATCH")
        return
    vault = document.body

    if state.deleted_at is not None or state.status in {"deleted", "delete_pending"}:
        if state.deleted_at is not None:
            deleted_at = state.deleted_at
            if deleted_at.tzinfo is None:
                deleted_at = deleted_at.replace(tzinfo=UTC)
            if utcnow() > deleted_at + timedelta(days=DELETION_RETENTION_DAYS):
                raise VaultBridgeError("OBSIDIAN_RESTORE_EXPIRED")
        if entity.editable:
            vault = await _apply_entity_content(
                adapter,
                session,
                entity=entity,
                user=user,
                content=vault,
                restore=True,
            )
            await _refresh_entity_metadata(session, entity)
            stats.files_imported += 1
        _set_state_content(state, vault)
        _write_entity(root, entity, state, vault)
        stats.files_written += 1
        return

    if not entity.editable:
        if vault != polaris:
            _set_state_content(state, polaris)
            _write_entity(root, entity, state, polaris)
            stats.files_written += 1
        else:
            _set_state_content(state, polaris)
            stats.files_unchanged += 1
        return

    outcome = three_way_merge(state.base_content, polaris, vault)
    if outcome.status == "conflict" or outcome.content is None:
        await _record_conflict(
            session,
            root=root,
            connection=connection,
            state=state,
            entity=entity,
            base=state.base_content,
            polaris=polaris,
            vault=vault,
        )
        stats.conflicts += 1
        return

    selected = outcome.content
    if outcome.status in {"vault", "merged"}:
        selected = await _apply_entity_content(
            adapter, session, entity=entity, user=user, content=selected
        )
        await _refresh_entity_metadata(session, entity)
        stats.files_imported += 1
    should_write = outcome.status in {"polaris", "merged"} or (
        document.metadata.get("polaris_baseline_hash") != content_hash(selected)
    ) or document.metadata != _entity_metadata(
        entity, baseline_hash=content_hash(selected)
    )
    _set_state_content(state, selected)
    if should_write:
        _write_entity(root, entity, state, selected)
        stats.files_written += 1
    else:
        stats.files_unchanged += 1


async def _sync_connection_unlocked(
    session: AsyncSession,
    *,
    connection: ObsidianVaultConnection,
    user: User,
    library_id: uuid.UUID | None = None,
    paper_id: uuid.UUID | None = None,
    entity_types: frozenset[str] | None = None,
    adapter: VaultDomainAdapter = DEFAULT_DOMAIN_ADAPTER,
) -> VaultSyncStats:
    """Reconcile all enabled bindings (or one selected binding) against the vault."""
    stats = VaultSyncStats()
    try:
        vault = validate_vault_root(connection.vault_path)
        root = managed_root(vault, connection.managed_directory)
        if not root.is_dir():
            # Missing a whole managed tree (unmounted storage, external rename or interrupted
            # relocation) is never a request to delete every summary and private note.
            raise VaultBridgeError("OBSIDIAN_MANAGED_SOURCE_MISSING")
        atomic_write_text(root, MANIFEST_FILENAME, render_manifest(connection))
        identity_index = _scan_identity_index(root)
        stmt = select(VaultLibraryBinding).where(
            VaultLibraryBinding.connection_id == connection.id,
            VaultLibraryBinding.enabled.is_(True),
        )
        if library_id is not None:
            stmt = stmt.where(VaultLibraryBinding.library_id == library_id)
        bindings = list((await session.execute(stmt.order_by(VaultLibraryBinding.id))).scalars())
        for binding in bindings:
            library = await session.get(DirectionLibrary, binding.library_id)
            if library is None or not await libraries_service.can_manage_library(
                session, library=library, user=user
            ):
                stats.errors.append("OBSIDIAN_LIBRARY_ACCESS_REVOKED")
                continue
            entities = await _projected_entities(
                session,
                binding=binding,
                user=user,
                paper_id=paper_id,
                include_index=paper_id is None,
            )
            for entity in entities:
                if entity_types is not None and entity.entity_type not in entity_types:
                    continue
                try:
                    await _sync_entity(
                        session,
                        root=root,
                        connection=connection,
                        entity=entity,
                        identity_index=identity_index,
                        user=user,
                        adapter=adapter,
                        stats=stats,
                    )
                except (OSError, VaultBridgeError) as exc:
                    stats.errors.append(getattr(exc, "code", "OBSIDIAN_FILE_WRITE_FAILED"))
            binding.last_synced_at = utcnow()
        connection.last_synced_at = utcnow()
        connection.status = "error" if stats.errors else "ready"
        connection.last_error = stats.errors[0] if stats.errors else None
        await session.flush()
        return stats
    except VaultBridgeError as exc:
        connection.status = "error"
        connection.last_error = exc.code
        await session.flush()
        stats.errors.append(exc.code)
        return stats


async def sync_connection(
    session: AsyncSession,
    *,
    connection: ObsidianVaultConnection,
    user: User,
    library_id: uuid.UUID | None = None,
    paper_id: uuid.UUID | None = None,
    entity_types: frozenset[str] | None = None,
    adapter: VaultDomainAdapter = DEFAULT_DOMAIN_ADAPTER,
) -> VaultSyncStats:
    """Serialize watcher, manual, and projection reconciliation for one local connection."""
    lock = _SYNC_LOCKS.setdefault(connection.id, asyncio.Lock())
    async with lock:
        # A location change may have committed while this reconciliation awaited the lock.
        await session.refresh(connection, attribute_names=["vault_path", "managed_directory"])
        stats = await _sync_connection_unlocked(
            session,
            connection=connection,
            user=user,
            library_id=library_id,
            paper_id=paper_id,
            entity_types=entity_types,
            adapter=adapter,
        )
        # The next reconciler must observe the committed merge base before it reads our files.
        await session.commit()
        return stats


async def sync_paper_to_vaults(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    entity_types: frozenset[str] | None = None,
) -> VaultSyncStats:
    """Incrementally project one paper to every enabled local Vault binding that contains it.

    ``user_id`` narrows private note projection to the note owner.  Shared summaries deliberately
    omit it so every library copy of the global ``PaperWiki`` is updated.
    """
    stats = VaultSyncStats()
    stmt = (
        select(ObsidianVaultConnection, User)
        .join(User, User.id == ObsidianVaultConnection.user_id)
        .join(
            VaultLibraryBinding,
            VaultLibraryBinding.connection_id == ObsidianVaultConnection.id,
        )
        .join(
            LibraryPaper,
            LibraryPaper.library_id == VaultLibraryBinding.library_id,
        )
        .where(
            VaultLibraryBinding.enabled.is_(True),
            LibraryPaper.paper_id == paper_id,
            LibraryPaper.trash_reason.is_(None),
        )
        .distinct()
    )
    if user_id is not None:
        stmt = stmt.where(ObsidianVaultConnection.user_id == user_id)
    rows = (await session.execute(stmt)).all()
    for connection, user in rows:
        current = await sync_connection(
            session,
            connection=connection,
            user=user,
            paper_id=paper_id,
            entity_types=entity_types,
        )
        stats.files_written += current.files_written
        stats.files_imported += current.files_imported
        stats.files_unchanged += current.files_unchanged
        stats.files_deleted += current.files_deleted
        stats.conflicts += current.conflicts
        stats.errors.extend(current.errors)
    return stats


async def sync_library_to_vaults(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    entity_types: frozenset[str] | None = None,
) -> VaultSyncStats:
    """Project one selected library to every local Vault connection that enabled it."""
    stats = VaultSyncStats()
    rows = (
        await session.execute(
            select(ObsidianVaultConnection, User)
            .join(User, User.id == ObsidianVaultConnection.user_id)
            .join(
                VaultLibraryBinding,
                VaultLibraryBinding.connection_id == ObsidianVaultConnection.id,
            )
            .where(
                VaultLibraryBinding.library_id == library_id,
                VaultLibraryBinding.enabled.is_(True),
            )
        )
    ).all()
    for connection, user in rows:
        current = await sync_connection(
            session,
            connection=connection,
            user=user,
            library_id=library_id,
            entity_types=entity_types,
        )
        stats.files_written += current.files_written
        stats.files_imported += current.files_imported
        stats.files_unchanged += current.files_unchanged
        stats.files_deleted += current.files_deleted
        stats.conflicts += current.conflicts
        stats.errors.extend(current.errors)
    return stats


async def enqueue_paper_projection(
    *,
    paper_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    entity_type: Literal["summary", "notes"] | None = None,
) -> None:
    """Best-effort Desktop enqueue after a domain transaction has committed."""
    from app.core.config import get_settings

    if not get_settings().is_desktop:
        return
    try:
        from app.core.queue import get_task_queue

        queue = await get_task_queue()
        await queue.enqueue(
            "sync_obsidian_vault_paper_task",
            str(paper_id),
            str(user_id) if user_id else None,
            entity_type,
        )
    except Exception:  # noqa: BLE001 - Vault projection never rolls back the authoritative DB edit
        logger.warning("failed to enqueue Obsidian projection for paper %s", paper_id)


async def enqueue_library_projection(
    *,
    library_id: uuid.UUID,
    entity_type: Literal["summary", "notes"] | None = None,
) -> None:
    """Best-effort Desktop enqueue for a batch that changed one whole library."""
    from app.core.config import get_settings

    if not get_settings().is_desktop:
        return
    try:
        from app.core.queue import get_task_queue

        queue = await get_task_queue()
        await queue.enqueue(
            "sync_obsidian_vault_library_task",
            str(library_id),
            entity_type,
        )
    except Exception:  # noqa: BLE001
        logger.warning("failed to enqueue Obsidian projection for library %s", library_id)


async def open_conflicts(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    user: User,
    status: str = "open",
) -> list[VaultConflict]:
    return list(
        (
            await session.execute(
                select(VaultConflict)
                .join(
                    DirectionLibrary,
                    DirectionLibrary.id == VaultConflict.library_id,
                )
                .where(
                    VaultConflict.connection_id == connection_id,
                    VaultConflict.status == status,
                    or_(
                        DirectionLibrary.submitted_by.is_(None),
                        DirectionLibrary.submitted_by == user.id,
                    ),
                )
                .order_by(VaultConflict.created_at.desc())
            )
        ).scalars()
    )


async def resolve_conflict(
    session: AsyncSession,
    *,
    conflict: VaultConflict,
    strategy: Literal["polaris", "vault", "merged"],
    user: User,
    content: str | None = None,
    adapter: VaultDomainAdapter = DEFAULT_DOMAIN_ADAPTER,
    expected_version: str | None = None,
) -> VaultConflict:
    expected = expected_version or conflict.version
    lock = _SYNC_LOCKS.setdefault(conflict.connection_id, asyncio.Lock())
    async with lock:
        await session.refresh(conflict)
        if conflict.version != expected:
            raise VaultBridgeError("OBSIDIAN_CONFLICT_CHANGED")
        result = await _resolve_conflict_unlocked(
            session, conflict=conflict, strategy=strategy, user=user,
            content=content, adapter=adapter,
        )
        await session.commit()
        return result


async def _resolve_conflict_unlocked(
    session: AsyncSession,
    *,
    conflict: VaultConflict,
    strategy: Literal["polaris", "vault", "merged"],
    user: User,
    content: str | None,
    adapter: VaultDomainAdapter,
) -> VaultConflict:
    if conflict.status != "open":
        raise VaultBridgeError("OBSIDIAN_CONFLICT_ALREADY_RESOLVED")
    if strategy == "merged":
        if content is None or not content.strip():
            raise VaultBridgeError("OBSIDIAN_MERGED_CONTENT_REQUIRED")
        selected = normalize_markdown(content)
    elif strategy == "polaris":
        selected = normalize_markdown(conflict.polaris_content)
    else:
        selected = normalize_markdown(conflict.vault_content)

    connection = await session.get(ObsidianVaultConnection, conflict.connection_id)
    state = await session.get(VaultFileState, conflict.file_state_id)
    library = await session.get(DirectionLibrary, conflict.library_id)
    paper = (
        await session.get(Paper, conflict.entity_id)
        if conflict.entity_type != "library_index"
        else None
    )
    if connection is None or state is None or library is None:
        raise VaultBridgeError("OBSIDIAN_CONFLICT_TARGET_MISSING")
    await session.refresh(connection, attribute_names=["vault_path", "managed_directory"])
    if not await libraries_service.can_manage_library(
        session, user=user, library=library
    ):
        raise VaultBridgeError("OBSIDIAN_CONFLICT_NOT_FOUND")
    entity = ProjectedEntity(
        entity_type=conflict.entity_type,  # type: ignore[arg-type]
        entity_id=conflict.entity_id,
        library=library,
        paper=paper,
        relative_path=state.relative_path,
        body=selected,
        metadata={"title": paper.title if paper else library.name},
        editable=conflict.entity_type != "library_index",
    )
    root = managed_root(validate_vault_root(connection.vault_path), connection.managed_directory)
    if not root.is_dir():
        raise VaultBridgeError("OBSIDIAN_MANAGED_SOURCE_MISSING")
    target = safe_managed_path(root, state.relative_path)
    live_vault = ""
    original_raw: str | None = None
    if target.exists():
        original_raw = target.read_text(encoding="utf-8")
        document = parse_markdown_document(original_raw)
        if not _document_matches_entity(document, entity):
            raise VaultBridgeError("OBSIDIAN_FILE_IDENTITY_MISMATCH")
        live_vault = document.body
    live_polaris = conflict.polaris_content
    if entity.entity_type == "summary":
        wiki = await session.scalar(
            select(PaperWiki).where(PaperWiki.paper_id == entity.entity_id)
            .execution_options(populate_existing=True)
        )
        live_polaris = wiki.content if wiki is not None and wiki.deleted_at is None else ""
    elif entity.entity_type == "notes" and paper is not None:
        notes = list((await session.scalars(
            select(PaperNote).where(
                PaperNote.paper_id == paper.id, PaperNote.author_id == user.id,
                PaperNote.deleted_at.is_(None),
            ).execution_options(populate_existing=True)
        )).all())
        live_polaris = render_notes_body(paper, notes)
    if (normalize_markdown(live_vault) != normalize_markdown(conflict.vault_content)
            or normalize_markdown(live_polaris) != normalize_markdown(conflict.polaris_content)):
        await _record_conflict(
            session, root=root, connection=connection, state=state, entity=entity,
            base=state.base_content, polaris=live_polaris, vault=live_vault,
        )
        await session.commit()
        raise VaultBridgeError("OBSIDIAN_CONFLICT_CHANGED")
    accepts_deletion = entity.editable and not selected.strip()
    if accepts_deletion:
        await _soft_delete_entity(adapter, session, entity=entity, user=user)
        target = safe_managed_path(root, state.relative_path)
        if (target.read_text(encoding="utf-8") if target.exists() else None) != original_raw:
            await session.rollback()
            raise VaultBridgeError("OBSIDIAN_CONFLICT_CHANGED")
        if target.is_file() and not target.is_symlink():
            target.unlink()
        state.status = "deleted"
        state.deleted_at = utcnow()
        state.polaris_hash = content_hash("")
        state.vault_hash = content_hash("")
    else:
        if strategy in {"vault", "merged"} and entity.editable:
            selected = await _apply_entity_content(
                adapter, session, entity=entity, user=user, content=selected
            )
        await _refresh_entity_metadata(session, entity)
        target = safe_managed_path(root, state.relative_path)
        if (target.read_text(encoding="utf-8") if target.exists() else None) != original_raw:
            await session.rollback()
            raise VaultBridgeError("OBSIDIAN_CONFLICT_CHANGED")
        _set_state_content(state, selected)
        _write_entity(root, entity, state, selected)
    companion = safe_managed_path(
        root,
        _conflict_companion_path(library, state, conflict.id),
    )
    if companion.is_file() and not companion.is_symlink():
        companion.unlink()
    conflict.status = "resolved"
    conflict.resolution = strategy
    conflict.resolved_content = selected
    conflict.resolved_at = utcnow()
    await session.flush()
    return conflict


async def purge_expired_tombstones(
    session: AsyncSession, *, now: Any | None = None
) -> int:
    """Remove bridge-only tombstones after 30 days; domain entities are never deleted here."""
    cutoff = (now or utcnow()) - timedelta(days=DELETION_RETENTION_DAYS)
    states = list(
        (
            await session.execute(
                select(VaultFileState).where(
                    VaultFileState.deleted_at.is_not(None), VaultFileState.deleted_at < cutoff
                )
            )
        ).scalars()
    )
    for state in states:
        await session.delete(state)
    await session.flush()
    return len(states)


class VaultWatcher:
    """Dependency-free polling watcher with a 750 ms debounce.

    Startup code supplies a callback that opens its own database session and invokes
    :func:`sync_connection`.  Keeping session ownership outside prevents a long-lived watcher
    from retaining an ``AsyncSession`` across unrelated desktop requests.
    """

    def __init__(
        self,
        root: Path,
        callback: Callable[[], Awaitable[None]],
        *,
        debounce_seconds: float = 0.75,
        poll_seconds: float = 0.25,
    ) -> None:
        self.root = root
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _snapshot(self) -> tuple[tuple[str, int, int], ...]:
        rows: list[tuple[str, int, int]] = []
        if not self.root.is_dir() or self.root.is_symlink():
            return ()
        for path in self.root.rglob("*.md"):
            try:
                if "conflicts" in path.relative_to(self.root).parts:
                    continue  # generated conflict companions are not editable inputs
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                rows.append(
                    (path.relative_to(self.root).as_posix(), stat.st_mtime_ns, stat.st_size)
                )
            except OSError:
                continue
        return tuple(sorted(rows))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        self._task = None

    async def _run(self) -> None:
        previous = await asyncio.to_thread(self._snapshot)
        changed_at: float | None = None
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                continue
            except TimeoutError:
                pass
            from app.core.desktop_runtime import paused

            if paused():
                continue
            current = await asyncio.to_thread(self._snapshot)
            if current != previous:
                previous = current
                changed_at = loop.time()
            if changed_at is not None and loop.time() - changed_at >= self.debounce_seconds:
                changed_at = None
                try:
                    await self.callback()
                except Exception:  # noqa: BLE001 - one failed reconciliation must not kill watch
                    continue


_WATCHERS: dict[uuid.UUID, VaultWatcher] = {}


def watcher_running(connection_id: uuid.UUID) -> bool:
    watcher = _WATCHERS.get(connection_id)
    return watcher is not None and watcher._task is not None and not watcher._task.done()


async def start_connection_watcher(
    *, connection_id: uuid.UUID, user_id: uuid.UUID, vault_path: str,
    managed_directory: str = MANAGED_DIRECTORY,
) -> None:
    """Start or replace the process-local watcher for one desktop connection."""
    await stop_connection_watcher(connection_id)
    root = managed_root(validate_vault_root(vault_path), managed_directory)
    if not root.is_dir():
        raise VaultBridgeError("OBSIDIAN_MANAGED_SOURCE_MISSING")

    async def reconcile() -> None:
        # Delayed import avoids binding the service module to one engine during tests.
        from app.core.db import get_sessionmaker

        global _ACTIVE_RECONCILIATIONS
        _ACTIVE_RECONCILIATIONS += 1
        try:
            async with get_sessionmaker()() as session:
                connection = await session.get(ObsidianVaultConnection, connection_id)
                user = await session.get(User, user_id)
                if connection is None or user is None:
                    _WATCHERS.pop(connection_id, None)
                    return
                await sync_connection(session, connection=connection, user=user)
                await session.commit()
        finally:
            _ACTIVE_RECONCILIATIONS -= 1

    watcher = VaultWatcher(root, reconcile)
    _WATCHERS[connection_id] = watcher
    watcher.start()


async def stop_connection_watcher(connection_id: uuid.UUID) -> None:
    watcher = _WATCHERS.pop(connection_id, None)
    if watcher is not None:
        await watcher.stop()


async def stop_all_watchers() -> None:
    for connection_id in list(_WATCHERS):
        await stop_connection_watcher(connection_id)


async def resume_configured_watchers() -> None:
    """Desktop startup hook: full reconciliation first, then begin watching changes."""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(ObsidianVaultConnection))).scalars())
    for connection in rows:
        try:
            async with get_sessionmaker()() as session:
                current = await session.get(ObsidianVaultConnection, connection.id)
                user = await session.get(User, connection.user_id)
                if current is None or user is None:
                    continue
                await sync_connection(session, connection=current, user=user)
                await session.commit()
            await start_connection_watcher(
                connection_id=connection.id,
                user_id=connection.user_id,
                vault_path=connection.vault_path,
                managed_directory=connection.managed_directory,
            )
        except (OSError, VaultBridgeError):
            continue
