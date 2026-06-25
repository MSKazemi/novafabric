"""JSON Schema validation for EventEnvelope v1 using the canonical schema file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "schemas"
    / "event-envelope-v1"
    / "envelope-v1.json"
)


class EventEnvelopeValidationError(Exception):
    """Raised when an event dict fails JSON Schema validation."""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message)
        self.path = path


def _load_schema() -> dict[str, Any]:
    schema_path = _locate_schema()
    result: dict[str, Any] = json.loads(schema_path.read_text(encoding="utf-8"))
    return result


def _locate_schema() -> Path:
    """Find envelope-v1.json by searching from this file's location upward."""
    candidates = [
        _SCHEMA_PATH,
        Path(__file__).parent.parent.parent.parent
        / "schemas"
        / "event-envelope-v1"
        / "envelope-v1.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "envelope-v1.json not found. Expected at schemas/event-envelope-v1/envelope-v1.json"
    )


_schema_cache: dict[str, Any] | None = None


def validate_event(event: dict[str, Any]) -> None:
    """Validate an event dict against the EventEnvelope v1 JSON Schema.

    Raises EventEnvelopeValidationError on failure.
    """
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = _load_schema()
    try:
        jsonschema.validate(event, _schema_cache, format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path)
        raise EventEnvelopeValidationError(exc.message, path=path) from exc
