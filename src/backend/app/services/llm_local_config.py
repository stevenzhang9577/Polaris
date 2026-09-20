"""Desktop-only discovery and import of Codex / Claude Code model configs.

Only the documented user-level config files are read.  This module never reads
Codex ``auth.json`` or Claude ``~/.claude.json`` and never executes an
``auth_command``/``apiKeyHelper``.  Discovery objects retain credentials only
in process memory; API schemas are built from their redacted projections.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.llm.base import EFFORT_LEVELS, EffortLevel
from app.core.llm.router import get_llm_router, is_plugin_stage, known_stages
from app.core.security import encrypt_secret
from app.models.base import utcnow
from app.models.llm_config import LLMProviderConfig, ModelRoute
from app.schemas.llm_admin import LocalConfigPreview, RouteItem
from app.services import llm_admin

_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_ENV_REFERENCE = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")


class DesktopOnlyError(RuntimeError):
    pass


class LocalConfigNotFoundError(RuntimeError):
    pass


class LocalConfigInvalidError(RuntimeError):
    pass


class LocalConfigProbeError(RuntimeError):
    pass


@dataclass(slots=True)
class DiscoveredConfig:
    source: Literal["codex", "claude_code"]
    source_key: str
    display_name: str
    kind: Literal["openai_compat", "anthropic", "fake"]
    transport: Literal["chat_completions", "responses", "anthropic_messages", "fake"]
    auth_scheme: Literal["bearer", "x_api_key", "none"]
    endpoint_origin: str | None
    models: list[str]
    default_model: str | None
    effort: EffortLevel | None
    credential_status: Literal["available", "missing", "not_required", "unsupported"]
    importable: bool
    warnings: list[str]
    fingerprint: str
    base_url: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)

    def preview(self, existing_provider_id: uuid.UUID | None = None) -> LocalConfigPreview:
        return LocalConfigPreview(
            source=self.source,
            source_key=self.source_key,
            display_name=self.display_name,
            kind=self.kind,
            transport=self.transport,
            auth_scheme=self.auth_scheme,
            endpoint_origin=self.endpoint_origin,
            models=self.models,
            default_model=self.default_model,
            effort=self.effort,
            credential_status=self.credential_status,
            importable=self.importable,
            warnings=self.warnings,
            fingerprint=self.fingerprint,
            existing_provider_id=existing_provider_id,
        )


@dataclass(slots=True)
class DiscoveryResult:
    configs: list[DiscoveredConfig]
    errors: list[str]


@dataclass(slots=True)
class ImportResult:
    provider: LLMProviderConfig
    routes: list[ModelRoute]
    created: bool
    updated_stages: list[str]
    skipped_stages: list[str]
    latency_ms: int


def require_desktop_profile() -> None:
    if get_settings().profile != "desktop":
        raise DesktopOnlyError("LLM_CONFIG_IMPORT_DESKTOP_ONLY")


def _read_limited(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(_MAX_CONFIG_BYTES + 1)
    if len(data) > _MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds size limit")
    return data


def _codex_path(environ: Mapping[str, str]) -> Path:
    root = Path(environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return root.expanduser() / "config.toml"


def _claude_path(environ: Mapping[str, str]) -> Path:
    root = Path(environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return root.expanduser() / "settings.json"


def _endpoint(value: object) -> tuple[str | None, str | None, str | None]:
    """Return (usable URL, redacted origin, warning code)."""
    if not isinstance(value, str) or not value.strip():
        return None, None, "BASE_URL_MISSING"
    raw = value.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port  # validates malformed ports
    except ValueError:
        return None, None, "BASE_URL_INVALID"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None, None, "BASE_URL_INVALID"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return raw, urlunsplit((parsed.scheme, netloc, "", "", "")), None


def _fingerprint(metadata: Mapping[str, object]) -> str:
    encoded = json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strings(values: Sequence[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


def _effort(value: object, warnings: list[str]) -> EffortLevel | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in EFFORT_LEVELS:
        return normalized  # type: ignore[return-value]
    warnings.append("EFFORT_UNSUPPORTED")
    return None


def _resolve_setting_value(
    value: object, environ: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """Resolve ``$NAME``/``${NAME}``; return (value, referenced env name)."""
    if not isinstance(value, str) or not value:
        return None, None
    match = _ENV_REFERENCE.fullmatch(value.strip())
    if not match:
        return value, None
    name = match.group(1) or match.group(2)
    return environ.get(name), name


def parse_codex_config(path: Path, environ: Mapping[str, str]) -> list[DiscoveredConfig]:
    """Parse only Codex's user-level ``config.toml`` without touching auth state."""
    root = tomllib.loads(_read_limited(path).decode("utf-8"))
    if not isinstance(root, dict):
        raise ValueError("root must be a table")

    effective = dict(root)
    profile_name = root.get("profile")
    profiles = root.get("profiles")
    if isinstance(profile_name, str) and isinstance(profiles, dict):
        profile = profiles.get(profile_name)
        if isinstance(profile, dict):
            effective.update(profile)

    source_key = str(effective.get("model_provider") or "openai")
    if len(source_key) > 255 or any(ord(char) < 32 for char in source_key):
        raise ValueError("invalid provider identifier")
    providers = root.get("model_providers")
    raw_provider = providers.get(source_key) if isinstance(providers, dict) else None
    warnings: list[str] = []
    if not isinstance(raw_provider, dict):
        # Built-in OpenAI normally uses Codex sign-in state from auth.json.  We
        # intentionally report it but never open that file.
        model = effective.get("model") if isinstance(effective.get("model"), str) else None
        metadata = {
            "source": "codex",
            "source_key": source_key,
            "model": model,
            "transport": "responses",
            "credential": "unsupported",
        }
        return [
            DiscoveredConfig(
                source="codex",
                source_key=source_key,
                display_name="Codex · OpenAI",
                kind="openai_compat",
                transport="responses",
                auth_scheme="bearer",
                endpoint_origin="https://api.openai.com",
                models=_strings([model]),
                default_model=model,
                effort=_effort(effective.get("model_reasoning_effort"), warnings),
                credential_status="unsupported",
                importable=False,
                warnings=["CODEX_SIGN_IN_NOT_IMPORTABLE"],
                fingerprint=_fingerprint(metadata),
                base_url="https://api.openai.com/v1",
            )
        ]

    display = raw_provider.get("name")
    display_name = (
        str(display).strip() if isinstance(display, str) and display.strip() else source_key
    )
    display_name = f"Codex · {display_name}"[:255]
    base_url, origin, endpoint_warning = _endpoint(raw_provider.get("base_url"))
    if endpoint_warning:
        warnings.append(endpoint_warning)

    wire = str(raw_provider.get("wire_api") or "responses").strip().lower()
    if wire in {"responses", "response"}:
        transport: Literal["chat_completions", "responses"] = "responses"
    elif wire in {"chat", "chat_completions", "chat-completions"}:
        transport = "chat_completions"
    else:
        transport = "responses"
        warnings.append("CODEX_WIRE_API_UNSUPPORTED")

    model = effective.get("model") if isinstance(effective.get("model"), str) else None
    if not model:
        warnings.append("DEFAULT_MODEL_MISSING")

    api_key: str | None = None
    incompatible_features = False
    env_key = raw_provider.get("env_key")
    credential_status: Literal["available", "missing", "not_required", "unsupported"]
    nested_auth = raw_provider.get("auth")
    env_headers = raw_provider.get("env_http_headers")
    authorization_ref = None
    if isinstance(env_headers, dict):
        for header, reference in env_headers.items():
            if str(header).lower() == "authorization" and isinstance(reference, str):
                authorization_ref = reference
            else:
                incompatible_features = True
                warnings.append("ENV_HEADERS_NOT_IMPORTED")

    if raw_provider.get("http_headers"):
        # Static header values may contain secrets and the provider runtime does
        # not accept arbitrary headers. Do not inspect or expose them.
        incompatible_features = True
        warnings.append("STATIC_HEADERS_NOT_IMPORTED")
    if raw_provider.get("query_params") or raw_provider.get("env_query_params"):
        incompatible_features = True
        warnings.append("QUERY_PARAMS_NOT_IMPORTED")

    if isinstance(nested_auth, dict):
        # Never execute provider-defined commands. Treat the whole nested auth
        # block as opaque so an unknown future auth shape cannot be mistaken
        # for an anonymous endpoint.
        credential_status = "unsupported"
        warnings.append("NESTED_AUTH_NOT_IMPORTED")
        if nested_auth.get("command"):
            warnings.append("AUTH_COMMAND_NOT_EXECUTED")
    elif raw_provider.get("auth_command"):
        credential_status = "unsupported"
        warnings.append("AUTH_COMMAND_NOT_EXECUTED")
    elif isinstance(raw_provider.get("experimental_bearer_token"), str):
        api_key, _reference = _resolve_setting_value(
            raw_provider.get("experimental_bearer_token"), environ
        )
        if api_key and api_key.lower().startswith("bearer "):
            api_key = api_key[7:].strip()
        credential_status = "available" if api_key else "missing"
        if not api_key:
            warnings.append("CREDENTIAL_ENV_MISSING")
    elif isinstance(env_key, str) and env_key.strip():
        api_key = environ.get(env_key.strip())
        credential_status = "available" if api_key else "missing"
        if not api_key:
            warnings.append("CREDENTIAL_ENV_MISSING")
    elif authorization_ref:
        api_key = environ.get(authorization_ref)
        if api_key and api_key.lower().startswith("bearer "):
            api_key = api_key[7:].strip()
        credential_status = "available" if api_key else "missing"
        if not api_key:
            warnings.append("CREDENTIAL_ENV_MISSING")
    elif raw_provider.get("requires_openai_auth"):
        credential_status = "unsupported"
        warnings.append("CODEX_SIGN_IN_NOT_IMPORTABLE")
    else:
        credential_status = "not_required"

    effort = _effort(effective.get("model_reasoning_effort"), warnings)
    importable = bool(
        base_url
        and model
        and "CODEX_WIRE_API_UNSUPPORTED" not in warnings
        and not incompatible_features
        and credential_status in {"available", "not_required"}
    )
    metadata = {
        "source": "codex",
        "source_key": source_key,
        "display_name": display_name,
        "base_url": base_url,
        "transport": transport,
        "model": model,
        "effort": effort,
        "credential_status": credential_status,
    }
    return [
        DiscoveredConfig(
            source="codex",
            source_key=source_key,
            display_name=display_name,
            kind="openai_compat",
            transport=transport,
            auth_scheme="bearer" if credential_status != "not_required" else "none",
            endpoint_origin=origin,
            models=_strings([model]),
            default_model=model,
            effort=effort,
            credential_status=credential_status,
            importable=importable,
            warnings=warnings,
            fingerprint=_fingerprint(metadata),
            base_url=base_url,
            api_key=api_key,
        )
    ]


