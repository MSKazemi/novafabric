"""Trust Attestation Radar — JSON projection of a capsule's verification output.

ADR-0173 (data slice). Pure and read-only: given the Trust-Layer verification guarantees a
capsule already produces, project them onto a **fixed-axis radar model**. One axis per
guarantee, each plotted ``0..1`` (booleans → ``0``/``1``; ``redaction_coverage`` is already
a ratio, clamped into range). The filled polygon's shape *is* the verdict.

This is the Python/JSON half of feature F-05: it *feeds* the ``web/`` SVG glyph that
ADR-0173 describes; it is not the glyph. No capsule-schema change, no new dependency —
it consumes existing verification output verbatim (ADR-0173 §98).

Design notes:
- **Fixed axis order** (:data:`AXIS_ORDER`) so a radar's shape is comparable across capsules.
- **``n/a`` is not failure.** A guarantee that is absent/``None`` (e.g. an unsealed capsule
  has no ``signature_ok``) renders as an ``na`` axis with ``value=None``, kept distinct from
  a ``fail`` axis (``value=0.0``).
- **Hard-fail axes** (:data:`HARD_FAIL_AXES`) — the seal integrity anchors. A ``fail`` on any
  of them is ``critical``; a ``fail``/partial on the rest is a ``partial`` dent.
- A capsule with **no seal** (signature ``n/a``) can never be ``attested`` — its verdict is
  ``unsealed`` regardless of how clean policy/eval are.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel

# axis key -> (human label, source flag in the verification mapping)
_AXES: tuple[tuple[str, str, str], ...] = (
    ("signature", "Signature", "signature_ok"),
    ("timestamp", "Timestamp", "timestamp_ok"),
    ("log_integrity", "Log integrity", "log_integrity_ok"),
    ("redaction_coverage", "Redaction coverage", "redaction_coverage"),
    ("secret_scan", "Secret scan", "secret_scan_clean"),
    ("policy", "Policy", "policy_pass"),
    ("eval_gate", "Eval gate", "eval_gate_pass"),
)

#: Fixed v1 axis order — do not reorder (radar shapes are compared like-for-like).
AXIS_ORDER: tuple[str, ...] = tuple(key for key, _, _ in _AXES)

#: Seal integrity anchors — a ``fail`` here is ``critical``, not a mere warning.
HARD_FAIL_AXES: frozenset[str] = frozenset({"signature", "log_integrity"})


class AxisState(str, Enum):
    ok = "ok"  # value == 1.0
    warn = "warn"  # 0 < value < 1
    fail = "fail"  # value == 0.0 (guarantee applicable but not met)
    na = "na"  # not applicable (absent guarantee, e.g. unsealed)


class RadarVerdict(str, Enum):
    attested = "attested"  # sealed and every applicable axis ok
    partial = "partial"  # sealed, no hard-fail, but some axis < 1
    critical = "critical"  # a hard-fail (seal integrity) axis failed
    unsealed = "unsealed"  # no signature guarantee — cannot be attested


class RadarAxis(BaseModel):
    key: str
    label: str
    value: float | None  # 0..1, or None when not applicable
    state: AxisState


class TrustRadar(BaseModel):
    capsule_id: str | None = None
    axes: list[RadarAxis]
    verdict: RadarVerdict


def _to_value(raw: Any) -> float | None:
    """Project one guarantee to a 0..1 reach, or ``None`` when not applicable."""
    if raw is None:
        return None
    if isinstance(raw, bool):  # must precede the numeric check (bool is an int)
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    return None  # unknown type → treat as not applicable, never as a pass


def _to_state(value: float | None) -> AxisState:
    if value is None:
        return AxisState.na
    if value >= 1.0:
        return AxisState.ok
    if value <= 0.0:
        return AxisState.fail
    return AxisState.warn


def _verdict(axes: list[RadarAxis]) -> RadarVerdict:
    by_key = {a.key: a for a in axes}
    signature = by_key["signature"]
    if signature.state is AxisState.na:
        return RadarVerdict.unsealed
    if any(a.key in HARD_FAIL_AXES and a.state is AxisState.fail for a in axes):
        return RadarVerdict.critical
    applicable = [a for a in axes if a.state is not AxisState.na]
    if applicable and all(a.state is AxisState.ok for a in applicable):
        return RadarVerdict.attested
    return RadarVerdict.partial


def build_trust_radar(
    flags: Mapping[str, Any], *, capsule_id: str | None = None
) -> TrustRadar:
    """Project a verification-output mapping onto a fixed-axis :class:`TrustRadar`.

    ``flags`` is a mapping of the seven guarantees (``signature_ok``, ``timestamp_ok``,
    ``log_integrity_ok``, ``redaction_coverage``, ``secret_scan_clean``, ``policy_pass``,
    ``eval_gate_pass``). Any absent or ``None`` guarantee becomes an ``n/a`` axis. The input
    is never mutated and no I/O is performed.
    """
    axes: list[RadarAxis] = []
    for key, label, source in _AXES:
        value = _to_value(flags.get(source))
        axes.append(RadarAxis(key=key, label=label, value=value, state=_to_state(value)))
    return TrustRadar(capsule_id=capsule_id, axes=axes, verdict=_verdict(axes))
