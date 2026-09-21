"""OpenAI Responses API provider.

This adapter is intentionally separate from :mod:`openai_compat`: the wire
formats for messages, tool calls and streaming events are different enough
that pretending Responses is Chat Completions makes multi-turn tool use fail in
subtle ways.  Codex local providers currently declare this transport.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.llm.base import (
    CompletionResult,
    EffortLevel,
    ImageBlock,
    LLMProvider,
    Message,
    OpaqueProviderState,
    OpaqueProviderStateBlock,
    StreamDone,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolsUnsupportedError,
    ToolUseArgsDelta,
    ToolUseBlock,
    ToolUseStart,
    ToolUseStop,
    normalize_finish_reason,
)
from app.core.llm.usage import normalize_openai_responses_usage

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_TOOLS_REJECT_MARKERS = (
    "tools is not supported",
    "tool_choice",
    "does not support tools",
    "function calling",
    "unsupported parameter: 'tools'",
)


def _tools_unsupported(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _TOOLS_REJECT_MARKERS)


def _responses_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/responses"
    return f"{normalized}/v1/responses"


def _usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Backward-compatible local name for the shared usage normalizer."""
    return normalize_openai_responses_usage(raw)


def _message_content(message: Message) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            if block.text:
                parts.append({"type": "input_text", "text": block.text})
        elif isinstance(block, ImageBlock):
            encoded = base64.b64encode(block.data).decode("ascii")
            parts.append(
                {"type": "input_image", "image_url": f"data:{block.mime};base64,{encoded}"}
            )
        # Thinking blocks are presentation-neutral summaries from other
        # providers. Tool/provider-state blocks are emitted as top-level items.
    return parts


def _input_payload(messages: Sequence[Message], images: list[bytes] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    last_user_index: int | None = None
    for message in messages:
        for block in message.blocks:
            if (
                isinstance(block, OpaqueProviderStateBlock)
                and block.provider == "openai_responses"
                and block.payload.get("type") == "reasoning"
            ):
                # Copy so caller-owned history cannot be mutated by payload assembly.
                items.append(dict(block.payload))
        parts = _message_content(message)
        tool_uses = [block for block in message.blocks if isinstance(block, ToolUseBlock)]
        tool_results = [block for block in message.blocks if isinstance(block, ToolResultBlock)]
        if parts:
            items.append({"role": message.role, "content": parts})
            if message.role == "user":
                last_user_index = len(items) - 1
        for call in tool_uses:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.input, ensure_ascii=False),
                }
            )
        for result in tool_results:
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": result.tool_use_id,
                    "output": result.content,
                }
            )
            if result.images:
                image_parts = [
                    {"type": "input_text", "text": "以下是该工具调用返回的图片。"}
                ]
                for image in result.images:
                    encoded = base64.b64encode(image.data).decode("ascii")
                    image_parts.append(
                        {
                            "type": "input_image",
                            "image_url": f"data:{image.mime};base64,{encoded}",
                        }
                    )
                items.append({"role": "user", "content": image_parts})
                last_user_index = len(items) - 1

    if images:
        if last_user_index is None:
            items.append({"role": "user", "content": []})
            last_user_index = len(items) - 1
        content = items[last_user_index]["content"]
        for image in images:
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"}
            )
    return items


