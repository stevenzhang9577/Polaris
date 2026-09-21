"""Deterministic per-call USD estimates from normalized usage and configured rates."""

from decimal import Decimal, InvalidOperation
from typing import Any

_MILLION = Decimal(1_000_000)
_PRECISION = Decimal("0.00000000000001")


def estimate_cost(usage: dict[str, int], pricing: dict[str, Any] | None) -> Decimal | None:
    """Input includes cache reads/writes; charge each token exactly once.

    Unknown usage buckets or rates required by nonzero usage leave cost unknown.
    Zero-priced models are supported, but absence of pricing is never free usage.
    """
    if pricing is None:
        return None
    names = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_creation_tokens")
    values = [usage.get(name) for name in names]
    if any(not isinstance(value, int) or value < 0 for value in values):
        return None
    prompt, output, read, creation = values
    fresh = prompt - read - creation
    if fresh < 0:
        return None
    total = Decimal(0)
    for tokens, key in (
        (fresh, "input_per_million"),
        (output, "output_per_million"),
        (read, "cache_read_per_million"),
        (creation, "cache_creation_per_million"),
    ):
        if tokens == 0:
            continue
        rate = pricing.get(key)
        if rate is None:
            return None
        try:
            rate = Decimal(str(rate))
        except InvalidOperation:
            return None
        if not rate.is_finite() or rate < 0:
            return None
        total += tokens * rate / _MILLION
    return total.quantize(_PRECISION)
