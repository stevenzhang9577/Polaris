"""Read only CC Switch's local price table; never read provider credentials."""

import asyncio
import re
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.llm_admin import ModelPricing


def canonical_model(model: str) -> str:
    # Claude Code's context suffix does not change the base model's reference price.
    return re.sub(r"\[\d+[mk]\]$", "", model.strip().lower())


def read_prices(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1) as conn:
            rows = conn.execute(
                "SELECT model_id, input_cost_per_million, output_cost_per_million, "
                "cache_read_cost_per_million, cache_creation_cost_per_million "
                "FROM model_pricing LIMIT 10000"
            ).fetchall()
    except sqlite3.Error:
        return {}
    prices = {}
    for model, input_rate, output, read, creation in rows:
        try:
            price = ModelPricing(
                input_per_million=input_rate,
                output_per_million=output,
                cache_read_per_million=read,
                cache_creation_per_million=creation,
            )
        except ValidationError:
            continue
        prices[canonical_model(model)] = price.model_dump(mode="json")
    return prices


async def local_prices() -> dict[str, dict]:
    if not get_settings().is_desktop:
        return {}
    return await asyncio.to_thread(read_prices, Path.home() / ".cc-switch" / "cc-switch.db")
