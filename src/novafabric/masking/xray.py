"""Redaction / Secret-scan X-Ray — JSON projection of a capsule's field-protection state.

ADR-0174 (data slice). Pure and read-only: project a capsule's field structure + redaction /
scan metadata onto a per-field **state** overlay, with a coverage meter and per-state counts.

**Load-bearing invariant (ADR-0174 §1): values are never shown.** This projection carries the
field *path* and its *state* only — never the captured/redacted/scrubbed value. It is enforced
structurally: :class:`FieldXRay` has no value field, and :func:`build_field_xray` copies only
``path`` and ``state`` out of each input record, so a value handed in alongside them cannot
survive into the model or its serialization.

This is the Python/JSON half of feature F-06 — it feeds the ``web/`` heat-overlay tree that
ADR-0174 describes; it is not that view. No capsule-schema change.

Coverage semantics (documented, deliberately conservative):
- **sensitive surface** = ``redacted + secret_scrubbed + unknown`` (fields that are sensitive,
  or of unverified sensitivity because scan metadata was absent — ADR-0174 §5);
- **protected** = ``redacted + secret_scrubbed``;
- ``coverage = protected / sensitive_surface`` (``None`` when the surface is empty).
``clear`` and ``never_captured`` are excluded — they are not sensitive surface. ``unknown``
lowers coverage on purpose: an unverified field is an honest gap, never asserted as ``clear``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel


class FieldState(str, Enum):
    clear = "clear"  # captured verbatim (not sensitive)
    redacted = "redacted"  # value present but masked by policy
    secret_scrubbed = "secret_scrubbed"  # a detected secret, removed
    never_captured = "never_captured"  # not captured by design (ADR-0021 §4)
    unknown = "unknown"  # scan metadata absent — never asserted as clear (ADR-0174 §5)


#: States that make up the "sensitive surface" the coverage meter is computed over.
_SENSITIVE = frozenset({FieldState.redacted, FieldState.secret_scrubbed, FieldState.unknown})
_PROTECTED = frozenset({FieldState.redacted, FieldState.secret_scrubbed})

# MaskingPipeline redaction-strategy / on-error verbs → field state.
_STRATEGY_STATE = {
    "mask": FieldState.redacted,
    "redact": FieldState.redacted,
    "hash": FieldState.redacted,
    "drop": FieldState.secret_scrubbed,
    "remove": FieldState.secret_scrubbed,
    "scrub": FieldState.secret_scrubbed,
}


class FieldXRay(BaseModel):
    path: str
    state: FieldState
    # Intentionally NO value field — the ADR-0174 §1 invariant, enforced at the type level.


class XRayReport(BaseModel):
    capsule_id: str | None = None
    fields: list[FieldXRay]
    counts: dict[str, int]
    sensitive_total: int
    sensitive_protected: int
    coverage: float | None


def build_field_xray(
    records: Iterable[Mapping[str, Any] | FieldXRay], *, capsule_id: str | None = None
) -> XRayReport:
    """Project field ``{path, state}`` records into an :class:`XRayReport`.

    Only ``path`` and ``state`` are read from each record — any other key (e.g. a stray
    ``value`` or ``replacement``) is dropped and can never reach the model or its output.
    """
    fields: list[FieldXRay] = []
    for rec in records:
        if isinstance(rec, FieldXRay):
            fields.append(FieldXRay(path=rec.path, state=rec.state))
        else:
            fields.append(FieldXRay(path=rec["path"], state=FieldState(rec["state"])))

    counts = {state.value: 0 for state in FieldState}
    for f in fields:
        counts[f.state.value] += 1

    sensitive_total = sum(counts[s.value] for s in _SENSITIVE)
    sensitive_protected = sum(counts[s.value] for s in _PROTECTED)
    coverage = (sensitive_protected / sensitive_total) if sensitive_total else None

    return XRayReport(
        capsule_id=capsule_id,
        fields=fields,
        counts=counts,
        sensitive_total=sensitive_total,
        sensitive_protected=sensitive_protected,
        coverage=coverage,
    )


def field_states_from_findings(
    findings: Iterable[Mapping[str, Any]],
) -> list[FieldXRay]:
    """Adapt raw ``MaskingPipeline`` findings to :class:`FieldXRay` records.

    Reads only the field reference (``target_ref``) and the redaction verb
    (``redaction_strategy`` or ``action_taken``) — never the ``match_hash`` digest or the
    ``replacement`` marker. An unrecognised verb maps to ``redacted`` (a finding always means
    something *was* masked; it is never treated as ``clear``).
    """
    out: list[FieldXRay] = []
    for finding in findings:
        path = str(finding.get("target_ref", ""))
        verb = str(finding.get("redaction_strategy") or finding.get("action_taken") or "")
        state = _STRATEGY_STATE.get(verb, FieldState.redacted)
        out.append(FieldXRay(path=path, state=state))
    return out
