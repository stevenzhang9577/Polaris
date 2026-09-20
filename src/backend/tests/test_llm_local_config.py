import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.llm.base import StreamDone, TextDelta, ToolUseArgsDelta, ToolUseStart, ToolUseStop
from app.core.llm.fake import FakeProvider
from app.models.llm_config import LLMProviderConfig, ModelRoute
from app.services import llm_admin, llm_local_config
from tests.conftest import register_and_login


def test_parse_codex_config_uses_env_reference_without_exposing_secret(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
model = "codex-test-model"
model_provider = "relay"
model_reasoning_effort = "high"

[model_providers.relay]
name = "Local Relay"
base_url = "http://127.0.0.1:9123/v1"
env_key = "RELAY_TOKEN"
wire_api = "responses"
""",
        encoding="utf-8",
    )
    discovered = llm_local_config.parse_codex_config(
        config, {"RELAY_TOKEN": "super-secret-token"}
    )[0]

    assert discovered.transport == "responses"
    assert discovered.default_model == "codex-test-model"
    assert discovered.effort == "high"
    assert discovered.credential_status == "available"
    assert discovered.importable is True
    assert discovered.api_key == "super-secret-token"
    preview = discovered.preview().model_dump_json()
    assert "super-secret-token" not in preview
    assert preview.count("127.0.0.1") == 1
    assert "/v1" not in preview  # only origin is returned


def test_parse_codex_builtin_never_reads_auth_json(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "codex-test-model"\n', encoding="utf-8")
    (tmp_path / "auth.json").write_text("not valid json and must not be read", encoding="utf-8")

    discovered = llm_local_config.parse_codex_config(config, {})[0]

    assert discovered.credential_status == "unsupported"
    assert discovered.importable is False
    assert "CODEX_SIGN_IN_NOT_IMPORTABLE" in discovered.warnings


def test_parse_codex_experimental_token_and_rejects_unrepresentable_options(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
model = "gpt-test"
model_provider = "relay"

[model_providers.relay]
base_url = "https://relay.test/v1"
wire_api = "responses"
experimental_bearer_token = "$RELAY_TOKEN"
query_params = { tenant = "private" }
""",
        encoding="utf-8",
    )
    discovered = llm_local_config.parse_codex_config(config, {"RELAY_TOKEN": "secret"})[0]
    assert discovered.api_key == "secret"
    assert discovered.credential_status == "available"
    assert discovered.importable is False
    assert "QUERY_PARAMS_NOT_IMPORTED" in discovered.warnings


