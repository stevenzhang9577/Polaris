"""Provider usage normalization keeps unknown cache buckets unknown."""

from app.core.llm.usage import (
    normalize_anthropic_usage,
    normalize_openai_chat_usage,
    normalize_openai_responses_usage,
)


def test_openai_missing_cache_read_is_not_reported_as_zero():
    assert normalize_openai_chat_usage({"prompt_tokens": 3, "completion_tokens": 1}) == {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "cache_creation_tokens": 0,
    }
    assert normalize_openai_responses_usage({"input_tokens": 3, "output_tokens": 1}) == {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "cache_creation_tokens": 0,
    }


def test_anthropic_total_input_requires_all_cache_buckets():
    usage = normalize_anthropic_usage(
        {
            "input_tokens": 3,
            "cache_read_input_tokens": 2,
            "output_tokens": 1,
        }
    )
    assert usage == {"completion_tokens": 1, "cache_read_tokens": 2}
    assert "prompt_tokens" not in usage
    assert "cache_creation_tokens" not in usage


def test_anthropic_omitted_cache_pair_means_no_cache():
    assert normalize_anthropic_usage({"input_tokens": 3, "output_tokens": 1}) == {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "total_tokens": 4,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_missing_usage_object_stays_empty():
    assert normalize_openai_chat_usage(None) == {}
    assert normalize_openai_responses_usage(None) == {}
    assert normalize_anthropic_usage(None) == {}
