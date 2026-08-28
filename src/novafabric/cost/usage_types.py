"""Token usage-type accounting (ADR-0132, spec token-usage-types-v0).

Normalizes provider-reported usage payloads into the additive, optional
``nova.usage`` block on a model-call record, and rolls per-call blocks up
into the capsule-level ``usage_totals`` aggregate at finalize.

Invariants (normative — see ``the private design/spec/token-usage-types-v0.md``):

- **Verbatim.** Every count is copied from the provider usage payload;
  NovaFabric never re-tokenizes content to compute a usage type.
- **Absent != zero.** A usage type the provider did not report is absent,
  never zero-filled. Roll-ups skip absent fields instead of counting 0.
- **Superset, not replacement.** The legacy scalars
  (``gen_ai.usage.input_tokens`` / ``output_tokens`` and
  ``CostFacet.{input_tokens, completion_tokens, cached_tokens}``) are
  unchanged; ``nova.usage`` adds the rest of the provider's breakdown.
- **Never fail the workload.** Extraction is best-effort: a malformed or
  missing payload yields ``None`` (no block), never an exception.
- **Open ``extra`` map.** Provider-reported ``*_tokens`` keys NovaFabric
  does not name land in ``extra`` under their snake_case name, so new
  provider usage types are captured with zero schema change.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The closed set of named usage-type fields (schemas/token-usage.schema.json).
NAMED_USAGE_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
    "image_input_tokens",
    "image_output_tokens",
    "total_tokens",
)

_EXTRA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Provider payload key -> nova.usage field (ADR-0132 D2). The mapping lives
# here in the capture layer, never in the record itself.
_OPENAI_TOP: dict[str, str] = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "total_tokens": "total_tokens",
}
_OPENAI_PROMPT_DETAILS: dict[str, str] = {
    "cached_tokens": "cached_tokens",
    "audio_tokens": "audio_input_tokens",
    "image_tokens": "image_input_tokens",
}
_OPENAI_COMPLETION_DETAILS: dict[str, str] = {
    "reasoning_tokens": "reasoning_tokens",
    "audio_tokens": "audio_output_tokens",
    "image_tokens": "image_output_tokens",
}
_ANTHROPIC_TOP: dict[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cached_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    # Extended-thinking token fields, when the provider reports them.
    "thinking_tokens": "reasoning_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def _as_count(value: object) -> int | None:
    """Return ``value`` as a verbatim non-negative int, else ``None``.

    Strict on purpose: bools, floats, strings, Mock objects, and negative
    numbers are all "not a reported count" — recorded as absent, never
    coerced or zero-guessed.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _get(payload: object, key: str) -> object:
    """Read ``key`` from a mapping or an SDK object attribute."""
    if isinstance(payload, Mapping):
        return payload.get(key)
    return getattr(payload, key, None)


def _to_mapping(payload: object) -> Mapping[str, Any] | None:
    """Best-effort mapping view of a usage payload for key enumeration.

    Plain dicts pass through; Pydantic-style SDK objects are dumped via
    ``model_dump()``. Anything else (including test doubles whose
    ``model_dump()`` does not return a mapping) yields ``None`` — named
    fields are still read by attribute, only ``extra`` enumeration is
    skipped.
    """
    if isinstance(payload, Mapping):
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:  # noqa: BLE001 — never fail the workload
            return None
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _normalize_extra_key(key: str) -> str | None:
    """Normalize a provider usage-type key to snake_case; None if unusable."""
    normalized = key.strip().replace("-", "_").replace(" ", "_").lower()
    return normalized if _EXTRA_KEY_RE.fullmatch(normalized) else None


def _add_extra(extra: dict[str, int], key: str, count: int) -> None:
    normalized = _normalize_extra_key(key)
    if normalized is None:
        return
    # Same unrecognized key appearing in more than one sub-object is summed.
    extra[normalized] = extra.get(normalized, 0) + count


