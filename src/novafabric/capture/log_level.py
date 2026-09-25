"""Observation log-level severity for run-capsule records (ADR-0127).

A ``log_level`` is an optional, additive severity tag on the observation-shaped
records inside a Run Capsule (``model-calls.jsonl``, ``tool-calls.jsonl``). It
is a **stored, filterable forensic attribute** — written once at capture and
read back through the ADR-0129 query DSL — never a live signal: NovaFabric
records the level; it does not raise, alert, retry, or block on it.

Contract (``the private design/spec/observation-log-levels-v0.md``):

- Stored domain is exactly ``debug | info | warn | error`` (lower-case).
  Producers normalize framework names *before* writing
  (``WARNING`` → ``warn``, ``CRITICAL``/``FATAL`` → ``error``,
  ``TRACE`` → ``debug``); anything else is rejected at write.
- Absence is preserved — a missing ``log_level`` is read as ``info`` by
  filters but is never back-filled into the stored record.
- When multiple capture-time sources disagree, the **most severe** level wins
  and ``log_level_source`` records the winning source
  (``framework`` > ``span-status`` > ``adapter`` > ``user`` on ties).
- OTel interop (P4): :func:`to_otel_severity` projects a level onto the OTel
  logs ``SeverityNumber`` scale; :func:`from_otel_severity` /
  :func:`from_otel_severity_text` map it back (``None`` — never a guess — on
  malformed input).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

#: Canonical stored domain, in ascending severity order (spec §Enum domain).
LOG_LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")

#: Provenance domain for ``log_level_source`` (spec §Provenance).
LOG_LEVEL_SOURCES: tuple[str, ...] = ("framework", "span-status", "adapter", "user")

#: How a reader treats an absent ``log_level`` (spec §Default & absence).
DEFAULT_LOG_LEVEL = "info"

_SEVERITY_RANK: dict[str, int] = {level: rank for rank, level in enumerate(LOG_LEVELS)}

#: Producer-side normalization of framework level names (spec §Enum domain).
#: Lossy-but-deterministic, mirroring the OTel ``SeverityNumber`` collapse
#: (TRACE 1–4 → debug, FATAL/CRITICAL 21–24 → error).
_LEVEL_ALIASES: dict[str, str] = {
    "warning": "warn",
    "critical": "error",
    "fatal": "error",
    "trace": "debug",
}

LogLevelSource = Literal["framework", "span-status", "adapter", "user"]

#: Outbound OTel projection (spec §OTel mapping, ADR-0127 P4): canonical level →
#: (``SeverityText``, canonical ``SeverityNumber``). Each number is the lowest
#: value of the OTel logs data-model range for that severity (DEBUG 5–8,
#: INFO 9–12, WARN 13–16, ERROR 17–20). Lossy-but-deterministic interop value;
#: the four-value enum stays canonical in the stored capsule.
_OTEL_SEVERITY: dict[str, tuple[str, int]] = {
    "debug": ("DEBUG", 5),
    "info": ("INFO", 9),
    "warn": ("WARN", 13),
    "error": ("ERROR", 17),
}


class InvalidLogLevelError(ValueError):
    """An ADR-0127 severity field carries an out-of-domain value.

    Raised at write time (and for explicit annotations) so a bad value never
    enters the stored capsule; producers normalize with
    :func:`normalize_log_level` first.
    """


class ResolvedLogLevel(BaseModel):
    """A capture-time log level together with its recorded provenance."""

    value: str
    source: LogLevelSource


def validate_log_level(value: Any) -> str:
    """Strict domain check: *value* must be exactly one of :data:`LOG_LEVELS`.

    This is the write-time gate — no normalization happens here (the stored
    record must already be canonical). Raises :class:`InvalidLogLevelError`
    otherwise.
    """
    if isinstance(value, str) and value in _SEVERITY_RANK:
        return value
    raise InvalidLogLevelError(
        f"invalid log_level {value!r}: must be one of {', '.join(LOG_LEVELS)} "
        "(lower-case; normalize producer names with normalize_log_level())"
    )


def normalize_log_level(value: Any) -> str:
    """Normalize a framework-style level name onto the canonical enum.

    Case-insensitive; maps ``WARNING`` → ``warn``, ``CRITICAL``/``FATAL`` →
    ``error``, ``TRACE`` → ``debug``. Raises :class:`InvalidLogLevelError`
    for anything that does not map onto the four-value domain — NovaFabric
    never guesses a severity.
    """
    if isinstance(value, str):
        lowered = value.lower()
        canonical = _LEVEL_ALIASES.get(lowered, lowered)
        if canonical in _SEVERITY_RANK:
            return canonical
    raise InvalidLogLevelError(
        f"cannot normalize log level {value!r} onto {', '.join(LOG_LEVELS)}"
    )


def severity_rank(level: str) -> int:
    """Ascending severity rank of a canonical level (``debug`` = 0)."""
    return _SEVERITY_RANK[validate_log_level(level)]


def most_severe(*levels: str | None) -> str | None:
    """The most severe of the given canonical levels; ``None`` if none given."""
    present = [validate_log_level(level) for level in levels if level is not None]
    if not present:
        return None
    return max(present, key=lambda level: _SEVERITY_RANK[level])


def log_level_from_span_status(status: str) -> str | None:
    """Inbound OTel span-status mapping (spec §OTel mapping).

    ``ERROR`` → ``error``; ``OK`` → ``info``; ``UNSET`` (or anything else)
    sets nothing from the span alone — a framework or adapter may still
    supply a level.
    """
    if status == "ERROR":
        return "error"
    if status == "OK":
        return DEFAULT_LOG_LEVEL
    return None


def resolve_log_level(
    framework: str | None = None,
    span_status: str | None = None,
    adapter: str | None = None,
    user: str | None = None,
) -> ResolvedLogLevel | None:
    """Resolve the capture-time level from the available sources.

    ``framework``/``adapter``/``user`` are level names (normalized here);
    ``span_status`` is an OTel span status (``ERROR``/``OK``/``UNSET``).
    The **most severe** candidate wins; on a tie the higher-priority source
    (``framework`` > ``span-status`` > ``adapter`` > ``user``) is recorded.
    Returns ``None`` when no source supplies a level — absence is preserved.

    Raises:
        InvalidLogLevelError: an explicitly supplied level name does not
            normalize onto the canonical domain.
    """
    candidates: list[tuple[str, LogLevelSource]] = []
    if framework is not None:
        candidates.append((normalize_log_level(framework), "framework"))
    if span_status is not None:
        from_span = log_level_from_span_status(span_status)
        if from_span is not None:
            candidates.append((from_span, "span-status"))
    if adapter is not None:
        candidates.append((normalize_log_level(adapter), "adapter"))
    if user is not None:
        candidates.append((normalize_log_level(user), "user"))
    if not candidates:
        return None
    # Priority order is preserved by construction; strict ">" keeps the
    # earlier (higher-priority) source on severity ties.
    value, source = candidates[0]
    for cand_value, cand_source in candidates[1:]:
        if _SEVERITY_RANK[cand_value] > _SEVERITY_RANK[value]:
            value, source = cand_value, cand_source
    return ResolvedLogLevel(value=value, source=source)


class OtelSeverity(BaseModel):
    """The OTel logs ``SeverityText`` / ``SeverityNumber`` pair for a level."""

    text: str
    number: int


def to_otel_severity(level: str) -> OtelSeverity:
    """Project a canonical ``log_level`` onto the OTel ``SeverityNumber`` scale.

    Outbound half of the spec's *OTel mapping* table (ADR-0127 P4):
    ``debug`` → ``DEBUG``/5, ``info`` → ``INFO``/9, ``warn`` → ``WARN``/13,
    ``error`` → ``ERROR``/17 (the canonical — lowest — number of each range).
    No normalization happens here: *level* must already be canonical.

    Raises:
        InvalidLogLevelError: *level* is not one of :data:`LOG_LEVELS`.
    """
    text, number = _OTEL_SEVERITY[validate_log_level(level)]
    return OtelSeverity(text=text, number=number)


#: Inbound OTel logs ``SeverityNumber`` ranges (spec §OTel mapping, ADR-0127 P4):
#: inclusive ``(low, high, level)``. TRACE (1–4) collapses to ``debug`` and FATAL
#: (21–24) to ``error`` — lossy-but-deterministic, the inverse of
#: :func:`to_otel_severity` on every canonical number.
_OTEL_SEVERITY_RANGES: tuple[tuple[int, int, str], ...] = (
    (1, 4, "debug"),  # TRACE
    (5, 8, "debug"),  # DEBUG
    (9, 12, "info"),  # INFO
    (13, 16, "warn"),  # WARN
    (17, 20, "error"),  # ERROR
    (21, 24, "error"),  # FATAL
)

#: OTel ``SeverityText`` short names with an optional ``2``–``4`` sub-level suffix
#: (``WARN2``, ``FATAL4``) — the only suffixed spellings accepted inbound.
_OTEL_SHORT_NAME_RE = re.compile(r"^(trace|debug|info|warn|error|fatal)[234]$")

#: Longest ``SeverityText`` considered at all (bounded work on foreign input).
_MAX_SEVERITY_TEXT_LEN = 32


def from_otel_severity(number: Any) -> str | None:
    """Map an OTel ``SeverityNumber`` back onto the canonical ``log_level``.

    Inbound half of the spec's *OTel mapping* table (ADR-0127 P4): 1–4
    (TRACE) and 5–8 (DEBUG) → ``debug``, 9–12 → ``info``, 13–16 → ``warn``,
    17–20 (ERROR) and 21–24 (FATAL) → ``error``. The inverse of
    :func:`to_otel_severity` on each canonical number.

    Returns ``None`` — never a guess — for anything that is not an ``int`` in
    ``1..24``: ``0`` (``SEVERITY_NUMBER_UNSPECIFIED``), out-of-range numbers,
    ``bool``, ``float``, and numeric strings alike. Foreign OTLP input is data,
    so this never raises.
    """
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    for low, high, level in _OTEL_SEVERITY_RANGES:
        if low <= number <= high:
            return level
    return None


def from_otel_severity_text(text: Any) -> str | None:
    """Map an OTel ``SeverityText`` onto the canonical ``log_level``, or ``None``.

    Accepts the canonical names, the producer aliases of
    :func:`normalize_log_level` (``WARNING``, ``CRITICAL``, ``FATAL``,
    ``TRACE``; case-insensitive) and the OTel short names with a ``2``–``4``
    sub-level suffix (``WARN2`` → ``warn``). Anything else — including
    non-strings and over-long values — yields ``None``; it never raises.
    Callers prefer ``SeverityNumber`` (the normalized OTel field) and use the
    text only when no number is present.
    """
    if not isinstance(text, str) or not text or len(text) > _MAX_SEVERITY_TEXT_LEN:
        return None
    lowered = text.strip().lower()
    if _OTEL_SHORT_NAME_RE.match(lowered):
        lowered = lowered[:-1]
    try:
        return normalize_log_level(lowered)
    except InvalidLogLevelError:
        return None


def validate_severity_fields(record: dict[str, Any]) -> None:
    """Write-time gate for the three ADR-0127 fields on an observation record.

    Accepts records that omit all three fields (absence = today's behavior);
    rejects an out-of-domain ``log_level`` or ``log_level_source`` and a
    non-string ``status_message`` (``None`` is allowed) with
    :class:`InvalidLogLevelError`, so a bad value never enters the capsule.
    """
    if "log_level" in record:
        validate_log_level(record["log_level"])
    if "log_level_source" in record:
        source = record["log_level_source"]
        if source not in LOG_LEVEL_SOURCES:
            raise InvalidLogLevelError(
                f"invalid log_level_source {source!r}: must be one of "
                f"{', '.join(LOG_LEVEL_SOURCES)}"
            )
    if "status_message" in record:
        message = record["status_message"]
        if message is not None and not isinstance(message, str):
            raise InvalidLogLevelError(
                f"invalid status_message of type {type(message).__name__}: "
                "must be a string or null"
            )
