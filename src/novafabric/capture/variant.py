"""Variant-attribution resolution for run capsules (ADR-0116).

Records *which A/B experiment and variant an external allocator had active*
for a run — as an optional, additive ``variant`` block on the capsule
manifest. This is pure provenance, not experimentation:

- NovaFabric **never** allocates, splits, samples, or randomizes variants,
  and never analyzes experiment outcomes (``strategy/non-goals.md``, "Not a
  feature-flag or experimentation platform").
- Every field is **copied verbatim** from what the caller supplied; nothing
  is derived or defaulted (ADR-0116 D2). In particular ``assignment_source``
  — the field that documents that an *external* system made the decision —
  must itself be supplied by the caller, and ``assigned_at`` is never
  substituted with the capture time.
- Absent inputs ⇒ absent block; a capsule without variant attribution is the
  common case and stays byte-identical to pre-ADR-0116 capsules.

Sources are resolved **atomically per tier** (CLI flags > ``NOVAFABRIC_VARIANT*``
env vars > SDK argument): the winning tier supplies the whole block, so an
experiment id from one source is never paired with a variant id from another.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Env vars consulted when no CLI flag supplies the block (ADR-0116 P2).
VARIANT_ENV_VAR = "NOVAFABRIC_VARIANT"
EXPERIMENT_ENV_VAR = "NOVAFABRIC_VARIANT_EXPERIMENT"
VARIANT_LABEL_ENV_VAR = "NOVAFABRIC_VARIANT_LABEL"
VARIANT_SOURCE_ENV_VAR = "NOVAFABRIC_VARIANT_SOURCE"
VARIANT_ASSIGNED_AT_ENV_VAR = "NOVAFABRIC_VARIANT_ASSIGNED_AT"

#: Keys accepted in a caller-supplied variant mapping (SDK arg / CLI bundle).
_FIELD_KEYS = frozenset(
    {"experiment_id", "variant_id", "assignment_source", "variant_label", "assigned_at"}
)
_ALLOWED_KEYS = _FIELD_KEYS | {"extensions"}
_REQUIRED_KEYS = ("experiment_id", "variant_id", "assignment_source")

VariantSource = Literal["cli-flag", "env-var", "sdk-arg"]


class InvalidVariantAttributionError(ValueError):
    """An explicitly supplied variant attribution is invalid or incomplete.

    Raised for explicit program input (CLI flags, SDK argument) so the
    operator gets immediate feedback *before* any capture starts. Invalid or
    incomplete ambient ``NOVAFABRIC_VARIANT*`` env vars never raise — they are
    warned about and skipped, because a bad shell variable must never block
    the user workload (fail-open).
    """


class VariantAttribution(BaseModel):
    """A resolved ADR-0116 ``variant`` block — verbatim caller-supplied values."""

    experiment_id: str
    variant_id: str
    assignment_source: str
    variant_label: str | None = None
    assigned_at: str | None = None
    extensions: dict[str, Any] | None = None

    def to_manifest_block(self) -> dict[str, Any]:
        """The additive optional ``variant`` manifest block (absent optionals omitted)."""
        block: dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "variant_id": self.variant_id,
            "assignment_source": self.assignment_source,
        }
        if self.variant_label is not None:
            block["variant_label"] = self.variant_label
        if self.assigned_at is not None:
            block["assigned_at"] = self.assigned_at
        if self.extensions is not None:
            block["extensions"] = self.extensions
        return block


def _supplied(value: Any) -> str | None:
    """Normalize one candidate: empty / whitespace-only / non-string ⇒ not supplied."""
    if isinstance(value, str) and value.strip():
        return value  # recorded verbatim — never trimmed or normalized
    return None


def _valid_rfc3339(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _resolve_tier(
    fields: Mapping[str, Any], source: VariantSource
) -> VariantAttribution | None:
    """Validate one tier's field set; behavior depends on how explicit it was."""
    unknown = set(fields) - _ALLOWED_KEYS
    if unknown:
        message = (
            f"unknown variant attribution field(s) {sorted(unknown)} (from {source}): "
            f"allowed keys are {sorted(_ALLOWED_KEYS)}"
        )
        if source == "env-var":  # pragma: no cover - env tier never carries unknown keys
            logger.warning("Ignoring variant attribution from env vars: %s", message)
            return None
        raise InvalidVariantAttributionError(message)

    values = {key: _supplied(fields.get(key)) for key in _FIELD_KEYS}
    extensions = fields.get("extensions")
    if extensions is not None and not isinstance(extensions, Mapping):
        raise InvalidVariantAttributionError(
            f"variant attribution 'extensions' must be a mapping (from {source})"
        )
    if not any(values.values()) and extensions is None:
        return None  # tier supplied nothing — fall through

    missing = [key for key in _REQUIRED_KEYS if values[key] is None]
    if missing:
        # ADR-0116 D2: NovaFabric MUST NOT derive or default any field — in
        # particular assignment_source, which must name the EXTERNAL allocator.
        message = (
            f"incomplete variant attribution (from {source}): missing {missing}. "
            "A variant block requires experiment_id, variant_id, and "
            "assignment_source (the external system that assigned the arm) — "
            "NovaFabric never assigns or defaults them (ADR-0116)."
        )
        if source == "env-var":
            logger.warning("Ignoring variant attribution from env vars: %s", message)
            return None
        raise InvalidVariantAttributionError(message)

    assigned_at = values["assigned_at"]
    if assigned_at is not None and not _valid_rfc3339(assigned_at):
        message = (
            f"invalid variant assigned_at {assigned_at!r} (from {source}): "
            "must be an RFC 3339 timestamp with a UTC offset"
        )
        if source == "env-var":
            # Ambient input: drop only the bad timestamp — the externally
            # decided arm itself is still recorded (fail-open).
            logger.warning("%s — dropping assigned_at, keeping the variant block", message)
            assigned_at = None
        else:
            raise InvalidVariantAttributionError(message)

    return VariantAttribution(
        experiment_id=values["experiment_id"],  # type: ignore[arg-type]
        variant_id=values["variant_id"],  # type: ignore[arg-type]
        assignment_source=values["assignment_source"],  # type: ignore[arg-type]
        variant_label=values["variant_label"],
        assigned_at=assigned_at,
        extensions=dict(extensions) if isinstance(extensions, Mapping) else None,
    )


