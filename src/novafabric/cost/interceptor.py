"""CostInterceptor: extract CostFacet from OpenAI and Anthropic SDK responses.

cap-002 / ADR-0066.  Prices are estimates; actual billing may differ.
"""

from __future__ import annotations

import logging

from novafabric.cost.usage_types import usage_from_anthropic, usage_from_openai
from novafabric.schemas.event_schema import CostFacet

log = logging.getLogger(__name__)


class CostInterceptor:
    """Wraps OpenAI and Anthropic clients to extract CostFacet from usage.

    Prices are per-1k-token pairs (input, output).  All values are estimates;
    actual billing may differ.  cap-002 / ADR-0066.
    """

    PRICE_TABLE: dict[str, tuple[float, float]] = {
        # (input_price_per_1k_tokens, output_price_per_1k_tokens)
        # Anthropic Claude 4.x (2025+)
        "claude-opus-4-7": (0.015, 0.075),
        "claude-opus-4": (0.015, 0.075),
        "claude-sonnet-4-6": (0.003, 0.015),
        "claude-sonnet-4-5": (0.003, 0.015),
        "claude-haiku-4-5": (0.0008, 0.004),
        "claude-haiku-4": (0.0008, 0.004),
        # Anthropic Claude 3.x (versioned API IDs)
        "claude-3-7-sonnet-20250219": (0.003, 0.015),
        "claude-3-5-sonnet-20241022": (0.003, 0.015),
        "claude-3-5-haiku-20241022": (0.0008, 0.004),
        "claude-3-opus-20240229": (0.015, 0.075),
        "claude-3-haiku-20240307": (0.00025, 0.00125),
        # Legacy short names (still seen in some SDK wrappers)
        "claude-3-5-sonnet": (0.003, 0.015),
        "claude-3-opus": (0.015, 0.075),
        "claude-3-haiku": (0.00025, 0.00125),
        # OpenAI GPT (2024-2025)
        "gpt-4o": (0.0025, 0.01),
        "gpt-4o-2024-11-20": (0.0025, 0.01),
        "gpt-4o-mini": (0.00015, 0.0006),
        "gpt-4o-mini-2024-07-18": (0.00015, 0.0006),
        "gpt-4-turbo": (0.01, 0.03),
        # OpenAI o-series
        "o1": (0.015, 0.06),
        "o1-2024-12-17": (0.015, 0.06),
        "o1-mini": (0.0011, 0.0044),
        "o3-mini": (0.0011, 0.0044),
    }

    @classmethod
    def _estimate_cost(
        cls,
        model: str,
        input_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
    ) -> float:
        """Return estimated cost in USD.  Returns 0.0 for unknown models.

        Consults the local model-pricing catalog (ADR-0133) merged over
        ``PRICE_TABLE``: a user/project/explicit catalog entry overrides the
        built-in price; with no catalog files on disk the merged catalog is
        exactly ``PRICE_TABLE`` and the figures are unchanged. Pricing must
        never fail a capture, so any catalog error falls back to the legacy
        built-in-table math.
        """
        try:
            from novafabric.cost import (  # noqa: PLC0415 — avoid import cycle
                pricing_catalog,
            )

            catalog = pricing_catalog.load_merged_catalog()
            resolved = pricing_catalog.resolve_entry(catalog, model)
            if resolved is not None:
                return pricing_catalog.price_usage(
                    resolved.entry,
                    {
                        "input": input_tokens,
                        "output": completion_tokens,
                        "cached": cached_tokens,
                    },
                )
        except Exception as exc:  # noqa: BLE001 — pricing must never fail a capture
            log.debug("pricing catalog unavailable, using built-in table: %s", exc)
        prices = cls.PRICE_TABLE.get(model)
        if prices is None:
            return 0.0
        input_price, output_price = prices
        return (
            (input_tokens / 1000.0) * input_price
            + (completion_tokens / 1000.0) * output_price
        )

    @classmethod
    def extract_from_openai_response(cls, response: object, model: str) -> CostFacet:
        """Extract CostFacet from an OpenAI Chat Completion response object.

        Accepts both real SDK objects and plain dicts (for testing).
        """
        if isinstance(response, dict):
            usage = response.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            cached_tokens = int(
                usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            )
        else:
            usage = getattr(response, "usage", None)
            if usage is None:
                input_tokens = completion_tokens = cached_tokens = 0
            else:
                input_tokens = int(getattr(usage, "prompt_tokens", 0))
                completion_tokens = int(getattr(usage, "completion_tokens", 0))
                details = getattr(usage, "prompt_tokens_details", None)
                cached_tokens = int(
                    getattr(details, "cached_tokens", 0) if details else 0
                )

        return CostFacet(
            model_id=model,
            provider="openai",
            input_tokens=input_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cost_usd_estimated=cls._estimate_cost(
                model, input_tokens, completion_tokens, cached_tokens
            ),
            # ADR-0132: full usage-type breakdown, verbatim; None when absent.
            usage=usage_from_openai(usage),
        )

    @classmethod
    def extract_from_anthropic_response(cls, response: object, model: str) -> CostFacet:
        """Extract CostFacet from an Anthropic Message response object.

        Accepts both real SDK objects and plain dicts (for testing).
        """
        if isinstance(response, dict):
            usage = response.get("usage", {})
            input_tokens = int(usage.get("input_tokens", 0))
            completion_tokens = int(usage.get("output_tokens", 0))
            cached_tokens = int(usage.get("cache_read_input_tokens", 0))
        else:
            usage = getattr(response, "usage", None)
            if usage is None:
                input_tokens = completion_tokens = cached_tokens = 0
            else:
                input_tokens = int(getattr(usage, "input_tokens", 0))
                completion_tokens = int(getattr(usage, "output_tokens", 0))
                cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0))

        return CostFacet(
            model_id=model,
            provider="anthropic",
            input_tokens=input_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cost_usd_estimated=cls._estimate_cost(
                model, input_tokens, completion_tokens, cached_tokens
            ),
            # ADR-0132: full usage-type breakdown, verbatim; None when absent.
            usage=usage_from_anthropic(usage),
        )
