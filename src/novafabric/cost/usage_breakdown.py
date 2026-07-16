"""Token usage-type breakdown projection (ADR-0132 D3/D4).

A pure descriptive projection over the ``usage_totals`` aggregate NovaFabric already writes to a
capsule manifest at finalize (:func:`novafabric.cost.usage_types.usage_totals_from_model_calls`). It
reports the **composition** of a capsule's token volume — each usage type's share of the counted
tokens — plus a couple of well-defined ratios and factual booleans.

It honours the ADR-0132 **"absent != zero"** invariant: a usage type the provider never reported is
absent from the composition, never zero-filled, and ratios that cannot be computed are ``None``
(never a fabricated ``0.0``). It reports **token composition only** — there is deliberately no
cost/dollars field (pricing is ADR-0133) and no efficient/within-budget/verdict field.
``total_tokens`` is excluded from the composition so the provider's own aggregate is not
double-counted.

This slice is the pure statistic over a supplied ``usage_totals`` mapping; the CLI collector reads
it from the sealed capsule manifest.
"""
from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from novafabric.cost.usage_types import NAMED_USAGE_FIELDS

#: Named fields that are provider aggregates, excluded from composition (avoids double-counting).
_AGGREGATE_FIELDS: frozenset[str] = frozenset({"total_tokens"})

#: Named fields whose presence (> 0) means the call carried non-text modality.
_MULTIMODAL_FIELDS: tuple[str, ...] = (
    "audio_input_tokens",
    "audio_output_tokens",
    "image_input_tokens",
    "image_output_tokens",
)


class UsageBreakdown(BaseModel):
    counted_tokens: int  # sum of every token type in the composition
    composition: dict[str, float]  # usage type -> share of counted_tokens (present types only)
    cached_read_ratio: float | None  # cached_tokens / input_tokens, None when not computable
    has_reasoning_tokens: bool
    is_multimodal: bool
    extra_total: int  # sum of the open ``extra`` map (0 when absent)
    # Intentionally NO cost/usd/efficient/within_budget/verdict field — token composition only.


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def compute_usage_breakdown(usage_totals: Mapping[str, object]) -> UsageBreakdown:
    """Project a capsule's ``usage_totals`` into a descriptive token-composition breakdown.

    ``usage_totals`` is the manifest aggregate: named token fields present only when the provider
    reported them, plus an open ``extra`` map. The composition gives each present usage type's share
    of the counted tokens (named fields except ``total_tokens``, plus ``extra.<key>`` entries).
    Absent types stay absent; an empty input yields a safe zero breakdown.
    """
    counts: dict[str, int] = {}
    for field in NAMED_USAGE_FIELDS:
        if field in _AGGREGATE_FIELDS:
            continue
        count = _as_int(usage_totals.get(field))
        if count is not None:
            counts[field] = count

    extra = usage_totals.get("extra")
    extra_total = 0
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            count = _as_int(value)
            if isinstance(key, str) and count is not None:
                counts[f"extra.{key}"] = count
                extra_total += count

    counted_tokens = sum(counts.values())
    composition = (
        {k: counts[k] / counted_tokens for k in counts} if counted_tokens > 0 else {}
    )

    input_tokens = _as_int(usage_totals.get("input_tokens"))
    cached_tokens = _as_int(usage_totals.get("cached_tokens"))
    cached_read_ratio: float | None = None
    if input_tokens is not None and cached_tokens is not None and input_tokens > 0:
        cached_read_ratio = cached_tokens / input_tokens

    reasoning = _as_int(usage_totals.get("reasoning_tokens")) or 0
    is_multimodal = any((_as_int(usage_totals.get(f)) or 0) > 0 for f in _MULTIMODAL_FIELDS)

    return UsageBreakdown(
        counted_tokens=counted_tokens,
        composition=composition,
        cached_read_ratio=cached_read_ratio,
        has_reasoning_tokens=reasoning > 0,
        is_multimodal=is_multimodal,
        extra_total=extra_total,
    )