def resolve_variant_attribution(
    cli_values: Mapping[str, Any] | None = None,
    sdk_values: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> VariantAttribution | None:
    """Resolve the variant attribution block from explicit sources only.

    Precedence mirrors ADR-0126: CLI flags > ``NOVAFABRIC_VARIANT*`` env vars >
    SDK argument — resolved **atomically per tier** (the first tier that
    supplies anything must supply the full required triple and wins wholly).
    Empty / whitespace-only values count as *not supplied*. If no tier
    supplies a block the result is ``None`` and the capsule stays unchanged.

    Raises:
        InvalidVariantAttributionError: A CLI/SDK-supplied block is incomplete
            (missing ``experiment_id`` / ``variant_id`` / ``assignment_source``),
            carries unknown keys, or has a malformed ``assigned_at``. Ambient
            env-var problems are warned about and skipped instead — a bad
            shell variable never blocks the workload.
    """
    env = os.environ if environ is None else environ
    env_fields = {
        "experiment_id": env.get(EXPERIMENT_ENV_VAR),
        "variant_id": env.get(VARIANT_ENV_VAR),
        "assignment_source": env.get(VARIANT_SOURCE_ENV_VAR),
        "variant_label": env.get(VARIANT_LABEL_ENV_VAR),
        "assigned_at": env.get(VARIANT_ASSIGNED_AT_ENV_VAR),
    }
    tiers: list[tuple[Mapping[str, Any], VariantSource]] = [
        (cli_values or {}, "cli-flag"),
        (env_fields, "env-var"),
        (sdk_values or {}, "sdk-arg"),
    ]
    for fields, source in tiers:
        resolved = _resolve_tier(fields, source)
        if resolved is not None:
            return resolved
    return None
