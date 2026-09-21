"""Normalize provider token usage into Polaris' billing buckets.

``prompt_tokens`` is the full input context: fresh input plus cache reads and
cache writes.  Cache buckets are only emitted when the provider reports them
exactly, except for APIs whose protocol has no cache-write billing bucket.
"""

from typing import Any


def _token_count(value: Any) -> int | None:
    """Return a trustworthy token count, rejecting bools and invalid values."""
    if type(value) is int and value >= 0:
        return value
    return None


def _common_usage(*, prompt: Any, completion: Any, total: Any = None) -> dict[str, int]:
    usage: dict[str, int] = {}
    if (value := _token_count(prompt)) is not None:
        usage["prompt_tokens"] = value
    if (value := _token_count(completion)) is not None:
        usage["completion_tokens"] = value
    if (value := _token_count(total)) is not None:
        usage["total_tokens"] = value
    return usage


def normalize_openai_chat_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize Chat Completions and compatible-provider usage.

    OpenAI reports cached input as a subset of ``prompt_tokens``. DeepSeek's
    compatible API exposes the same value as ``prompt_cache_hit_tokens``.
    Neither protocol exposes a separately billed cache-write bucket.
    """
    if raw is None:
        return {}

    prompt = _token_count(raw.get("prompt_tokens"))
    cache_read: int | None = None
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        cache_read = _token_count(details.get("cached_tokens"))
    if cache_read is None:
        cache_read = _token_count(raw.get("prompt_cache_hit_tokens"))

    # DeepSeek normally returns prompt_tokens, but hit + miss is also an exact
    # total for compatible relays that omit it.
    if prompt is None and cache_read is not None:
        cache_miss = _token_count(raw.get("prompt_cache_miss_tokens"))
        if cache_miss is not None:
            prompt = cache_read + cache_miss

    usage = _common_usage(
        prompt=prompt,
        completion=raw.get("completion_tokens"),
        total=raw.get("total_tokens"),
    )
    if cache_read is not None:
        usage["cache_read_tokens"] = cache_read
    # Chat Completions has no cache-write usage/billing field. A received usage
    # object therefore establishes this bucket exactly, even when read details
    # are absent.
    usage["cache_creation_tokens"] = 0
    return usage


def normalize_openai_responses_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize Responses API usage; cached input is part of input_tokens."""
    if raw is None:
        return {}

    usage = _common_usage(
        prompt=raw.get("input_tokens"),
        completion=raw.get("output_tokens"),
        total=raw.get("total_tokens"),
    )
    details = raw.get("input_tokens_details")
    if (
        isinstance(details, dict)
        and (cached := _token_count(details.get("cached_tokens"))) is not None
    ):
        usage["cache_read_tokens"] = cached
    # Responses has cache reads but no separately billed cache-write bucket.
    usage["cache_creation_tokens"] = 0
    return usage


def normalize_anthropic_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize Anthropic usage without treating fresh input as total input.

    Anthropic's ``input_tokens`` excludes both cache reads and cache writes.
    The full prompt total is exact only when all three buckets are present.
    """
    if raw is None:
        return {}

    fresh = _token_count(raw.get("input_tokens"))
    cache_read = _token_count(raw.get("cache_read_input_tokens"))
    cache_creation = _token_count(raw.get("cache_creation_input_tokens"))
    if cache_read is None and cache_creation is None:
        # Without cache_control Anthropic omits both fields; that shape means
        # this call used neither cache reads nor cache writes.
        cache_read = 0
        cache_creation = 0
    prompt = (
        fresh + cache_read + cache_creation
        if fresh is not None and cache_read is not None and cache_creation is not None
        else None
    )
    completion = _token_count(raw.get("output_tokens"))
    usage = _common_usage(prompt=prompt, completion=completion)
    if cache_read is not None:
        usage["cache_read_tokens"] = cache_read
    if cache_creation is not None:
        usage["cache_creation_tokens"] = cache_creation
    if prompt is not None and completion is not None:
        usage["total_tokens"] = prompt + completion
    return usage