def _collect(
    payload: object,
    named_map: Mapping[str, str],
    block: dict[str, Any],
    extra: dict[str, int],
) -> None:
    """Collect named fields (by key/attribute) and unrecognized ``*_tokens``.

    Named fields are read even from plain SDK objects; the ``extra`` sweep
    needs key enumeration and therefore only applies to mapping payloads
    (dicts or objects exposing a mapping via ``model_dump()``).
    """
    if payload is None:
        return
    for src_key, field in named_map.items():
        count = _as_count(_get(payload, src_key))
        if count is not None and field not in block:
            block[field] = count
    mapping = _to_mapping(payload)
    if mapping is None:
        return
    for key, value in mapping.items():
        if not isinstance(key, str) or key in named_map:
            continue
        if not key.endswith("_tokens"):
            continue
        count = _as_count(value)
        if count is not None:
            _add_extra(extra, key, count)


def _finalize(block: dict[str, Any], extra: dict[str, int]) -> dict[str, Any] | None:
    if extra:
        block["extra"] = extra
    return block or None


def usage_from_openai(usage: object) -> dict[str, Any] | None:
    """Build a ``nova.usage`` block from an OpenAI usage payload.

    Accepts the SDK usage object or a plain dict. Returns ``None`` when the
    payload is missing, empty, or reports nothing usable — an absent block
    is a valid record (absent != zero). Never raises.
    """
    try:
        if usage is None:
            return None
        block: dict[str, Any] = {}
        extra: dict[str, int] = {}
        _collect(usage, _OPENAI_TOP, block, extra)
        _collect(_get(usage, "prompt_tokens_details"), _OPENAI_PROMPT_DETAILS, block, extra)
        _collect(
            _get(usage, "completion_tokens_details"),
            _OPENAI_COMPLETION_DETAILS,
            block,
            extra,
        )
        return _finalize(block, extra)
    except Exception as exc:  # noqa: BLE001 — capture must never fail the workload
        log.debug("nova.usage: OpenAI usage extraction skipped: %s", exc)
        return None


def usage_from_anthropic(usage: object) -> dict[str, Any] | None:
    """Build a ``nova.usage`` block from an Anthropic usage payload.

    Accepts the SDK usage object or a plain dict. Returns ``None`` when the
    payload is missing, empty, or reports nothing usable. Never raises.
    """
    try:
        if usage is None:
            return None
        block: dict[str, Any] = {}
        extra: dict[str, int] = {}
        _collect(usage, _ANTHROPIC_TOP, block, extra)
        return _finalize(block, extra)
    except Exception as exc:  # noqa: BLE001 — capture must never fail the workload
        log.debug("nova.usage: Anthropic usage extraction skipped: %s", exc)
        return None


def sum_usage_totals(
    blocks: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Pure per-usage-type sum over ``nova.usage`` blocks (ADR-0132 D3).

    Absent per-call fields are skipped, never treated as 0. ``extra`` keys
    are summed per key. Returns ``None`` when no block contributed anything,
    so the capsule aggregate stays absent (not zero-filled).
    """
    totals: dict[str, int] = {}
    extra_totals: dict[str, int] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        for field in NAMED_USAGE_FIELDS:
            count = _as_count(block.get(field))
            if count is not None:
                totals[field] = totals.get(field, 0) + count
        extra = block.get("extra")
        if isinstance(extra, Mapping):
            for key, value in extra.items():
                count = _as_count(value)
                if isinstance(key, str) and count is not None:
                    _add_extra(extra_totals, key, count)
    # Stable field order: named fields first (spec order), then extra.
    result: dict[str, Any] = {
        field: totals[field] for field in NAMED_USAGE_FIELDS if field in totals
    }
    if extra_totals:
        result["extra"] = extra_totals
    return result or None


def usage_totals_from_model_calls(path: Path) -> dict[str, Any] | None:
    """Roll ``nova.usage`` blocks in a ``model-calls.jsonl`` file up into
    ``usage_totals`` (ADR-0132 D3). Tolerant of missing files, blank lines,
    and malformed records — a capsule with no usage evidence gets ``None``
    (no aggregate), never a fabricated zero block.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    blocks: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            block = record.get("nova.usage")
            if isinstance(block, dict):
                blocks.append(block)
    return sum_usage_totals(blocks)
