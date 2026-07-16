"""Tests for the token usage-type breakdown projection (ADR-0132 D3/D4)."""
from __future__ import annotations

import pytest

from novafabric.cost.usage_breakdown import (
    UsageBreakdown,
    compute_usage_breakdown,
)


def test_composition_shares_reflect_counts_and_sum_to_one() -> None:
    totals = {"input_tokens": 60, "output_tokens": 40}
    b = compute_usage_breakdown(totals)
    assert isinstance(b, UsageBreakdown)
    assert b.counted_tokens == 100
    assert b.composition["input_tokens"] == pytest.approx(0.6)
    assert b.composition["output_tokens"] == pytest.approx(0.4)
    assert sum(b.composition.values()) == pytest.approx(1.0)


def test_absent_field_is_not_zero_filled() -> None:
    # cache_write_tokens was never reported → it must be absent, not 0.0.
    b = compute_usage_breakdown({"input_tokens": 10, "output_tokens": 10})
    assert "cache_write_tokens" not in b.composition


def test_total_tokens_excluded_from_composition() -> None:
    # total_tokens is the provider's own aggregate — counting it would double-count.
    b = compute_usage_breakdown({"input_tokens": 30, "output_tokens": 70, "total_tokens": 100})
    assert "total_tokens" not in b.composition
    assert b.counted_tokens == 100


def test_cached_read_ratio() -> None:
    b = compute_usage_breakdown({"input_tokens": 100, "cached_tokens": 25})
    assert b.cached_read_ratio == pytest.approx(0.25)


def test_cached_read_ratio_none_when_input_absent() -> None:
    b = compute_usage_breakdown({"cached_tokens": 25})
    assert b.cached_read_ratio is None


def test_has_reasoning_tokens_flag() -> None:
    assert compute_usage_breakdown({"reasoning_tokens": 5}).has_reasoning_tokens is True
    assert compute_usage_breakdown({"output_tokens": 5}).has_reasoning_tokens is False


def test_is_multimodal_flag() -> None:
    assert compute_usage_breakdown({"audio_input_tokens": 3}).is_multimodal is True
    assert compute_usage_breakdown({"image_output_tokens": 3}).is_multimodal is True
    assert compute_usage_breakdown({"input_tokens": 3}).is_multimodal is False


def test_extra_keys_in_composition_and_total() -> None:
    b = compute_usage_breakdown({"input_tokens": 90, "extra": {"video_input_tokens": 10}})
    assert b.extra_total == 10
    assert b.composition["extra.video_input_tokens"] == pytest.approx(0.1)


def test_empty_usage_is_safe_zero() -> None:
    b = compute_usage_breakdown({})
    assert b.counted_tokens == 0
    assert b.composition == {}
    assert b.cached_read_ratio is None
    assert b.has_reasoning_tokens is False
    assert b.is_multimodal is False
    assert b.extra_total == 0


def test_no_cost_or_verdict_field() -> None:
    b = compute_usage_breakdown({"input_tokens": 10})
    forbidden = {"usd", "cost", "dollars", "efficient", "within_budget", "verdict", "score"}
    assert forbidden.isdisjoint(b.model_dump().keys())
