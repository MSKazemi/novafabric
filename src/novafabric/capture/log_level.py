"""Observation log-level severity for run-capsule records (ADR-0127).

A ``log_level`` is an optional, additive severity tag on the observation-shaped
records inside a Run Capsule (``model-calls.jsonl``, ``tool-calls.jsonl``). It
is a **stored, filterable forensic attribute** — written once at capture and
read back through the ADR-0129 query DSL — never a live signal: NovaFabric
records the level; it does not raise, alert, retry, or block on it.

Contract (``design/spec/observation-log-levels-v0.md``):

- Stored domain is exactly ``debug | info | warn | error`` (lower-case).
  Producers normalize framework names *before* writing
  (``WARNING`` → ``warn``, ``CRITICAL``/``FATAL`` → ``error``,
  ``TRACE`` → ``debug``); anything else is rejected at write.
- Absence is preserved — a missing ``log_level`` is read as ``info`` by
  filters but is never back-filled into the stored record.
- When multiple capture-time sources disagree, the **most severe** level wins
  and ``log_level_source`` records the winning source
  (``framework`` > ``span-status`` > ``adapter`` > ``user`` on ties).
"""

from __future__ import annotations

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
