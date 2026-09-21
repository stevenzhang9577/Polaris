"""Local pricing and usage detail authorization without real profile access."""

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from app.core.db import get_sessionmaker
from app.models.llm_config import LLMUsage
from app.services import cc_switch_pricing, llm_admin
from tests.conftest import register_and_login


def test_price_reader_is_readonly_validates_and_matches_context_suffix(tmp_path):
    path = tmp_path / "cc-switch.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE model_pricing (model_id, input_cost_per_million, "
            "output_cost_per_million, cache_read_cost_per_million, "
            "cache_creation_cost_per_million)"
        )
        conn.executemany(
            "INSERT INTO model_pricing VALUES (?, ?, ?, ?, ?)",
            [
                ("kimi-k3", "3", "15", ".3", "0"),
                ("bad", "-1", "2", "0", "0"),
            ],
        )
    before = path.read_bytes()
    prices = cc_switch_pricing.read_prices(path)
    assert prices.keys() == {"kimi-k3"}
    assert prices[cc_switch_pricing.canonical_model("Kimi-K3[1M]")]["input_per_million"] == "3"
    assert path.read_bytes() == before
    assert cc_switch_pricing.read_prices(tmp_path / "missing.db") == {}
    assert not (tmp_path / "missing.db").exists()


async def test_reference_price_does_not_rewrite_history_and_calls_are_private(client, monkeypatch):
    owner = await register_and_login(client, "usage-detail-owner@example.com")
    owner = {"Authorization": f"Bearer {owner}"}
    owner_id = (await client.get("/api/users/me", headers=owner)).json()["id"]
    other = await register_and_login(client, "usage-detail-other@example.com")
    other = {"Authorization": f"Bearer {other}"}
    rates = {
        "input_per_million": "3",
        "output_per_million": "15",
        "cache_read_per_million": "0.3",
        "cache_creation_per_million": "0",
    }
    monkeypatch.setattr(
        cc_switch_pricing, "local_prices", AsyncMock(return_value={"kimi-k3": rates})
    )
    stamp = datetime.now(UTC).replace(microsecond=123456)
    async with get_sessionmaker()() as session:
        call = LLMUsage(
            user_id=uuid.UUID(owner_id),
            stage="librarian",
            model="kimi-k3[1M]",
            prompt_tokens=1000,
            completion_tokens=100,
            created_at=stamp,
        )
        session.add(call)
        await session.commit()
        call_id = call.id
    response = await client.get("/api/users/me/usage/calls?limit=1", headers=owner)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    row = page["items"][0]
    assert datetime.fromisoformat(row["occurred_at"]) == stamp
    assert row["provider_name"] is None
    assert row["cost_usd"] is None
    assert Decimal(row["reference_cost_usd"]) == Decimal("0.0045")
    assert (await client.get("/api/users/me/usage/calls", headers=other)).json()["total"] == 0
    assert (await client.get("/api/admin/llm/usage/calls", headers=other)).status_code == 403
    offset_page = await client.get("/api/users/me/usage/calls?offset=1", headers=owner)
    assert offset_page.json()["items"] == []
    filtered = await client.get("/api/users/me/usage/calls?model=other", headers=owner)
    assert filtered.json()["total"] == 0
    async with get_sessionmaker()() as session:
        call = await session.get(LLMUsage, call_id)
        assert call.cost_usd is None and call.provider_name is None
        rows = await llm_admin.usage_report(session, user_id=uuid.UUID(owner_id))
        assert rows[0]["reference_cost_usd"] == Decimal("0.0045")


async def test_server_never_reads_local_price_database(app, monkeypatch):
    def forbidden(_):
        raise AssertionError("Server must not read local prices")

    monkeypatch.setattr(cc_switch_pricing, "read_prices", forbidden)
    assert await cc_switch_pricing.local_prices() == {}


async def test_route_save_during_cache_load_cannot_restore_old_model():
    from app.core.llm.router import LLMRouter

    router = LLMRouter()
    reading = asyncio.Event()
    finish = asyncio.Event()
    reads = 0

    async def load(_):
        nonlocal reads
        reads += 1
        if reads == 1:
            reading.set()
            await finish.wait()
            return {"default": "old-model"}
        return {"default": "new-model"}

    router._load_routes = load
    task = asyncio.create_task(router._cached_routes(None))
    await reading.wait()
    router.invalidate_cache()
    finish.set()
    assert (await task)["default"] == "new-model"
    assert (await router._cached_routes(None))["default"] == "new-model"
