# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Behavioral fingerprint over a sealed run (ADR-0147 D5 / NF-155).

A behavioral fingerprint is a **deterministic, offline-reproducible signature** over what an
agent *did* — its canonicalized trajectory, the mix of tools it reached for, and the profile of
its quality scores. Two runs of the same agent against the same task should fingerprint
identically even when the transcript differs in ways that carry no meaning; when the signature
moves, something about the behaviour moved with it.

**It reuses the C3 canonicalizer** (:mod:`novafabric.replay.equivalence.canonicalize`, NF-128)
rather than normalizing trajectories a second way. That is the point of D3's *"one verdict
engine, many consumers"* rule applied to canonicalization: a second normalizer would drift from
the first, and the two would disagree about whether a run had changed.

Three properties decide whether this is worth anything, and **two of them fail quietly**:

- **Deterministic** — a signature that varies per call makes every run look shifted.
- **Stable across benign non-determinism** — a collapsed idempotent retry or a reordered pair of
  commutable calls must not move the signature, or the fingerprint measures transcript noise.
- **Version-sensitive** — the signature covers this module's version *and* the canonicalization
  ``rules_version``. Without that, changing the canonicalization rules silently reads as "no
  shift" when in fact nothing is comparable any more.

**A digest has no metric**, so ``distance`` is never computed from signatures: it is computed
over the basis components the fingerprints carry. Comparing two hex strings can only ever answer
same/different, which is the ``shifted`` boolean — the ADR asks for both, so the record keeps the
features that make the number possible.

Like every detector in this package, a fingerprint is an **observation surfaced for review, not
a verdict**: a shift is not a regression, and there is deliberately no ``regressed``/``failed``
field. Scores are taken as **higher-is-better and bounded ``[0, 1]``** (a judge score or a pass
rate); an out-of-range value is refused rather than clamped, because a silent clamp makes a
5-point Likert score read as a saturated 1.0.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from novafabric.replay.equivalence.canonicalize import ToolCall, canonicalize

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "fingerprint"

#: Bumped whenever the *basis* or the digest construction changes. It is inside the digest, so
#: a fingerprint from an older construction can never compare equal to a newer one by accident.
FINGERPRINT_VERSION = "nf155-v1"

#: Score statistics are rounded before they enter the digest so that float representation noise
#: cannot change a run's identity. The distance is computed from the same rounded values, so the
#: number a caller reads is the number the signature was built from.
SCORE_PRECISION = 6

#: The three basis components, in the ADR's vocabulary. A component that could not be observed is
#: absent from a record's ``basis`` rather than present-and-empty.
BASIS_TRAJECTORY = "trajectory"
BASIS_TOOL_MIX = "tool-mix"
BASIS_SCORE_PROFILE = "score-profile"


class FingerprintError(ValueError):
    """Raised when a fingerprint cannot be built from the inputs supplied."""


class IncomparableFingerprintsError(FingerprintError):
    """Raised when two fingerprints were not built the same way.

    Refused rather than scored: a distance between a run canonicalized under one rule set and a
    baseline canonicalized under another is a number that looks precise and means nothing.
    """


class ScoreProfile(BaseModel):
    """Summary of a run's quality scores. ``n`` travels with the mean deliberately.

    A mean over one sample and a mean over a hundred are not the same evidence, and a record that
    reported only the mean would let them compare as equals.
    """

    model_config = ConfigDict(frozen=True)

    n: int = Field(ge=1)
    mean: float
    minimum: float
    maximum: float


class BehavioralFingerprint(BaseModel):
    """One run's behavioral signature plus the basis it was computed from.

    The features are kept, not just the digest, because ``distance`` is computed from them —
    see the module docstring. ``basis`` lists only the components that were actually observed.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    signature: str
    basis: list[str]
    fingerprint_version: str = FINGERPRINT_VERSION
    schema_version: str = SCHEMA_VERSION
    #: The C3 canonicalizer's rule-set version, carried so a rules change is visible.
    rules_version: str
    #: Canonicalized call keys, in order — the trajectory component.
    trajectory: list[str] = Field(default_factory=list)
    #: Tool name → number of canonicalized calls. Counts, not proportions: proportions are a
    #: float view derived for the distance, while identity is exact.
    tool_counts: dict[str, int] = Field(default_factory=dict)
    score_profile: ScoreProfile | None = None


class ComponentDistance(BaseModel):
    """One basis component's contribution to the distance."""

    model_config = ConfigDict(frozen=True)

    component: str
    distance: float


