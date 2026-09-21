"""管理端 LLM 配置 schema（docs/task-system.md §7（原 api-m1.md §2））。"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.llm.base import EffortLevel

ProviderKind = Literal["openai_compat", "anthropic", "fake"]
ProviderTransport = Literal["chat_completions", "responses", "anthropic_messages", "fake"]
ProviderAuthScheme = Literal["bearer", "x_api_key", "none"]
UserAgent = Annotated[str, Field(max_length=255, pattern=r"^[^\r\n]*$")]
ModelPrice = Annotated[Decimal, Field(ge=0, le=1000000, max_digits=16, decimal_places=8)]
ModelId = Annotated[str, Field(min_length=1, max_length=255, pattern=r".*\S.*")]


class ModelPricing(BaseModel):
    """USD per million tokens. Missing cache rates are unknown, not free."""

    model_config = ConfigDict(extra="forbid")
    input_per_million: ModelPrice
    output_per_million: ModelPrice
    cache_read_per_million: ModelPrice | None = None
    cache_creation_per_million: ModelPrice | None = None


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: ProviderKind
    transport: ProviderTransport | None = None
    auth_scheme: ProviderAuthScheme | None = None
    base_url: str | None = None
    user_agent: UserAgent | None = None
    api_key: str | None = None  # 只写不读；入库前 Fernet 加密
    enabled: bool = True
    models: list[str] | None = None  # 可用模型 id 列表（None = 未配置）
    model_pricing: dict[ModelId, ModelPricing] | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    kind: ProviderKind | None = None
    transport: ProviderTransport | None = None
    auth_scheme: ProviderAuthScheme | None = None
    base_url: str | None = None
    user_agent: UserAgent | None = None  # 空字符串 = 恢复 HTTP 客户端默认值
    api_key: str | None = None  # 空字符串 = 不变
    enabled: bool | None = None
    models: list[str] | None = None  # 整体替换；None = 不变（清空传 []）
    model_pricing: dict[ModelId, ModelPricing] | None = None  # supplied null clears prices


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    transport: str
    auth_scheme: str
    base_url: str | None
    user_agent: str | None
    api_key_masked: str
    enabled: bool
    models: list[str] | None = None
    model_pricing: dict[str, ModelPricing] | None = None
    import_source: str | None = None
    import_source_key: str | None = None
    import_fingerprint: str | None = None
    imported_at: datetime | None = None


class RouteItem(BaseModel):
    stage: str
    provider_id: uuid.UUID
    model: str = Field(min_length=1, max_length=255)
    temperature: float | None = None  # None = 用 provider 默认
    context_window: int | None = Field(default=None, ge=1)
    # 推理档位；None = 不发送该参数（用模型默认）。某个模型具体支持哪几档由服务端校验，
    # 这里只挡明显非法的取值。
    effort: EffortLevel | None = None


LocalConfigSource = Literal["codex", "claude_code"]
CredentialStatus = Literal["available", "missing", "not_required", "unsupported"]


class LocalConfigPreview(BaseModel):
    """A deliberately redacted view of one user-level local model config."""

    source: LocalConfigSource
    source_key: str
    display_name: str
    kind: ProviderKind
    transport: ProviderTransport
    auth_scheme: ProviderAuthScheme
    endpoint_origin: str | None = None
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    effort: EffortLevel | None = None
    credential_status: CredentialStatus
    importable: bool
    warnings: list[str] = Field(default_factory=list)
    fingerprint: str
    existing_provider_id: uuid.UUID | None = None


class LocalConfigDiscovery(BaseModel):
    configs: list[LocalConfigPreview] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class LocalConfigImportRequest(BaseModel):
    source: LocalConfigSource
    source_key: str = Field(min_length=1, max_length=255)
    # Empty means "import/update the provider connection only".
    stages: list[str] = Field(default_factory=list)
    overwrite_routes: bool = False


class LocalConfigImportResult(BaseModel):
    provider: ProviderRead
    routes: list[RouteItem]
    created: bool
    updated_stages: list[str]
    skipped_stages: list[str]
    probe: "TestModelResult"


TestCapability = Literal["chat", "embedding", "rerank"]


class TestModelRequest(BaseModel):
    """模型连通性测试：按 provider 直连探测（不经过路由表，不记账、不写调用日志）。"""

    provider_id: uuid.UUID
    model: str = Field(min_length=1, max_length=255)
    capability: TestCapability = "chat"


class TestModelResult(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None = None


class UsageRow(BaseModel):
    requested_model: str | None = None
    pricing_model: str | None = None
    response_error: str | None = None
    id: str | None = None
    occurred_at: datetime | None = None
    reference_cost_usd: Decimal | None = None
    date: str
    stage: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    calls: int
    provider_name: str | None = None
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_reported_calls: int = 0
    estimated_calls: int = 0
    priced_calls: int = 0
    cost_usd: Decimal | None = None


class UsageCallPage(BaseModel):
    total: int
    items: list[UsageRow]


# ---- 调用日志 ----


class CallLogSettings(BaseModel):
    """调用日志开关（系统级，默认关）。"""

    enabled: bool


class CallLogRow(BaseModel):
    """列表行：request/response 只给截断预览，全文走详情端点。"""

    id: uuid.UUID
    created_at: datetime
    stage: str
    provider_name: str
    model: str
    duration_ms: int
    status: str  # ok|error
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    voyage_id: uuid.UUID | None
    request_preview: str
    response_preview: str


class CallLogPage(BaseModel):
    total: int
    items: list[CallLogRow]


class CallLogDetail(BaseModel):
    id: uuid.UUID
    created_at: datetime
    stage: str
    provider_name: str
    model: str
    duration_ms: int
    status: str
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    voyage_id: uuid.UUID | None
    request: Any | None  # {"messages": [{role, content}], "images": ["[image ~N KB]"]} 或摘要
    response: str | None
