import json

import httpx
import pytest
import respx

from app.core.llm.base import (
    Message,
    OpaqueProviderState,
    OpaqueProviderStateBlock,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseArgsDelta,
    ToolUseBlock,
    ToolUseStart,
)
from app.core.llm.openai_responses import OpenAIResponsesProvider, _responses_url
from app.core.llm.tool_stream import ToolCallAccumulator
from app.services.conversations import blocks_to_json


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://api.openai.com", "https://api.openai.com/v1/responses"),
        ("http://relay.test/v1", "http://relay.test/v1/responses"),
        ("http://relay.test/responses", "http://relay.test/responses"),
    ],
)
def test_responses_url_normalization(base, expected):
    assert _responses_url(base) == expected


@respx.mock
async def test_responses_complete_preserves_function_call_chain():
    route = respx.post("http://relay.test/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "encrypted_content": "opaque-ciphertext",
                        "summary": [],
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_next",
                        "name": "lookup",
                        "arguments": '{"q":"paper"}',
                    },
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )
    )
    provider = OpenAIResponsesProvider("http://relay.test/v1", "secret")
    try:
        result = await provider.complete(
            [
                Message(
                    role="assistant",
                    content=[ToolUseBlock("call_prev", "read", {"id": "p1"})],
                ),
                Message(
                    role="user",
                    content=[ToolResultBlock("call_prev", '{"title":"T"}')],
                ),
            ],
            model="gpt-test",
            tools=[{"name": "lookup", "parameters": {"type": "object"}}],
        )
    finally:
        await provider.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert payload["input"] == [
        {
            "type": "function_call",
            "call_id": "call_prev",
            "name": "read",
            "arguments": '{"id": "p1"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_prev",
            "output": '{"title":"T"}',
        },
    ]
    assert route.calls.last.request.headers["authorization"] == "Bearer secret"
    assert result.content == "done"
    assert result.finish_reason == "tool_use"
    assert result.tool_calls[0].id == "call_next"
    assert result.tool_calls[0].input == {"q": "paper"}
    assert result.usage["prompt_tokens"] == 3
    state = next(block for block in result.blocks if isinstance(block, OpaqueProviderStateBlock))
    assert state.payload["encrypted_content"] == "opaque-ciphertext"
    replay = provider._payload(  # type: ignore[attr-defined]
        [
            Message(role="assistant", content=list(result.blocks)),
            Message(role="user", content=[ToolResultBlock("call_next", "result")]),
        ],
        "gpt-test",
        None,
        None,
        stream=False,
    )
    reasoning_index = next(
        index for index, item in enumerate(replay["input"]) if item.get("type") == "reasoning"
    )
    call_index = next(
        index
        for index, item in enumerate(replay["input"])
        if item.get("type") == "function_call"
    )
    output_index = next(
        index
        for index, item in enumerate(replay["input"])
        if item.get("type") == "function_call_output"
    )
    assert reasoning_index < call_index < output_index
    assert replay["include"] == ["reasoning.encrypted_content"]
    # Opaque state is not serialized into conversation API storage.
    assert blocks_to_json([state, TextBlock("visible")]) == [
        {"kind": "text", "text": "visible"}
    ]


@respx.mock
async def test_responses_stream_maps_text_and_tool_events():
    body = "\n".join(
        [
            'data: {"type":"response.output_text.delta","delta":"hi"}',
            (
                'data: {"type":"response.output_item.done","output_index":0,'
                '"item":{"type":"reasoning","id":"rs_1",'
                '"encrypted_content":"opaque-ciphertext","summary":[]}}'
            ),
            (
                'data: {"type":"response.output_item.added","output_index":1,'
                '"item":{"type":"function_call","call_id":"call_1","name":"search"}}'
            ),
            (
                'data: {"type":"response.function_call_arguments.delta",'
                '"output_index":1,"delta":"{\\"q\\":"}'
            ),
            (
                'data: {"type":"response.output_item.done","output_index":1,'
                '"item":{"type":"function_call"}}'
            ),
            (
                'data: {"type":"response.completed","response":'
                '{"usage":{"input_tokens":2,"output_tokens":1}}}'
            ),
            "data: [DONE]",
            "",
        ]
    )
    respx.post("http://relay.test/v1/responses").mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    provider = OpenAIResponsesProvider("http://relay.test/v1", "", auth_scheme="none")
    try:
        events = [
            event
            async for event in provider.stream_events(
                [Message(role="user", content="ping")],
                model="gpt-test",
                tools=[{"name": "search", "parameters": {"type": "object"}}],
            )
        ]
    finally:
        await provider.aclose()

    assert any(isinstance(event, TextDelta) and event.text == "hi" for event in events)
    assert any(
        isinstance(event, ToolUseStart) and event.id == "call_1" for event in events
    )
    state_event = next(event for event in events if isinstance(event, OpaqueProviderState))
    accumulator = ToolCallAccumulator()
    for event in events:
        accumulator.feed(event)
    state_block = next(
        block for block in accumulator.finish() if isinstance(block, OpaqueProviderStateBlock)
    )
    assert state_event.payload["encrypted_content"] == "opaque-ciphertext"
    assert state_block.payload == state_event.payload
    assert any(isinstance(event, ToolUseArgsDelta) for event in events)
    assert events[-1].finish_reason == "tool_use"  # type: ignore[union-attr]
    assert events[-1].usage["prompt_tokens"] == 2  # type: ignore[union-attr]