def test_parse_codex_never_executes_nested_auth_command(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
model = "gpt-test"
model_provider = "relay"

[model_providers.relay]
base_url = "https://relay.test/v1"
wire_api = "responses"

[model_providers.relay.auth]
command = ["sh", "-c", "touch /tmp/must-not-run"]
""",
        encoding="utf-8",
    )
    discovered = llm_local_config.parse_codex_config(config, {})[0]
    assert discovered.credential_status == "unsupported"
    assert discovered.importable is False
    assert "NESTED_AUTH_NOT_IMPORTED" in discovered.warnings
    assert "AUTH_COMMAND_NOT_EXECUTED" in discovered.warnings


def test_parse_claude_settings_resolves_alias_and_bearer(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        """
{
  "model": "fable",
  "env": {
    "ANTHROPIC_BASE_URL": "https://gateway.example.test/anthropic/v1",
    "ANTHROPIC_AUTH_TOKEN": "Bearer $CLAUDE_GATEWAY_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-custom",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-custom",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-custom"
  }
}
""",
        encoding="utf-8",
    )
    discovered = llm_local_config.parse_claude_settings(
        settings, {"CLAUDE_GATEWAY_TOKEN": "bearer-secret"}
    )[0]

    assert discovered.default_model == "claude-fable-custom"
    assert discovered.models == [
        "claude-fable-custom",
        "claude-opus-custom",
        "claude-sonnet-custom",
    ]
    assert discovered.auth_scheme == "bearer"
    assert discovered.api_key == "bearer-secret"
    assert discovered.importable is True
    assert "bearer-secret" not in discovered.preview().model_dump_json()


async def test_local_config_endpoints_are_desktop_only(client):
    token = await register_and_login(client)
    response = await client.get(
        "/api/admin/llm/local-configs", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "LLM_CONFIG_IMPORT_DESKTOP_ONLY"


async def test_agent_probe_requires_an_actual_tool_call():
    class NoToolProvider(FakeProvider):
        async def stream_events(self, messages, **kwargs):  # noqa: ANN001, ANN003
            yield TextDelta("pong")
            yield StreamDone(finish_reason="stop")

    class ToolProvider(FakeProvider):
        async def stream_events(self, messages, **kwargs):  # noqa: ANN001, ANN003
            yield ToolUseStart(0, "call_probe", "polaris_probe")
            yield ToolUseArgsDelta(0, "{}")
            yield ToolUseStop(0)
            yield StreamDone(finish_reason="tool_use")

    ok, _latency, error = await llm_admin.probe_model(
        NoToolProvider(), "fake-default", "agent_tools"
    )
    assert ok is False
    assert "required tool call" in (error or "")
    ok, _latency, error = await llm_admin.probe_model(
        ToolProvider(), "fake-default", "agent_tools"
    )
    assert ok is True
    assert error is None


async def test_import_probes_then_merges_only_selected_routes(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    imported = llm_local_config.DiscoveredConfig(
        source="codex",
        source_key="relay",
        display_name="Codex · Relay",
        kind="openai_compat",
        transport="responses",
        auth_scheme="bearer",
        endpoint_origin="http://127.0.0.1:9123",
        models=["gpt-test"],
        default_model="gpt-test",
        effort="high",
        credential_status="available",
        importable=True,
        warnings=[],
        fingerprint="a" * 64,
        base_url="http://127.0.0.1:9123/v1",
        api_key="secret-not-in-response",
    )
    monkeypatch.setattr(
        llm_local_config,
        "discover_from_disk",
        lambda environ=None: llm_local_config.DiscoveryResult([imported], []),
    )

    probe_capabilities: list[str] = []

    async def successful_probe(provider, model, capability):  # noqa: ANN001
        assert provider.transport == "responses"
        assert model == "gpt-test"
        probe_capabilities.append(capability)
        return True, 7, None

    monkeypatch.setattr(llm_local_config.llm_admin, "test_model", successful_probe)

    existing = await client.post(
        "/api/admin/llm/providers",
        json={"name": "manual", "kind": "fake"},
        headers=headers,
    )
    manual_id = existing.json()["id"]
    await client.put(
        "/api/admin/llm/routes",
        json=[
            {"stage": "default", "provider_id": manual_id, "model": "manual-model"},
            {"stage": "embedding", "provider_id": manual_id, "model": "manual-embed"},
        ],
        headers=headers,
    )

    response = await client.post(
        "/api/admin/llm/local-configs/import",
        json={
            "source": "codex",
            "source_key": "relay",
            "stages": ["default", "agent"],
            "overwrite_routes": False,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"]["transport"] == "responses"
    assert body["provider"]["auth_scheme"] == "bearer"
    assert body["provider"]["import_source"] == "codex"
    assert body["updated_stages"] == ["agent"]
    assert body["skipped_stages"] == ["default"]
    assert probe_capabilities == ["agent_tools"]
    assert "secret-not-in-response" not in response.text

    routes = {
        route["stage"]: route
        for route in (await client.get("/api/admin/llm/routes", headers=headers)).json()
    }
    assert routes["default"]["model"] == "manual-model"
    assert routes["embedding"]["model"] == "manual-embed"
    assert routes["agent"]["model"] == "gpt-test"
    assert routes["agent"]["effort"] == "high"

    # A second import is idempotent and can explicitly overwrite only default;
    # the unrelated embedding and agent routes remain untouched.
    response = await client.post(
        "/api/admin/llm/local-configs/import",
        json={
            "source": "codex",
            "source_key": "relay",
            "stages": ["default"],
            "overwrite_routes": True,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert probe_capabilities == ["agent_tools", "chat"]
    assert response.json()["created"] is False
    routes = {
        route["stage"]: route
        for route in (await client.get("/api/admin/llm/routes", headers=headers)).json()
    }
    assert routes["default"]["model"] == "gpt-test"
    assert routes["embedding"]["model"] == "manual-embed"
    assert routes["agent"]["model"] == "gpt-test"

    async with get_sessionmaker()() as session:
        providers = (
            await session.execute(
                select(LLMProviderConfig).where(LLMProviderConfig.import_source == "codex")
            )
        ).scalars().all()
        assert len(providers) == 1


async def test_import_connection_without_routes(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    imported = llm_local_config.DiscoveredConfig(
        source="codex",
        source_key="relay",
        display_name="Codex · Relay",
        kind="openai_compat",
        transport="responses",
        auth_scheme="none",
        endpoint_origin="http://127.0.0.1:9123",
        models=["gpt-test"],
        default_model="gpt-test",
        effort=None,
        credential_status="not_required",
        importable=True,
        warnings=[],
        fingerprint="c" * 64,
        base_url="http://127.0.0.1:9123/v1",
    )
    monkeypatch.setattr(
        llm_local_config,
        "discover_from_disk",
        lambda environ=None: llm_local_config.DiscoveryResult([imported], []),
    )

    async def successful_probe(provider, model, capability):  # noqa: ANN001
        assert capability == "chat"
        return True, 4, None

    monkeypatch.setattr(llm_local_config.llm_admin, "test_model", successful_probe)
    response = await client.post(
        "/api/admin/llm/local-configs/import",
        json={"source": "codex", "source_key": "relay", "stages": []},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["routes"] == []
    assert response.json()["updated_stages"] == []
    assert (await client.get("/api/admin/llm/routes", headers=headers)).json() == []


async def test_failed_probe_writes_nothing(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    imported = llm_local_config.DiscoveredConfig(
        source="claude_code",
        source_key="default",
        display_name="Claude Code",
        kind="anthropic",
        transport="anthropic_messages",
        auth_scheme="bearer",
        endpoint_origin="https://gateway.test",
        models=["claude-test"],
        default_model="claude-test",
        effort=None,
        credential_status="available",
        importable=True,
        warnings=[],
        fingerprint="b" * 64,
        base_url="https://gateway.test/v1",
        api_key="must-be-redacted",
    )
    monkeypatch.setattr(
        llm_local_config,
        "discover_from_disk",
        lambda environ=None: llm_local_config.DiscoveryResult([imported], []),
    )

    async def failed_probe(provider, model, capability):  # noqa: ANN001
        return False, 3, "401 token=must-be-redacted"

    monkeypatch.setattr(llm_local_config.llm_admin, "test_model", failed_probe)
    response = await client.post(
        "/api/admin/llm/local-configs/import",
        json={"source": "claude_code", "source_key": "default", "stages": ["default"]},
        headers=headers,
    )
    assert response.status_code == 422
    assert "must-be-redacted" not in response.text

    async with get_sessionmaker()() as session:
        assert (
            await session.scalar(
                select(LLMProviderConfig).where(
                    LLMProviderConfig.import_source == "claude_code"
                )
            )
            is None
        )
        assert (await session.execute(select(ModelRoute))).scalars().all() == []


async def test_import_rolls_back_when_commit_fails(monkeypatch):
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    imported = llm_local_config.DiscoveredConfig(
        source="codex",
        source_key="commit-failure",
        display_name="Codex · Commit failure",
        kind="openai_compat",
        transport="responses",
        auth_scheme="none",
        endpoint_origin="http://127.0.0.1:9123",
        models=["gpt-test"],
        default_model="gpt-test",
        effort=None,
        credential_status="not_required",
        importable=True,
        warnings=[],
        fingerprint="d" * 64,
        base_url="http://127.0.0.1:9123/v1",
    )
    monkeypatch.setattr(
        llm_local_config,
        "discover_from_disk",
        lambda environ=None: llm_local_config.DiscoveryResult([imported], []),
    )

    async def successful_probe(provider, model, capability):  # noqa: ANN001
        return True, 1, None

    monkeypatch.setattr(llm_local_config.llm_admin, "test_model", successful_probe)
    async with get_sessionmaker()() as session:
        real_rollback = session.rollback
        rolled_back = False

        async def failed_commit():
            raise RuntimeError("simulated commit failure")

        async def tracked_rollback():
            nonlocal rolled_back
            rolled_back = True
            await real_rollback()

        monkeypatch.setattr(session, "commit", failed_commit)
        monkeypatch.setattr(session, "rollback", tracked_rollback)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await llm_local_config.import_local_config(
                session,
                owner_id=None,
                source="codex",
                source_key="commit-failure",
                stages=[],
                overwrite_routes=False,
            )
        assert rolled_back is True

    async with get_sessionmaker()() as session:
        assert (
            await session.scalar(
                select(LLMProviderConfig).where(
                    LLMProviderConfig.import_source_key == "commit-failure"
                )
            )
            is None
        )
