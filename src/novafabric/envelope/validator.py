"""JSON Schema validation for EventEnvelope v1 using the canonical schema file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

#: Where the schema lives in an installed wheel. `pyproject.toml`'s
#: force-include maps the canonical repo-root file here at build time, so this
#: is the same bytes as `schemas/event-envelope-v1/envelope-v1.json` — not a
#: second copy in the source tree.
_PACKAGED_SCHEMA_PATH = Path(__file__).parent / "_schemas" / "envelope-v1.json"

#: Repo-root fallbacks, used when running from a source checkout (where the
#: force-include has not happened). Kept because the dev tree is the spec's
#: home; they are NOT reachable in a wheel.
_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent.parent
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
    """Find envelope-v1.json, preferring the packaged copy.

    The packaged path must be tried **first**: in a wheel it is the only one
    that exists, and previously every candidate was a repo-root path, so a
    plain ``pip install novafabric`` raised ``FileNotFoundError`` here and
    Event Envelope v1 validation was simply unavailable. The source tree kept
    the bug invisible because its fallback resolves there.
    """
    candidates = [
        _PACKAGED_SCHEMA_PATH,
        _SCHEMA_PATH,
        # One level further up, for a checkout nested one directory deeper.
        Path(__file__).parent.parent.parent.parent.parent
        / "schemas"
        / "event-envelope-v1"
        / "envelope-v1.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "envelope-v1.json not found. Expected at "
        f"{_PACKAGED_SCHEMA_PATH} (installed) or "
        "schemas/event-envelope-v1/envelope-v1.json (source checkout)."
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
