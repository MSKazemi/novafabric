"""Three-tier entity resolution: normalise → alias table → unresolved queue.

Tier 1 (this module): pure-Python synchronous entity normalization.
Tier 2 (future): persistent alias table in KGStore.
Tier 3 (future): unresolved entity queue for manual review.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from functools import lru_cache

logger = logging.getLogger(__name__)

# OTel GenAI semconv model name controlled vocabulary (canonical forms)
MODEL_CANONICAL: dict[str, str] = {
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4-turbo",
    "gpt-4": "gpt-4",
    "gpt-3.5-turbo": "gpt-3.5-turbo",
    "claude-3-5-sonnet": "claude-3-5-sonnet",
    "claude-3-5-haiku": "claude-3-5-haiku",
    "claude-3-opus": "claude-3-opus",
    "claude-3-sonnet": "claude-3-sonnet",
    "claude-3-haiku": "claude-3-haiku",
    "gemini-1.5-pro": "gemini-1.5-pro",
    "gemini-1.5-flash": "gemini-1.5-flash",
    "gemini-2.0-flash": "gemini-2.0-flash",
    "llama-3.3-70b": "llama-3.3-70b",
    "llama-3.1-405b": "llama-3.1-405b",
    "mistral-large": "mistral-large",
}

# Ordered patterns mapping version-stamped names to canonical forms.
# Patterns are anchored (fullmatch) against the lowercased stripped raw name.
MODEL_PATTERN_MAP: list[tuple[re.Pattern[str], str]] = [
    # GPT-4o with date stamp: gpt-4o-2024-08-06
    (re.compile(r"gpt-4o-\d{4}-\d{2}-\d{2}"), "gpt-4o"),
    # GPT-4o-mini with date stamp
    (re.compile(r"gpt-4o-mini-\d{4}-\d{2}-\d{2}"), "gpt-4o-mini"),
    # Claude 3.5 Sonnet with date stamp: claude-3-5-sonnet-20241022
    (re.compile(r"claude-3-5-sonnet-2\d{7}"), "claude-3-5-sonnet"),
    (re.compile(r"claude-3-5-sonnet-\d{8}"), "claude-3-5-sonnet"),
    # Claude 3.5 Haiku with date stamp
    (re.compile(r"claude-3-5-haiku-2\d{7}"), "claude-3-5-haiku"),
    # Claude 3 Opus/Sonnet/Haiku with date stamp
    (re.compile(r"claude-3-opus-2\d{7}"), "claude-3-opus"),
    (re.compile(r"claude-3-sonnet-2\d{7}"), "claude-3-sonnet"),
    (re.compile(r"claude-3-haiku-2\d{7}"), "claude-3-haiku"),
    # Gemini with revision suffix: gemini-1.5-pro-001
    (re.compile(r"gemini-1\.5-pro-\d{3,4}"), "gemini-1.5-pro"),
    (re.compile(r"gemini-1\.5-flash-\d{3,4}"), "gemini-1.5-flash"),
    (re.compile(r"gemini-2\.0-flash-\d{3,4}"), "gemini-2.0-flash"),
]


class EntityNormaliser:
    """Tier-1 entity normalisation: pure-Python, LRU-cached, synchronous."""

    @staticmethod
    @lru_cache(maxsize=10_000)
    def normalise_model(raw: str) -> str:
        """Normalise a model name to its canonical OTel semconv form."""
        normalised = raw.strip().lower()
        # Direct lookup in controlled vocabulary
        if normalised in MODEL_CANONICAL:
            return MODEL_CANONICAL[normalised]
        # Pattern match for versioned/date-stamped variants
        for pattern, canonical in MODEL_PATTERN_MAP:
            if pattern.fullmatch(normalised):
                return canonical
        # Unknown model → return lowercased as-is; log at DEBUG for future triage
        logger.debug("Unknown model name (no canonical form): %r", raw)
        return normalised

    @staticmethod
    def normalise_endpoint(raw: str) -> str:
        """Normalise an API endpoint URL.

        Rules:
        - Upgrade http → https (for public endpoints)
        - Strip query string and fragment
        - Strip trailing slash from path
        - Lowercase the result
        """
        try:
            parsed = urllib.parse.urlparse(raw.strip())
            scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
            normalised = urllib.parse.urlunparse(
                (scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
            )
            return normalised.lower()
        except Exception:
            return raw.strip().lower()

    @staticmethod
    def normalise_agent(raw: str) -> str:
        """Normalise an agent identifier (strip whitespace, lowercase)."""
        return raw.strip().lower()

    @staticmethod
    def normalise_tool(raw: str) -> str:
        """Normalise a tool name (strip whitespace, lowercase)."""
        return raw.strip().lower()

    @staticmethod
    def normalise_mcp_server(raw: str) -> str:
        """Normalise an MCP server name (strip whitespace, lowercase)."""
        return raw.strip().lower()

    def normalise(self, entity_type: str, raw: str) -> str:
        """Dispatch to the correct normalizer by entity type string."""
        if entity_type == "model":
            return self.normalise_model(raw)
        if entity_type == "endpoint":
            return self.normalise_endpoint(raw)
        if entity_type == "agent":
            return self.normalise_agent(raw)
        if entity_type == "tool":
            return self.normalise_tool(raw)
        if entity_type == "mcp_server":
            return self.normalise_mcp_server(raw)
        logger.debug("Unknown entity_type %r — applying default normalisation", entity_type)
        return raw.strip().lower()
