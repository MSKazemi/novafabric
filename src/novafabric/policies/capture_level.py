"""Capture-level policy engine (cap-004, ADR-0066).

Four levels: minimal, standard, forensic, air_gapped.
Configured via NOVA_CAPTURE_LEVEL env var; default is standard.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Any


class CaptureLevel(str, Enum):
    """Four capture levels per scale-architecture ADR-004."""

    MINIMAL = "minimal"       # No PII fields; only metadata
    STANDARD = "standard"     # Standard capture; redacted PII
    FORENSIC = "forensic"     # Full capture including prompts/responses
    AIR_GAPPED = "air_gapped"  # Full capture, no external network calls permitted


# Fields allowed per capture level.  Empty set means *all* fields are allowed.
CAPTURE_LEVEL_ALLOWLIST: dict[CaptureLevel, set[str]] = {
    CaptureLevel.MINIMAL: {
        "run_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "capsule_id",
        "exit_code",
        "duration_ms",
    },
    CaptureLevel.STANDARD: {
        "run_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "capsule_id",
        "exit_code",
        "duration_ms",
        "model_id",
        "provider",
        "input_tokens",
        "completion_tokens",
        "tool_name",
        "tool_result_summary",
    },
    # Empty = all fields allowed
    CaptureLevel.FORENSIC: set(),
    CaptureLevel.AIR_GAPPED: set(),
}

# Fields exempt from the allowlist check at all levels.
_EXEMPT_FIELDS: frozenset[str] = frozenset(
    {"schema_version", "allowed_processing_regions"}
)


class CaptureLevelPolicy:
    """Enforces a capture level on incoming events.

    cap-004 / ADR-0066.
    """

    def __init__(self, level: CaptureLevel = CaptureLevel.STANDARD) -> None:
        self.level = level
        self._logger = logging.getLogger(__name__)

    def validate_event(self, event: dict[str, Any]) -> tuple[bool, str | None]:
        """Return (is_valid, rejection_reason).

        An empty allowlist means all fields are permitted (FORENSIC / AIR_GAPPED).
        Fields in _EXEMPT_FIELDS are always permitted.
        """
        if self.level in (CaptureLevel.FORENSIC, CaptureLevel.AIR_GAPPED):
            return True, None

        allowlist = CAPTURE_LEVEL_ALLOWLIST[self.level]
        forbidden = [
            k for k in event if k not in allowlist and k not in _EXEMPT_FIELDS
        ]
        if forbidden:
            return False, (
                f"Capture level {self.level.value} forbids fields: {forbidden}"
            )
        return True, None

    @classmethod
    def from_env(cls) -> CaptureLevelPolicy:
        """Read NOVA_CAPTURE_LEVEL env var; default standard."""
        level_str = os.getenv("NOVA_CAPTURE_LEVEL", "standard")
        try:
            level = CaptureLevel(level_str)
        except ValueError:
            logging.getLogger(__name__).warning(
                "Unknown NOVA_CAPTURE_LEVEL=%r; using standard", level_str
            )
            level = CaptureLevel.STANDARD
        return cls(level)