def parse_claude_settings(path: Path, environ: Mapping[str, str]) -> list[DiscoveredConfig]:
    """Parse Claude Code settings without opening ``~/.claude.json`` or running helpers."""
    root = json.loads(_read_limited(path))
    if not isinstance(root, dict):
        raise ValueError("root must be an object")
    settings_env = root.get("env") if isinstance(root.get("env"), dict) else {}
    warnings: list[str] = []

    def effective(name: str) -> str | None:
        if value := environ.get(name):
            return value
        value, reference = _resolve_setting_value(settings_env.get(name), environ)
        if reference and not value:
            warnings.append(f"ENV_REFERENCE_MISSING:{name}")
        return value

    raw_url = effective("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    base_url, origin, endpoint_warning = _endpoint(raw_url)
    if endpoint_warning:
        warnings.append(endpoint_warning)

    token = effective("ANTHROPIC_AUTH_TOKEN")
    if token and token.lower().startswith("bearer "):
        token = token[7:].strip()
        token, reference = _resolve_setting_value(token, environ)
        if reference and not token:
            warnings.append("ENV_REFERENCE_MISSING:ANTHROPIC_AUTH_TOKEN")
    api_key = effective("ANTHROPIC_API_KEY")
    secret = token or api_key
    auth_scheme: Literal["bearer", "x_api_key", "none"]
    if token:
        auth_scheme = "bearer"
    elif api_key:
        auth_scheme = "x_api_key"
    else:
        auth_scheme = "x_api_key"
    if secret:
        credential_status: Literal["available", "missing", "not_required", "unsupported"] = (
            "available"
        )
    elif root.get("apiKeyHelper"):
        credential_status = "unsupported"
        warnings.append("API_KEY_HELPER_NOT_EXECUTED")
    else:
        credential_status = "missing"
        warnings.append("CREDENTIAL_MISSING")

    model_map = {
        "opus": effective("ANTHROPIC_DEFAULT_OPUS_MODEL"),
        "sonnet": effective("ANTHROPIC_DEFAULT_SONNET_MODEL"),
        "haiku": effective("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        "fable": effective("ANTHROPIC_DEFAULT_FABLE_MODEL"),
    }
    default_override = effective("ANTHROPIC_DEFAULT_MODEL")
    raw_model = effective("ANTHROPIC_MODEL")
    if raw_model is None and isinstance(root.get("model"), str):
        raw_model = str(root["model"])
    raw_model = raw_model or default_override or model_map["sonnet"]

    default_model: str | None = None
    if raw_model:
        alias = raw_model.strip().lower()
        if alias in model_map:
            default_model = model_map[alias] or raw_model
            if not model_map[alias]:
                warnings.append("MODEL_ALIAS_UNRESOLVED")
        elif alias == "default":
            default_model = default_override or model_map["sonnet"] or raw_model
            if default_model == raw_model:
                warnings.append("MODEL_ALIAS_UNRESOLVED")
        elif alias == "opusplan":
            default_model = model_map["opus"] or raw_model
            warnings.append("OPUSPLAN_DYNAMIC_ROUTING_NOT_IMPORTED")
        else:
            default_model = raw_model
    else:
        warnings.append("DEFAULT_MODEL_MISSING")

    models = _strings([default_model, default_override, *model_map.values()])
    effort = _effort(effective("CLAUDE_CODE_EFFORT_LEVEL"), warnings)
    importable = bool(
        base_url and default_model and credential_status == "available"
    )
    metadata = {
        "source": "claude_code",
        "source_key": "default",
        "base_url": base_url,
        "transport": "anthropic_messages",
        "auth_scheme": auth_scheme,
        "models": models,
        "default_model": default_model,
        "effort": effort,
        "credential_status": credential_status,
    }
    return [
        DiscoveredConfig(
            source="claude_code",
            source_key="default",
            display_name="Claude Code",
            kind="anthropic",
            transport="anthropic_messages",
            auth_scheme=auth_scheme,
            endpoint_origin=origin,
            models=models,
            default_model=default_model,
            effort=effort,
            credential_status=credential_status,
            importable=importable,
            warnings=warnings,
            fingerprint=_fingerprint(metadata),
            base_url=base_url,
            api_key=secret,
        )
    ]


def discover_from_disk(environ: Mapping[str, str] | None = None) -> DiscoveryResult:
    """Read the two fixed user-level config locations and return sanitized errors."""
    require_desktop_profile()
    env = os.environ if environ is None else environ
    configs: list[DiscoveredConfig] = []
    errors: list[str] = []
    readers = (
        ("codex", _codex_path(env), parse_codex_config),
        ("claude_code", _claude_path(env), parse_claude_settings),
    )
    for source, path, parser in readers:
        if not path.is_file():
            continue
        try:
            configs.extend(parser(path, env))
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            tomllib.TOMLDecodeError,
            json.JSONDecodeError,
        ):
            # Do not include the absolute path or raw parser input in an API error.
            errors.append(f"{source}:CONFIG_INVALID")
    return DiscoveryResult(configs=configs, errors=errors)


async def previews(
    session: AsyncSession, owner_id: uuid.UUID | None
) -> tuple[list[LocalConfigPreview], list[str]]:
    discovered = discover_from_disk()
    existing = (
        await session.execute(
            select(LLMProviderConfig).where(
                llm_admin._owner_clause(LLMProviderConfig.owner_id, owner_id),
                LLMProviderConfig.import_source.is_not(None),
            )
        )
    ).scalars()
    by_source = {(row.import_source, row.import_source_key): row.id for row in existing}
    return (
        [
            config.preview(by_source.get((config.source, config.source_key)))
            for config in discovered.configs
        ],
        discovered.errors,
    )


def _validate_stages(stages: Sequence[str]) -> list[str]:
    valid = known_stages()
    normalized: list[str] = []
    for stage in stages:
        value = stage.strip()
        if not value or (value not in valid and not is_plugin_stage(value)):
            raise LocalConfigInvalidError(f"unknown stage: {stage}")
        if value in normalized:
            raise LocalConfigInvalidError(f"duplicate stage: {stage}")
        normalized.append(value)
    return normalized


def _probe_record(config: DiscoveredConfig) -> LLMProviderConfig:
    return LLMProviderConfig(
        name=config.display_name,
        kind=config.kind,
        transport=config.transport,
        auth_scheme=config.auth_scheme,
        base_url=config.base_url,
        api_key_encrypted=encrypt_secret(config.api_key) if config.api_key else None,
        enabled=True,
        models=config.models,
    )


def _redact_probe_error(error: str | None, secret: str | None) -> str:
    value = (error or "model probe failed")[:1000]
    if secret:
        value = value.replace(secret, "***")
    return value


async def _unique_name(
    session: AsyncSession, owner_id: uuid.UUID | None, desired: str
) -> str:
    names = set(
        (
            await session.execute(
                select(LLMProviderConfig.name).where(
                    llm_admin._owner_clause(LLMProviderConfig.owner_id, owner_id)
                )
            )
        ).scalars()
    )
    if desired not in names:
        return desired
    for suffix in range(2, 1000):
        candidate = f"{desired[:245]} ({suffix})"
        if candidate not in names:
            return candidate
    raise LocalConfigInvalidError("could not allocate provider name")


async def import_local_config(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID | None,
    source: str,
    source_key: str,
    stages: Sequence[str],
    overwrite_routes: bool,
) -> ImportResult:
    """Probe, then atomically upsert one provider and only selected routes."""
    require_desktop_profile()
    normalized_stages = _validate_stages(stages)
    discovery = discover_from_disk()
    config = next(
        (
            item
            for item in discovery.configs
            if item.source == source and item.source_key == source_key
        ),
        None,
    )
    if config is None:
        raise LocalConfigNotFoundError("LLM_LOCAL_CONFIG_NOT_FOUND")
    if not config.importable or not config.default_model:
        raise LocalConfigInvalidError("LLM_LOCAL_CONFIG_NOT_IMPORTABLE")

    probe_record = _probe_record(config)
    capability = "agent_tools" if "agent" in normalized_stages else "chat"
    ok, latency_ms, error = await llm_admin.test_model(
        probe_record, config.default_model, capability
    )
    if not ok:
        await session.rollback()
        raise LocalConfigProbeError(_redact_probe_error(error, config.api_key))

    provider = await session.scalar(
        select(LLMProviderConfig).where(
            llm_admin._owner_clause(LLMProviderConfig.owner_id, owner_id),
            LLMProviderConfig.import_source == config.source,
            LLMProviderConfig.import_source_key == config.source_key,
        )
    )
    created = provider is None
    if provider is None:
        provider = LLMProviderConfig(
            owner_id=owner_id,
            name=await _unique_name(session, owner_id, config.display_name),
        )
        session.add(provider)
    provider.kind = config.kind
    provider.transport = config.transport
    provider.auth_scheme = config.auth_scheme
    provider.base_url = config.base_url
    provider.api_key_encrypted = encrypt_secret(config.api_key) if config.api_key else None
    provider.enabled = True
    provider.models = config.models
    provider.import_source = config.source
    provider.import_source_key = config.source_key
    provider.import_fingerprint = config.fingerprint
    provider.imported_at = utcnow()
    await session.flush()

    existing_routes = {
        row.stage: row
        for row in (
            await session.execute(
                select(ModelRoute).where(
                    llm_admin._owner_clause(ModelRoute.owner_id, owner_id),
                    ModelRoute.stage.in_(normalized_stages),
                )
            )
        ).scalars()
    }
    updated_stages: list[str] = []
    skipped_stages: list[str] = []
    selected_routes: list[ModelRoute] = []
    for stage in normalized_stages:
        route = existing_routes.get(stage)
        if route is not None and not overwrite_routes:
            skipped_stages.append(stage)
            selected_routes.append(route)
            continue
        if route is None:
            route = ModelRoute(owner_id=owner_id, stage=stage)
            session.add(route)
        route.provider_id = provider.id
        route.model = config.default_model
        route.temperature = None
        route.effort = config.effort
        route.context_window = None
        updated_stages.append(stage)
        selected_routes.append(route)

    try:
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    await session.refresh(provider)
    for route in selected_routes:
        await session.refresh(route)
    get_llm_router().invalidate_cache()
    return ImportResult(
        provider=provider,
        routes=selected_routes,
        created=created,
        updated_stages=updated_stages,
        skipped_stages=skipped_stages,
        latency_ms=latency_ms,
    )


def route_items(routes: Sequence[ModelRoute]) -> list[RouteItem]:
    return [RouteItem.model_validate(route, from_attributes=True) for route in routes]
