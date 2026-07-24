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

"""Guardrail-decision objects — ADR-0145 D1/P1 (NF-131).

Promotes a runtime ``GuardrailEvent`` to sealed evidence: the same decision,
plus the digest binding, rule/reason provenance and detector attribution the
raw event lacks.

Four invariants from ADR-0145 shape every choice in this module:

- **I-1 Record-only / fail-open.** NovaFabric records that a guardrail fired;
  it never made the decision and never enforced it. Nothing here can block,
  rewrite, or fail a run.
- **I-2 Additive-first.** These objects live in optional ``facets.safety``.
  A capsule without the facet stays valid.
- **I-3 No payloads.** The judged content is represented only by
  ``decision_inputs_digest``. The adversarial prompt, the refused output and
  the detector's weights never enter the capsule through this path.
- **I-4 Verdict-not-adjudicated.** A decision records *what a detector said*,
  with provenance. It never asserts the verdict was correct.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture.events import GuardrailEvent
from novafabric.safety._primitives import DetectorProvenance, digest_bytes
from novafabric.safety.attempts import (
    InjectionAttempt,
    JailbreakAttempt,
    sort_attempts,
)

__all__ = [
    "DetectorProvenance",
    "GuardrailDecision",
    "SafetyFacet",
    "UnmappableOutcomeError",
    "attach_facet",
    "build_facet",
    "decision_from_event",
    "digest_inputs",
    "is_empty",
    "verify_decision_binding",
]

FACET_NAME = "safety"
SCHEMA_VERSION = "0.1.0"

Phase = Literal["input", "output"]
Disposition = Literal["allow", "block", "rewrite"]

#: GuardrailEvent.outcome → guardrail-decision disposition.
#:
#: `error` is deliberately absent. A detector that errored produced no verdict,
#: and mapping it to `allow` would manufacture one — recording a permissive
#: decision nobody made. Under ADR-0145 D5 a missing decision is itself
#: evidence, so an errored detector must leave a hole, not a green light.
_OUTCOME_TO_DISPOSITION: dict[str, Disposition] = {
    "passed": "allow",
    "blocked": "block",
    "modified": "rewrite",
}


#: ``DetectorProvenance`` is defined in ``_primitives`` and re-exported here
#: unchanged. It moved when P2 landed: the facet below has to hold the P2
#: attempt objects, so this module imports ``attempts``, and the detector block
#: both halves need had to live below both. The class, its fields and its
#: config are untouched, and ``from novafabric.safety.decisions import
#: DetectorProvenance`` still resolves to the same object.


class GuardrailDecision(BaseModel):
    """One guardrail decision, bindable into the sealed capsule root (D1)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    decision_id: str
    phase: Phase
    disposition: Disposition
    rule_id: str | None = None
    reason: str | None = None
    #: sha256 over the exact judged content. The content itself is not stored.
    decision_inputs_digest: str | None = None
    detector: DetectorProvenance | None = None
    score: float | None = None
    decided_at: str
    category: str | None = None
    agent_id: str | None = None


