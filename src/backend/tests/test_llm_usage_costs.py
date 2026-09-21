"""Usage accounting: cache partitions, immutable pricing and tenant boundaries."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.base import CompletionResult, Message, StreamDone, TextDelta
from app.core.llm.fake import FakeProvider
from app.core.llm.pricing import estimate_cost
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.llm_config import LLMUsage
from app.models.voyage import VoyageRun
from tests.conftest import RecordingBus, register_and_login

RATES = {
    "input_per_million": "2",
    "output_per_million": "8",
    "cache_read_per_million": "0.2",
    "cache_creation_per_million": "2.5",
}
USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 100,
    "cache_read_tokens": 600,
    "cache_creation_tokens": 100,
}


def test_cache_cost_does_not_double_count_input():
    # fresh 300 * $2 + read 600 * $0.2 + write 100 * $2.5 + output 100 * $8
    assert estimate_cost(USAGE, RATES) == Decimal("0.0017700000")


@pytest.mark.parametrize(
    "usage,rates",
    [
        (USAGE, None),
        ({"prompt_tokens": 1000, "completion_tokens": 100}, RATES),
        (USAGE, {**RATES, "cache_read_per_million": None}),
        ({**USAGE, "prompt_tokens": 500}, RATES),
        ({**USAGE, "cache_read_tokens": -1}, RATES),
    ],
)
def test_incomplete_or_invalid_cost_is_unknown(usage, rates):
    assert estimate_cost(usage, rates) is None


def test_smallest_allowed_price_retains_nonzero_per_call_cost():
    usage = {
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    assert estimate_cost(usage, {**RATES, "input_per_million": "0.00000001"}) == Decimal("1e-14")


def test_zero_usage_and_free_prices_are_not_missing():
    assert estimate_cost({key: 0 for key in USAGE}, RATES) == 0
    assert estimate_cost(USAGE, {key: "0" for key in RATES}) == 0
    zero = LLMRouter._ensure_usage(
        [Message("user", "large prompt")],
        "some output",
        {"prompt_tokens": 0, "completion_tokens": 0},
    )
    assert zero == {"prompt_tokens": 0, "completion_tokens": 0, "usage_estimated": 0}
    partial = LLMRouter._ensure_usage([], "output", {"prompt_tokens": 0})
    assert partial["prompt_tokens"] == 0
    assert partial["completion_tokens"] > 0
    assert partial["usage_estimated"] == 1


class MeteredProvider(FakeProvider):
    async def complete(self, messages, *, model, **kwargs):
        return CompletionResult("answer", model="actual-model", usage=dict(USAGE))

    async def stream_events(self, messages, **kwargs):
        yield TextDelta("answer")
        yield StreamDone("stop", dict(USAGE))


class LegacyStreamProvider(FakeProvider):
    """Old test/provider shape: plain stream override and no FakeProvider state."""

    def __init__(self):
        pass

    async def stream(self, messages, *, model, **kwargs):
        yield "legacy answer"


class StructuredStreamProvider(FakeProvider):
    async def stream_events(self, messages, **kwargs):
        raise AssertionError(
            "an inherited structured stream must not bypass a newer plain override"
        )
        yield  # pragma: no cover - keep this method an async generator


class AdapterLegacyStreamProvider(StructuredStreamProvider):
    def __init__(self):
        pass

    async def stream(self, messages, *, model, **kwargs):
        yield "adapter override"


async def configure(client, headers):
    response = await client.post(
        "/api/admin/llm/providers",
        headers=headers,
        json={"name": "metered", "kind": "fake", "model_pricing": {"billing-alias": RATES}},
    )
    assert response.status_code == 201, response.text
    provider = response.json()
    response = await client.put(
        "/api/admin/llm/routes",
        headers=headers,
        json=[{"stage": "default", "provider_id": provider["id"], "model": "billing-alias"}],
    )
    assert response.status_code == 200
    return provider


@pytest.mark.parametrize("mode", ["complete", "stream", "stream_events", "broadcast"])
async def test_all_generation_paths_record_metered_cache_usage(client, monkeypatch, mode):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    provider = await configure(client, headers)
    assert provider["model_pricing"]["billing-alias"]["cache_read_per_million"] == "0.2"
    router = get_llm_router()
    router.override_provider(MeteredProvider())
    messages = [Message("user", "hello")]
    if mode == "broadcast":
        # No Voyage writes are needed to verify the provider/router accounting boundary.
        monkeypatch.setattr("app.services.voyage_logs.record_terminal_log", AsyncMock())
        router.event_bus = RecordingBus()
        async with get_sessionmaker()() as session:
            run = VoyageRun(kind="writing", goal="verify usage")
            session.add(run)
            await session.commit()
            run_id = run.id
        result = await router.complete("librarian", messages, voyage_id=run_id)
        assert {key: result.usage[key] for key in USAGE} == USAGE
        assert result.content == "answer"
    elif mode == "complete":
        await router.complete("default", messages)
    elif mode == "stream":
        assert "".join([chunk async for chunk in router.stream("default", messages)]) == "answer"
    else:
        assert len([event async for event in router.stream_events("default", messages)]) == 2
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.prompt_tokens == 1000
        assert row.cache_read_tokens == 600
        assert row.cache_creation_tokens == 100
        assert row.usage_estimated is False
        assert row.provider_name == "metered"
        assert row.cost_usd == Decimal("0.00177")
        assert row.pricing_snapshot == {"model": "billing-alias", "currency": "USD", "rates": RATES}


async def test_legacy_plain_stream_broadcast_remains_compatible(client, monkeypatch):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    await configure(client, headers)
    router = get_llm_router()
    router.override_provider(LegacyStreamProvider())
    router.event_bus = RecordingBus()
    monkeypatch.setattr("app.services.voyage_logs.record_terminal_log", AsyncMock())
    async with get_sessionmaker()() as session:
        run = VoyageRun(kind="writing", goal="verify legacy stream")
        session.add(run)
        await session.commit()
        run_id = run.id

    result = await router.complete("librarian", [Message("user", "hello")], voyage_id=run_id)

    assert result.content == "legacy answer"
    assert "usage_estimated" not in result.usage
    router.event_bus = None
    chunks = [chunk async for chunk in router.stream("default", [Message("user", "hi")])]
    assert "".join(chunks) == "legacy answer"
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert len(rows) == 2
        assert all(row.usage_estimated is True for row in rows)


async def test_more_specific_plain_stream_beats_inherited_structured_stream(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    await configure(client, headers)
    router = get_llm_router()
    router.override_provider(AdapterLegacyStreamProvider())

    content = "".join([chunk async for chunk in router.stream("default", [Message("user", "hi")])])

    assert content == "adapter override"


async def test_price_changes_only_affect_future_calls_and_preserve_unknown_history(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    provider = await configure(client, headers)
    router = get_llm_router()
    router.override_provider(MeteredProvider())
    await router.complete("default", [Message("user", "first")])
    new_rates = {key: str(Decimal(value) * 2) for key, value in RATES.items()}
    result = await client.patch(
        f"/api/admin/llm/providers/{provider['id']}",
        headers=headers,
        json={"model_pricing": {"billing-alias": new_rates}},
    )
    assert result.status_code == 200
    await router.complete("default", [Message("user", "second")])
    async with get_sessionmaker()() as session:
        session.add(LLMUsage(stage="default", model="actual-model", prompt_tokens=40))
        await session.commit()
        rows = (
            (await session.execute(select(LLMUsage).order_by(LLMUsage.created_at))).scalars().all()
        )
        assert [row.cost_usd for row in rows] == [Decimal("0.00177"), Decimal("0.00354"), None]
        assert rows[0].pricing_snapshot["rates"] == RATES
    report = (await client.get("/api/admin/llm/usage", headers=headers)).json()
    metered = next(row for row in report if row["provider_name"] == "metered")
    assert Decimal(metered["cost_usd"]) == Decimal("0.00531")
    assert metered["priced_calls"] == metered["cache_reported_calls"] == 2
    assert metered["estimated_calls"] == 0
    legacy = next(row for row in report if row["provider_name"] is None)
    assert legacy["cost_usd"] is None
    assert legacy["priced_calls"] == legacy["cache_reported_calls"] == 0
    assert legacy["estimated_calls"] == 1


async def test_prices_and_usage_are_scoped_to_the_current_user(client):
    owner = {
        "Authorization": f"Bearer {await register_and_login(client, email='owner@example.com')}"
    }
    member = {
        "Authorization": f"Bearer {await register_and_login(client, email='member@example.com')}"
    }
    other = {
        "Authorization": f"Bearer {await register_and_login(client, email='other@example.com')}"
    }
    provider = await configure(client, member)
    provider_url = f"/api/admin/llm/providers/{provider['id']}"
    for headers in (owner, other):
        response = await client.patch(provider_url, headers=headers, json={"model_pricing": None})
        assert response.status_code == 404
    member_id = uuid.UUID((await client.get("/api/users/me", headers=member)).json()["id"])
    router = get_llm_router()
    router.override_provider(MeteredProvider())
    await router.complete("default", [Message("user", "hello")], user_id=member_id)
    mine = await client.get("/api/users/me/usage/history", headers=member)
    assert len(mine.json()) == 1
    assert Decimal(mine.json()[0]["cost_usd"]) == Decimal("0.00177")
    assert (await client.get("/api/users/me/usage/history", headers=other)).json() == []
    assert (await client.get("/api/admin/llm/usage", headers=member)).status_code == 403
    assert (await client.patch(provider_url, headers=member, json={"model_pricing": None})).json()[
        "model_pricing"
    ] is None


@pytest.mark.parametrize("rate", ["-1", "NaN", "Infinity", "1000001", "0.000000001", ""])
async def test_invalid_prices_rejected(client, rate):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    response = await client.post(
        "/api/admin/llm/providers",
        headers=headers,
        json={
            "name": "invalid",
            "kind": "fake",
            "model_pricing": {
                "model": {
                    **RATES,
                    "input_per_million": rate,
                }
            },
        },
    )
    assert response.status_code == 422
