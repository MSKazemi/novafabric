"""How a capsule event's model and token fields are read, shared by both accumulators.

``POST /capsule`` accepts two spellings for the same facts (``collector_app.CapsuleEvent``):
``model`` / ``tokens_in`` / ``tokens_out`` (the DuckDB columns) and ``model_id`` /
``tokens_total`` (the ClickHouse ones). Each accumulator used to read only its own, so the
same event was counted by one backend and lost by the other. Both now read through here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def event_model(event: Mapping[str, Any]) -> str | None:
    """The model name: ``model_id``, else ``model``; ``None`` when neither is a non-empty string."""
    for key in ("model_id", "model"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def event_tokens_in_out(event: Mapping[str, Any]) -> tuple[int, int]:
    """``(tokens_in, tokens_out)``, 0 for an absent count (the DuckDB columns' default)."""
    return int(event.get("tokens_in") or 0), int(event.get("tokens_out") or 0)


def event_tokens_total(event: Mapping[str, Any]) -> int | None:
    """``tokens_total`` when given, else ``tokens_in + tokens_out`` when either is given.

    ``None`` when the event reports no token count at all: "not reported" must stay
    distinguishable from a measured zero (``SUM`` skips ``NULL``).
    """
    total = event.get("tokens_total")
    if total is not None:
        return int(total)
    if event.get("tokens_in") is None and event.get("tokens_out") is None:
        return None
    tokens_in, tokens_out = event_tokens_in_out(event)
    return tokens_in + tokens_out