def digest_inputs(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of judged content.

    Callers hash the content they judged and keep the content out of the
    capsule (I-3). Returned in the same ``sha256:<hex>`` form the rest of the
    capsule uses, so a verifier does not have to know which subsystem wrote it.
    """
    return digest_bytes(content)


class UnmappableOutcomeError(ValueError):
    """Raised when a GuardrailEvent carries no verdict to record."""


def decision_from_event(
    event: GuardrailEvent,
    *,
    phase: Phase = "input",
    rule_id: str | None = None,
    decision_inputs_digest: str | None = None,
    detector: DetectorProvenance | None = None,
) -> GuardrailDecision:
    """Map a ``GuardrailEvent`` 1:1 into a guardrail-decision object.

    Raises:
        UnmappableOutcomeError: if ``event.outcome`` is ``error``. A detector
            that failed expressed no verdict; see ``_OUTCOME_TO_DISPOSITION``.
    """
    disposition = _OUTCOME_TO_DISPOSITION.get(event.outcome)
    if disposition is None:
        raise UnmappableOutcomeError(
            f"guardrail outcome {event.outcome!r} carries no verdict and cannot "
            "become a decision; record it as a detector failure instead "
            "(ADR-0145 D5 — a missing decision is evidence)"
        )
    return GuardrailDecision(
        decision_id=event.event_id,
        phase=phase,
        disposition=disposition,
        rule_id=rule_id,
        # `details` is opt-in content (ADR-0021) and is NOT copied into
        # `reason`: a reason string travels into evidence exports, and the
        # detector's details may quote the very payload I-3 keeps out.
        reason=None,
        decision_inputs_digest=decision_inputs_digest,
        detector=detector or DetectorProvenance(name=event.guardrail_name),
        score=event.score,
        decided_at=event.timestamp_utc,
        category=event.category,
        agent_id=event.agent_id,
    )


class SafetyFacet(BaseModel):
    """The optional ``facets.safety`` block (I-2).

    P1 carries the decision objects (NF-131); P2 adds injection (NF-132) and
    jailbreak (NF-133) attempts. The remaining ADR-0145 objects — red-team
    (NF-134), crosswalk (NF-135), refusals (NF-136), output verdicts (NF-137),
    bypass (NF-138), escalations (NF-139) — arrive in later phases and are
    absent rather than stubbed: an empty ``bypass`` list in a sealed root would
    read as "the guardrails were checked and none failed open", which is a far
    worse error than a missing key.

    That is the same reason the lists here are *pruned when empty* on the way
    into a capsule (see :func:`attach_facet`). Absent is not false: no
    ``injection`` key means "no injection attempt was recorded", never "no
    injection was attempted".
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    decisions: list[GuardrailDecision] = Field(default_factory=list)
    injection: list[InjectionAttempt] = Field(default_factory=list)
    jailbreak: list[JailbreakAttempt] = Field(default_factory=list)


def build_facet(
    decisions: Iterable[GuardrailDecision],
    *,
    injection: Iterable[InjectionAttempt] = (),
    jailbreak: Iterable[JailbreakAttempt] = (),
) -> SafetyFacet:
    """Assemble the safety facet, each list ordered by its own event time.

    ``decisions`` stays positional so every P1 call site keeps working; the
    P2 attempt lists are keyword-only.
    """
    return SafetyFacet(
        decisions=sorted(decisions, key=lambda d: d.decided_at),
        injection=sort_attempts(injection),
        jailbreak=sort_attempts(jailbreak),
    )


def is_empty(facet: SafetyFacet) -> bool:
    """True when the facet records nothing and must not be written (I-2)."""
    return not (facet.decisions or facet.injection or facet.jailbreak)


def attach_facet(capsule: dict[str, Any], facet: SafetyFacet) -> dict[str, Any]:
    """Attach the safety facet to a capsule dict, additively.

    Writes nothing when there is no safety material at all: a run with no
    guardrail activity must be byte-identical to one captured before this
    feature existed (I-2). Returns a new dict; the input is not mutated.

    Empty object lists are dropped from what is written, so a capsule that
    recorded only decisions does not gain ``"injection": []`` — which a reader
    would reasonably take as "checked, nothing found" (absent is not false).
    It also keeps a P1-era capsule's serialised facet unchanged byte-for-byte
    now that P2 has added fields to the model.
    """
    if is_empty(facet):
        return capsule
    dumped = facet.model_dump(exclude_none=True)
    body = {k: v for k, v in dumped.items() if not (isinstance(v, list) and not v)}
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = body
    out["facets"] = facets
    return out


# ── Verification ──────────────────────────────────────────────────────────


def verify_decision_binding(
    decision: GuardrailDecision, judged_content: str | bytes
) -> bool:
    """Re-verify a decision's digest against the content it claims to have judged.

    Returns False for a decision with no digest. An unbound decision is not
    "trivially valid" — it is precisely the case ADR-0145 wants surfaced, and
    returning True would let a decision with no binding pass a verifier that
    exists to check bindings.
    """
    if not decision.decision_inputs_digest:
        return False
    return decision.decision_inputs_digest == digest_inputs(judged_content)