def _response_blocks(data: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    texts: list[str] = []
    blocks: list[Any] = []
    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    value = str(part.get("text") or "")
                    texts.append(value)
                    blocks.append(TextBlock(value))
        elif kind == "function_call":
            raw = item.get("arguments") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                arguments = {"__parse_error__": str(raw)[:2000]}
            call_id = str(item.get("call_id") or item.get("id") or "")
            blocks.append(ToolUseBlock(call_id, str(item.get("name") or ""), arguments))
        elif kind == "reasoning":
            # Keep the complete opaque item (not its summary text) for the next
            # function-call round. It is never flattened into logs or UI data.
            blocks.append(OpaqueProviderStateBlock("openai_responses", dict(item)))
    return "".join(texts), tuple(blocks)


class OpenAIResponsesProvider(LLMProvider):
    """Responses transport with text, images, reasoning and function tools."""

    name = "openai_responses"
    supports_tools = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        auth_scheme: str = "bearer",
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        max_attempts: int = 4,
    ) -> None:
        self._api_url = _responses_url(base_url)
        self._api_key = api_key
        self._auth_scheme = auth_scheme
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._max_attempts = max_attempts

    def _headers(self) -> dict[str, str]:
        if self._auth_scheme == "none" or not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def _payload(
        messages: Sequence[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        *,
        stream: bool,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": _input_payload(messages, images),
            "stream": stream,
            # Stateless function calling with reasoning models requires this
            # opaque item to be replayed alongside function_call_output.
            "include": ["reasoning.encrypted_content"],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens
        if effort is not None:
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        return payload

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(
                    self._api_url, headers=self._headers(), json=payload
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                break
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_attempts - 1:
                await asyncio.sleep(2**attempt)
                continue
            return response
        raise RuntimeError(
            f"responses request failed after {self._max_attempts} attempts: {last_error}"
        )

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> CompletionResult:
        response = await self._post(
            self._payload(
                messages,
                model,
                temperature,
                max_tokens,
                stream=False,
                images=images,
                effort=effort,
                tools=tools,
                tool_choice=tool_choice,
            )
        )
        if response.status_code >= 400:
            body = response.text[:500]
            if tools and _tools_unsupported(body):
                raise ToolsUnsupportedError(body)
            raise RuntimeError(
                f"openai responses {response.status_code} from provider: {body}"
            )
        data = response.json()
        text, blocks = _response_blocks(data)
        has_tool = any(isinstance(block, ToolUseBlock) for block in blocks)
        incomplete = (data.get("incomplete_details") or {}).get("reason")
        finish = "tool_use" if has_tool else normalize_finish_reason(incomplete or "stop")
        return CompletionResult(
            content=text,
            model=str(data.get("model") or model),
            finish_reason=finish,
            usage=_usage(data.get("usage")),
            blocks=blocks,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_events(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            effort=effort,
        ):
            if isinstance(event, TextDelta):
                yield event.text

    async def stream_events(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._payload(
            messages,
            model,
            temperature,
            max_tokens,
            stream=True,
            images=images,
            effort=effort,
            tools=tools,
            tool_choice=tool_choice,
        )
        open_tools: set[int] = set()
        saw_tool = False
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        async with self._client.stream(
            "POST", self._api_url, headers=self._headers(), json=payload
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")[:500]
                if tools and _tools_unsupported(body):
                    raise ToolsUnsupportedError(body)
                raise RuntimeError(
                    f"openai responses {response.status_code} from provider: {body}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:") :].strip()
                if not raw or raw == "[DONE]":
                    continue
                event = json.loads(raw)
                kind = event.get("type")
                index = int(event.get("output_index", 0) or 0)
                if kind == "response.output_text.delta":
                    yield TextDelta(str(event.get("delta") or ""))
                elif kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        saw_tool = True
                        open_tools.add(index)
                        yield ToolUseStart(
                            index,
                            str(item.get("call_id") or item.get("id") or ""),
                            str(item.get("name") or ""),
                        )
                elif kind == "response.function_call_arguments.delta":
                    yield ToolUseArgsDelta(index, str(event.get("delta") or ""))
                elif kind == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "reasoning":
                        yield OpaqueProviderState("openai_responses", dict(item))
                    elif index in open_tools:
                        open_tools.remove(index)
                        yield ToolUseStop(index)
                elif kind in {"response.completed", "response.incomplete"}:
                    terminal = event.get("response") or {}
                    usage = _usage(terminal.get("usage"))
                    finish_reason = (terminal.get("incomplete_details") or {}).get("reason")
                elif kind in {"response.failed", "error"}:
                    error = event.get("error") or (event.get("response") or {}).get("error") or {}
                    raise RuntimeError(f"openai responses stream failed: {str(error)[:500]}")
        for index in sorted(open_tools):
            yield ToolUseStop(index)
        normalized_finish = "tool_use" if saw_tool else normalize_finish_reason(
            finish_reason or "stop"
        )
        yield StreamDone(finish_reason=normalized_finish, usage=usage)

    async def aclose(self) -> None:
        await self._client.aclose()
