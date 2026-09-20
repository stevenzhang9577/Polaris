"""Configurable Vault folder: safe paths, preservation, watching and rollback."""

import json

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.obsidian_vault import ObsidianVaultConnection, VaultConflict, VaultFileState
from app.models.paper import PaperWiki
from app.services import obsidian_vault_bridge as bridge
from app.services import paper_wiki
from tests.conftest import register_and_login
from tests.test_local_integrations_regressions import setup_vault


@pytest.mark.parametrize(
    "directory",
    ["", "..", ".obsidian", "../outside", "/tmp/escape", "a/b", "a\\b", " a", "a ",
     "a.", "a\x00b", "a:b", ".hidden", "x" * 129],
)
def test_reject_unsafe_managed_directory(tmp_path, directory):
    with pytest.raises(bridge.VaultBridgeError, match="OBSIDIAN_MANAGED_DIRECTORY_INVALID"):
        bridge.managed_root(tmp_path, directory, create=True)
    assert list(tmp_path.iterdir()) == []


def test_custom_directory_symlink_and_file_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "008-Polaris").symlink_to(outside, target_is_directory=True)
    with pytest.raises(bridge.VaultBridgeError, match="SYMLINK"):
        bridge.managed_root(vault, "008-Polaris", create=True)
    (vault / "not-a-folder").write_text("keep")
    with pytest.raises(bridge.VaultBridgeError, match="INVALID"):
        bridge.managed_root(vault, "not-a-folder")


async def summary_state(session, connection):
    return await session.scalar(select(VaultFileState).where(
        VaultFileState.connection_id == connection.id,
        VaultFileState.entity_type == "summary",
    ))


