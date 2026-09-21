"""A K3 request must not silently accept a GLM OpenAI payload as Anthropic."""
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import CompletionResult, Message, ProviderProtocolError
from app.core.llm.router import LLMRouter, ResolvedRoute
from app.models.llm_config import LLMUsage
from app.models.paper import Paper, PaperWikiRevision
from app.services import llm_admin, paper_summaries


def wrong_protocol(request):
    return httpx.Response(200, json={
        "model": "glm-5.3-flash", "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 47,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    })


@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_rejects_openai_payload_with_real_usage(stream):
    provider = AnthropicProvider("test", client=httpx.AsyncClient(
        transport=httpx.MockTransport(wrong_protocol)))
    try:
        with pytest.raises(ProviderProtocolError) as caught:
            if stream:
                events = provider.stream_events([Message("user", "ping")], model="kimi-k3[1M]")
                _ = [e async for e in events]
            else:
                await provider.complete([Message("user", "ping")], model="kimi-k3[1M]")
        assert caught.value.response_model == "glm-5.3-flash"
        assert caught.value.requested_model == "kimi-k3[1M]"
        assert caught.value.usage["completion_tokens"] == 47
    finally:
        await provider.aclose()


async def test_protocol_failure_records_requested_returned_and_usage_without_k3_price(app):
    provider = AnthropicProvider("test", client=httpx.AsyncClient(
        transport=httpx.MockTransport(wrong_protocol)))
    router = LLMRouter()
    route = ResolvedRoute("anthropic", None, "", "kimi-k3[1M]", None,
                          provider_name="Claude Code", pricing={"input_per_million": "3"})
    router.resolve = AsyncMock(return_value=(provider, route))
    try:
        with pytest.raises(ProviderProtocolError):
            await router.complete("librarian", [Message("user", "ping")])
        async with get_sessionmaker()() as session:
            call = await session.scalar(select(LLMUsage))
            assert call.model == "glm-5.3-flash"
            assert call.pricing_snapshot["model"] == "kimi-k3[1M]"
            assert call.pricing_snapshot["response_error"] == "LLM_PROVIDER_PROTOCOL_MISMATCH"
            assert call.cost_usd is None
            assert call.prompt_tokens == 17 and call.completion_tokens == 47
            assert not call.usage_estimated
            detail = (await llm_admin.usage_calls(session))["items"][0]
            assert detail["requested_model"] == "kimi-k3[1M]"
            assert detail["pricing_model"] is None
    finally:
        await provider.aclose()


async def test_connectivity_probe_rejects_wrong_model():
    provider = AsyncMock()
    provider.complete.return_value = CompletionResult("OK", "glm-5.3-flash")
    ok, _, error = await llm_admin.probe_model(provider, "kimi-k3[1M]", "chat")
    assert not ok and "LLM_MODEL_MISMATCH" in error


@pytest.mark.parametrize("protocol_error", [False, True])
async def test_summary_failure_preserves_attempt_model(app, monkeypatch, protocol_error):
    async with get_sessionmaker()() as session:
        paper = Paper(title="Provenance", abstract="Test")
        session.add(paper)
        await session.flush()
        revision = await paper_summaries.queue_summary_revision(
            session, paper=paper, created_by=None
        )
        await session.commit()
        revision_id = revision.id

    async def compile_failure(*args, on_response, **kwargs):
        if protocol_error:
            raise ProviderProtocolError("kimi-k3[1M]", "glm-5.3-flash")
        await on_response(CompletionResult("", "kimi-k3", requested_model="kimi-k3[1M]"))
        raise ValueError("librarian returned empty content")

    monkeypatch.setattr("app.services.wiki_compile.compile_paper", compile_failure)
    async with get_sessionmaker()() as session:
        with pytest.raises((ProviderProtocolError, ValueError)):
            await paper_summaries.generate_queued_revision(session, revision_id=revision_id)
    async with get_sessionmaker()() as session:
        failed = await session.get(PaperWikiRevision, revision_id)
        assert failed.status == "failed"
        assert failed.requested_model == "kimi-k3[1M]"
        assert failed.model == ("glm-5.3-flash" if protocol_error else "kimi-k3")
        expected = "LLM_PROVIDER_PROTOCOL_MISMATCH" if protocol_error else "LLM_EMPTY_RESPONSE"
        assert failed.error_code == expected


@pytest.mark.parametrize("protocol_error", [False, True])
async def test_late_provider_result_does_not_overwrite_cancellation(
    app, monkeypatch, protocol_error
):
    import asyncio

    async with get_sessionmaker()() as session:
        paper = Paper(title="Cancelled attempt", abstract="Test")
        session.add(paper)
        await session.flush()
        revision = await paper_summaries.queue_summary_revision(
            session, paper=paper, created_by=None
        )
        await session.commit()
        revision_id = revision.id

    async def late_result(*args, on_response, **kwargs):
        async with get_sessionmaker()() as other:
            cancelled = await other.get(PaperWikiRevision, revision_id)
            cancelled.status = "failed"
            cancelled.stage = None
            cancelled.error_code = "SUMMARY_CANCELLED"
            await other.commit()
        if protocol_error:
            raise ProviderProtocolError("kimi-k3[1M]", "glm-5.3-flash")
        await on_response(CompletionResult("", "kimi-k3", requested_model="kimi-k3[1M]"))

    monkeypatch.setattr("app.services.wiki_compile.compile_paper", late_result)
    async with get_sessionmaker()() as session:
        with pytest.raises(ProviderProtocolError if protocol_error else asyncio.CancelledError):
            await paper_summaries.generate_queued_revision(session, revision_id=revision_id)
    async with get_sessionmaker()() as session:
        cancelled = await session.get(PaperWikiRevision, revision_id)
        assert cancelled.status == "failed" and cancelled.error_code == "SUMMARY_CANCELLED"