class FingerprintComparison(BaseModel):
    """A fingerprint measured against a baseline fingerprint.

    ``distance`` and ``shifted`` are ``None`` together when no component is present on both
    sides. "We could not compare these" must not serialise as "they matched" — the same rule the
    canary record follows for an unknown baseline stack.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    signature: str
    baseline_signature: str
    basis: list[str]
    components: list[ComponentDistance] = Field(default_factory=list)
    threshold: float
    distance: float | None = None
    shifted: bool | None = None
    #: Present only when the comparison could not produce a distance, saying why.
    note: str | None = None
    # Intentionally NO regressed/failed/ok field — a shift is an observation, not a verdict.


# ── Building a fingerprint ────────────────────────────────────────────────


def _score_profile(scores: Sequence[float]) -> ScoreProfile:
    values = [float(s) for s in scores]
    for value in values:
        if not 0.0 <= value <= 1.0:
            raise FingerprintError(
                f"score {value} is outside [0, 1]; scores are higher-is-better and bounded "
                "(invert an error rate, and rescale a Likert score, before passing it) — "
                "an out-of-range score is refused rather than clamped"
            )
    return ScoreProfile(
        n=len(values),
        mean=round(sum(values) / len(values), SCORE_PRECISION),
        minimum=round(min(values), SCORE_PRECISION),
        maximum=round(max(values), SCORE_PRECISION),
    )


def _signature(
    *,
    rules_version: str,
    basis: Sequence[str],
    trajectory: Sequence[str],
    tool_counts: Mapping[str, int],
    score_profile: ScoreProfile | None,
) -> str:
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "rules_version": rules_version,
        "basis": list(basis),
        "trajectory": list(trajectory),
        "tool_counts": dict(sorted(tool_counts.items())),
        "score_profile": score_profile.model_dump() if score_profile else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprint_run(
    run_id: str,
    calls: Iterable[ToolCall] | Iterable[Mapping[str, Any]] = (),
    *,
    scores: Sequence[float] | None = None,
    commutable: Iterable[str] = (),
    idempotent: Iterable[str] = (),
    rules: Iterable[str] | None = None,
) -> BehavioralFingerprint:
    """Build the behavioral fingerprint of one run.

    Args:
        run_id: the run this fingerprint describes.
        calls: the observed trajectory — :class:`ToolCall`\\ s or ``{name, arguments}`` mappings.
        scores: per-step or per-run quality scores, higher-is-better in ``[0, 1]``.
        commutable: tool names whose relative order carries no meaning (passed to C3).
        idempotent: tool names whose consecutive repeat is a retry (passed to C3).
        rules: canonicalization rule names; C3's defaults when omitted.

    Raises:
        FingerprintError: if nothing observable was supplied, or a score is out of range.

    An **entirely empty basis is refused**: a signature over nothing compares equal to every
    other nothing, so a run about which nothing was observed would report "behaviour unchanged"
    rather than "nothing was measured".
    """
    tool_calls = [
        c
        if isinstance(c, ToolCall)
        else ToolCall(name=str(c["name"]), arguments=dict(c.get("arguments") or {}))
        for c in calls
    ]
    result = canonicalize(
        tool_calls, rules=rules, commutable=commutable, idempotent=idempotent
    )

    trajectory = [c.key() for c in result.calls]
    tool_counts: dict[str, int] = {}
    for call in result.calls:
        tool_counts[call.name] = tool_counts.get(call.name, 0) + 1

    profile = _score_profile(scores) if scores else None

    basis: list[str] = []
    if trajectory:
        basis.append(BASIS_TRAJECTORY)
        basis.append(BASIS_TOOL_MIX)
    if profile is not None:
        basis.append(BASIS_SCORE_PROFILE)
    if not basis:
        raise FingerprintError(
            f"run {run_id!r} has no observable behaviour to fingerprint: no tool calls and no "
            "scores. A signature over nothing compares equal to every other nothing, which would "
            "read as 'behaviour unchanged' when nothing was measured."
        )

    return BehavioralFingerprint(
        run_id=run_id,
        signature=_signature(
            rules_version=result.rules_version,
            basis=basis,
            trajectory=trajectory,
            tool_counts=tool_counts,
            score_profile=profile,
        ),
        basis=basis,
        rules_version=result.rules_version,
        trajectory=trajectory,
        tool_counts=tool_counts,
        score_profile=profile,
    )


# ── Comparing two fingerprints ────────────────────────────────────────────


def _normalized_edit_distance(a: Sequence[str], b: Sequence[str]) -> float:
    """Levenshtein distance over two call-key sequences, divided by the longer length.

    Bounded ``[0, 1]``: identical sequences give ``0.0``, and a sequence sharing nothing with a
    same-length other gives ``1.0``.
    """
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    previous = list(range(len(b) + 1))
    for i, item_a in enumerate(a, start=1):
        current = [i]
        for j, item_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (item_a != item_b),
                )
            )
        previous = current
    return previous[-1] / max(len(a), len(b))


def _tool_mix_distance(a: Mapping[str, int], b: Mapping[str, int]) -> float:
    """Total-variation distance between two tool-mix distributions, bounded ``[0, 1]``.

    A *mix* is a proportion, so a run that did twice as much of everything has not changed its
    mix. Absolute volume is a different signal and belongs to the trajectory component.
    """
    total_a = sum(a.values())
    total_b = sum(b.values())
    if total_a == 0 or total_b == 0:
        return 1.0
    names = set(a) | set(b)
    return 0.5 * sum(
        abs(a.get(name, 0) / total_a - b.get(name, 0) / total_b) for name in names
    )


def compare_fingerprints(
    fingerprint: BehavioralFingerprint,
    baseline: BehavioralFingerprint,
    *,
    threshold: float,
) -> FingerprintComparison:
    """Measure *fingerprint* against *baseline*.

    ``threshold`` is required rather than defaulted: what counts as a shift is the caller's
    policy, and a hidden default would make that policy look like a property of the data.

    Only components present on **both** sides contribute — a component missing on one side is not
    a distance of zero, and averaging it in as zero would dilute a real shift towards "unchanged".
    The distance is the unweighted mean of the contributing components.

    Raises:
        IncomparableFingerprintsError: if the two were built under different fingerprint or
            canonicalization rule versions.
        FingerprintError: if ``threshold`` is outside ``[0, 1]``.
    """
    if not 0.0 <= threshold <= 1.0:
        raise FingerprintError(
            f"threshold {threshold} is outside [0, 1]; component distances are bounded, so a "
            "threshold outside that range can never fire (or always fires)"
        )
    if fingerprint.fingerprint_version != baseline.fingerprint_version:
        raise IncomparableFingerprintsError(
            "fingerprint versions differ "
            f"({fingerprint.fingerprint_version} vs {baseline.fingerprint_version}); "
            "re-fingerprint the baseline rather than comparing across constructions"
        )
    if fingerprint.rules_version != baseline.rules_version:
        raise IncomparableFingerprintsError(
            "canonicalization rule versions differ "
            f"({fingerprint.rules_version} vs {baseline.rules_version}); a distance across two "
            "canonicalizations looks precise and means nothing — re-fingerprint the baseline"
        )

    components: list[ComponentDistance] = []
    shared = [b for b in fingerprint.basis if b in baseline.basis]
    if BASIS_TRAJECTORY in shared:
        components.append(
            ComponentDistance(
                component=BASIS_TRAJECTORY,
                distance=_normalized_edit_distance(
                    fingerprint.trajectory, baseline.trajectory
                ),
            )
        )
    if BASIS_TOOL_MIX in shared:
        components.append(
            ComponentDistance(
                component=BASIS_TOOL_MIX,
                distance=_tool_mix_distance(
                    fingerprint.tool_counts, baseline.tool_counts
                ),
            )
        )
    if BASIS_SCORE_PROFILE in shared:
        assert fingerprint.score_profile is not None  # implied by the basis
        assert baseline.score_profile is not None
        components.append(
            ComponentDistance(
                component=BASIS_SCORE_PROFILE,
                distance=abs(
                    fingerprint.score_profile.mean - baseline.score_profile.mean
                ),
            )
        )

    if not components:
        return FingerprintComparison(
            run_id=fingerprint.run_id,
            signature=fingerprint.signature,
            baseline_signature=baseline.signature,
            basis=shared,
            components=[],
            threshold=threshold,
            distance=None,
            shifted=None,
            note=(
                "no basis component is present on both sides "
                f"({fingerprint.basis} vs {baseline.basis}); distance is unknown, which is not "
                "the same as unchanged"
            ),
        )

    distance = sum(c.distance for c in components) / len(components)
    return FingerprintComparison(
        run_id=fingerprint.run_id,
        signature=fingerprint.signature,
        baseline_signature=baseline.signature,
        basis=shared,
        components=components,
        threshold=threshold,
        distance=distance,
        shifted=distance > threshold,
    )


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], comparison: FingerprintComparison | None
) -> dict[str, Any]:
    """Attach *comparison* to a capsule dict additively; returns a new dict.

    Writes nothing when *comparison* is None: a run with no fingerprint comparison must stay
    byte-identical to one captured before this feature existed.
    """
    if comparison is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = comparison.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> FingerprintComparison | None:
    """Read a fingerprint comparison back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return FingerprintComparison.model_validate(block)
    except ValueError as exc:
        raise FingerprintError(
            f"capsule holds an invalid fingerprint facet: {exc}"
        ) from exc


__all__ = [
    "BASIS_SCORE_PROFILE",
    "BASIS_TOOL_MIX",
    "BASIS_TRAJECTORY",
    "FACET_NAME",
    "FINGERPRINT_VERSION",
    "SCHEMA_VERSION",
    "BehavioralFingerprint",
    "ComponentDistance",
    "FingerprintComparison",
    "FingerprintError",
    "IncomparableFingerprintsError",
    "ScoreProfile",
    "attach_facet",
    "compare_fingerprints",
    "facet_from_capsule",
    "fingerprint_run",
]