@pytest.mark.asyncio
async def test_rename_preserves_unsynced_edits_and_original_merge_base(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await summary_state(session, connection)
        state_id, base_hash = state.id, state.base_hash
        old = vault / "Polaris"
        file = old / state.relative_path
        text = file.read_text().replace("## TL;DR\nbase\n", "## TL;DR\nunsynced edit\n")
        file.write_text(text)
        (old / "personal-extra.txt").write_text("keep this too")

        await bridge.configure_connection(
            session, user_id=user.id, vault_path=str(vault), managed_directory="008-Polaris"
        )
        await session.refresh(state)
        target = vault / "008-Polaris"
        assert not old.exists()
        assert state.id == state_id and state.base_hash == base_hash
        assert (target / state.relative_path).read_text() == text
        assert (target / "personal-extra.txt").read_text() == "keep this too"
        assert json.loads((target / bridge.MANIFEST_FILENAME).read_text())[
            "managed_directory"
        ] == "008-Polaris"
        result = await bridge.sync_connection(session, connection=connection, user=user)
        assert result.files_imported == 1 and not result.errors
        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        assert "unsynced edit" in wiki.content and wiki.deleted_at is None
        assert not old.exists(), "Subsequent projection must never recreate the old folder"


@pytest.mark.asyncio
async def test_open_conflict_and_companion_survive_directory_change(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        state = await summary_state(session, connection)
        file = vault / "Polaris" / state.relative_path
        file.write_text(file.read_text().replace("## TL;DR\nbase\n", "## TL;DR\nvault edit\n"))
        await paper_wiki.upsert_wiki(
            session, paper=paper, content="## TL;DR\nserver edit\n", compiled_by=user.id
        )
        result = await bridge.sync_connection(session, connection=connection, user=user)
        assert result.conflicts == 1
        conflict = await session.scalar(select(VaultConflict))
        conflict_id = conflict.id
        companions = list((vault / "Polaris").rglob("conflicts/*.md"))
        relative = companions[0].relative_to(vault / "Polaris")
        companion_text = companions[0].read_text()
        await bridge.configure_connection(
            session, user_id=user.id, vault_path=str(vault), managed_directory="008-Polaris"
        )
        await session.refresh(conflict)
        assert conflict.id == conflict_id and conflict.status == "open"
        assert (vault / "008-Polaris" / relative).read_text() == companion_text
        await bridge.resolve_conflict(session, conflict=conflict, strategy="vault", user=user)
        await session.commit()
        assert "vault edit" in (vault / "008-Polaris" / state.relative_path).read_text()
        assert not (vault / "Polaris").exists()


@pytest.mark.asyncio
async def test_existing_destination_never_overwritten_and_config_kept(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, _paper, connection = await setup_vault(session, tmp_path)
        target = vault / "008-Polaris"
        target.mkdir()
        (target / "mine.md").write_text("existing private file")
        with pytest.raises(bridge.VaultBridgeError, match="DESTINATION_ALREADY_EXISTS"):
            await bridge.configure_connection(
                session, user_id=user.id, vault_path=str(vault), managed_directory="008-Polaris"
            )
        await session.refresh(connection)
        assert connection.managed_directory == "Polaris"
        assert (vault / "Polaris").is_dir()
        assert (target / "mine.md").read_text() == "existing private file"


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_directory_move(app, tmp_path, monkeypatch):
    async with get_sessionmaker()() as session:
        vault, user, _paper, connection = await setup_vault(session, tmp_path)
        state = await summary_state(session, connection)
        relative = state.relative_path
        original = (vault / "Polaris" / relative).read_text()

        async def failed_commit():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(session, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await bridge.configure_connection(
                session, user_id=user.id, vault_path=str(vault), managed_directory="008-Polaris"
            )
        await session.refresh(connection)
        assert connection.managed_directory == "Polaris"
        assert (vault / "Polaris" / relative).read_text() == original
        assert not (vault / "008-Polaris").exists()
        assert json.loads((vault / "Polaris" / bridge.MANIFEST_FILENAME).read_text())[
            "managed_directory"
        ] == "Polaris"


@pytest.mark.asyncio
async def test_switch_vault_preserves_old_files_and_reuses_baseline(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, _paper, connection = await setup_vault(session, tmp_path)
        state = await summary_state(session, connection)
        original_id = state.id
        other = tmp_path / "Other Vault"
        (other / ".obsidian").mkdir(parents=True)
        file = vault / "Polaris" / state.relative_path
        file.write_text(file.read_text().replace("## TL;DR\nbase\n", "## TL;DR\nlatest edit\n"))
        raw = file.read_text()
        await bridge.configure_connection(
            session, user_id=user.id, vault_path=str(other), managed_directory="008-Polaris"
        )
        assert file.read_text() == raw
        assert (other / "008-Polaris" / state.relative_path).read_text() == raw
        assert (await summary_state(session, connection)).id == original_id
        result = await bridge.sync_connection(session, connection=connection, user=user)
        assert result.files_imported == 1


@pytest.mark.asyncio
async def test_missing_entire_directory_never_becomes_mass_deletion(app, tmp_path):
    async with get_sessionmaker()() as session:
        vault, user, paper, connection = await setup_vault(session, tmp_path)
        (vault / "Polaris").rename(vault / "external-rename")
        result = await bridge.sync_connection(session, connection=connection, user=user)
        assert result.errors == ["OBSIDIAN_MANAGED_SOURCE_MISSING"]
        assert result.files_deleted == 0
        wiki = await session.scalar(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        assert wiki.deleted_at is None and wiki.content
        state = await summary_state(session, connection)
        assert state.deleted_at is None
        with pytest.raises(bridge.VaultBridgeError, match="MANAGED_SOURCE_MISSING"):
            await bridge.start_connection_watcher(
                connection_id=connection.id, user_id=user.id, vault_path=str(vault)
            )
        assert not (vault / "Polaris").exists()


@pytest.mark.asyncio
async def test_configure_api_custom_folder_and_watcher(app, client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    token = await register_and_login(client, email="custom-vault@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    vault = tmp_path / "My Vault"
    (vault / ".obsidian").mkdir(parents=True)
    response = await client.put("/api/obsidian-vault", headers=headers, json={
        "vault_path": str(vault), "managed_directory": "008-Polaris",
    })
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["connection"]["managed_directory"] == "008-Polaris"
    assert state["connection"]["watching"]
    async with get_sessionmaker()() as session:
        connection = await session.scalar(select(ObsidianVaultConnection))
        assert bridge._WATCHERS[connection.id].root == vault / "008-Polaris"
    assert not (vault / "Polaris").exists()
    response = await client.put("/api/obsidian-vault", headers=headers, json={
        "vault_path": str(vault), "managed_directory": "../other",
    })
    assert response.status_code == 422
    assert "OBSIDIAN_MANAGED_DIRECTORY_INVALID" in response.text
    state = (await client.get("/api/obsidian-vault", headers=headers)).json()
    assert state["connection"]["managed_directory"] == "008-Polaris"
    response = await client.put("/api/obsidian-vault", headers=headers, json={
        "vault_path": str(vault), "managed_directory": "研究笔记",
    })
    assert response.status_code == 200, response.text
    assert response.json()["connection"]["managed_directory"] == "研究笔记"
    async with get_sessionmaker()() as session:
        connection = await session.scalar(select(ObsidianVaultConnection))
        assert bridge._WATCHERS[connection.id].root == vault / "研究笔记"
    assert not (vault / "008-Polaris").exists()
    await bridge.stop_all_watchers()
