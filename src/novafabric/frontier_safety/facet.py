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

"""Frontier-safety facet — ADR-0167 D1/P1 (NF-351, NF-353).

Records that a named frontier-safety-framework threshold evaluation (RSP/ASL,
Preparedness, FSF-CCL) *ran* on a run, and which published framework
commitment a run or an observed incident implicates — by reference, into the
optional ``facets.frontier_safety`` block.

Five invariants from ADR-0167 / the NF-351-360 spec §3 shape every choice here:

- **I-1 Record-only, never enforcing.** NovaFabric records that an external
  evaluator, protocol, or human decided something. Nothing here blocks,
  gates, rewrites, quarantines or refuses a workload, and this module exposes
  no entry point that could be mistaken for one.
- **I-2 Additive-first, fail-open.** The facet lives in optional
  ``facets.frontier_safety``. Absent safety material means *no facet* — never
  an empty one, never an exception. A capsule with nothing to say here is
  byte-identical to one captured before this feature existed. Safety evidence
  must never block the very workload it observes.
- **I-3 Never a computed verdict.** NovaFabric must never author a
  frontier-safety verdict. ``verdict`` is ``null``, or it is accompanied by
  the external ``verdict_ref`` + ``verdict_source`` that says who issued it.
  See :class:`ComputedVerdictError` — this is enforced, not merely documented.
- **I-4 Absent is not false.** A missing verdict means *not evaluated*. It
  never means safe and it never means unsafe. No boolean anywhere in this
  module collapses that third state into two.
- **I-5 No payloads.** References, digests, versions and identifiers only.
  Eval results, commitment texts, prompts, weights and red-team transcripts
  never enter the capsule through this path (ADR-0009, ADR-0021 §4).

P1 is the facet, the NF-351 threshold-eval binding and the NF-353 commitment
binding. P2 adds the AI-control-protocol decision (NF-352) and the tripwire
trigger (NF-357), defined in :mod:`novafabric.frontier_safety.control`. P3 adds
the alignment-risk signals — deception signal (NF-354), sandbagging record
(NF-355), autonomy attempt (NF-356) and elicitation record (NF-358) — defined
in :mod:`novafabric.frontier_safety.alignment`. All are carried here as
additive, optional members. The deployment gate (NF-359) and the safety-case
leaves (NF-360) are P4 and deliberately absent — the facet's ``extra="allow"``
config is what lets a later slice add them without a schema break.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from novafabric.frontier_safety import _common
from novafabric.frontier_safety._common import (
    MAX_REF_LENGTH,
    ComputedVerdictError,
    Framework,
    FrontierSafetyError,
    InvalidReferenceError,
    PayloadCaptureError,
    VerdictSource,
    _ExternalVerdict,
    _validate_digest,
    _validate_ref,
    digest_ref,
)
from novafabric.frontier_safety.alignment import (
    AutonomyAttempt,
    DeceptionSignal,
    ElicitationRecord,
    SandbaggingRecord,
)
from novafabric.frontier_safety.control import ControlDecision, TripwireTrigger

# Re-exported for backward compatibility: P1 callers imported these from
# ``novafabric.frontier_safety.facet`` before they moved to ``_common``.
__all__ = [
    "FACET_NAME",
    "MAX_REF_LENGTH",
    "SCHEMA_VERSION",
    "CommitmentBinding",
    "ComputedVerdictError",
    "Framework",
    "FrontierSafetyError",
    "FrontierSafetyFacet",
    "InvalidReferenceError",
    "PayloadCaptureError",
    "SubjectType",
    "ThresholdEval",
    "VerdictSource",
    "VerificationFlags",
    "attach_facet",
    "build_facet",
    "digest_ref",
    "facet_from_capsule",
    "verify_commitment_binding",
    "verify_eval_binding",
]

FACET_NAME = "frontier_safety"
#: Declared here as well as in ``_common`` because the facet module is where
#: readers (and the capsule-facet contract guard) look for a facet's version;
#: it is the same object, so the two cannot drift.
SCHEMA_VERSION: str = _common.SCHEMA_VERSION

#: What a commitment binding is about (NF-353). `incident` is the
#: incident→commitment direction the spec requires; the others cover the
#: run→commitment direction.
SubjectType = Literal["run", "threshold_eval", "incident", "other"]


# ── Objects ───────────────────────────────────────────────────────────────


class ThresholdEval(_ExternalVerdict):
    """A dangerous-capability threshold evaluation that ran (NF-351).

    Records *that* a named RSP/ASL, Preparedness or FSF-CCL threshold eval
    executed, bound to the NF-154 eval-integrity record that proves it, with
    the external evaluator's verdict by reference. It does not record whether
    the threshold was met — that determination belongs to the framework's
    evaluator (ADR-0167 D1, I-3).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    framework: Framework
    framework_version: str
    #: e.g. `ASL-3`, `preparedness.cyber.high`, `fsf.ccl.cbrn`.
    threshold_id: str
    #: Literal True, not bool. The object's whole meaning is "this eval ran";
    #: `eval_ran: false` would be an object asserting the absence of an
    #: evaluation, which under I-2 is recorded by there being no facet at all,
    #: not by a facet claiming a negative.
    eval_ran: Literal[True] = True
    #: Digest of the NF-154 eval-integrity record. A digest, not a URI: this
    #: is the binding that makes "the eval ran" checkable.
    eval_ref: str
    #: Digest of the sealed capsule root this facet is bound into. Optional in
    #: P1: the root is only known once the capsule is sealed, and a facet
    #: built during a run legitimately does not have it yet.
    bound_root: str | None = None

    @field_validator("eval_ref", mode="before")
    @classmethod
    def _check_eval_ref(cls, v: object) -> str:
        return _validate_digest(v, field="eval_ref")

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")

    @field_validator("framework_version", "threshold_id")
    @classmethod
    def _check_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "framework_version and threshold_id must be non-empty; an "
                "unversioned threshold cannot be reconciled against the "
                "published framework text later (NF-351)"
            )
        return v


class CommitmentBinding(_ExternalVerdict):
    """Which published framework commitment a run or incident implicates (NF-353).

    Maps a subject — the run, a threshold eval, or an observed incident — to a
    specific published commitment, by a digest of the commitment text. It
    records *which* commitment is implicated; it never records whether the
    commitment was *satisfied*. There is deliberately no ``satisfied`` field:
    that judgement is the framework owner's, and a boolean here would be
    NovaFabric adjudicating compliance (ADR-0167 D1, I-3).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    framework: Framework
    commitment_id: str
    #: Digest of the published commitment text/section, never the text itself
    #: (I-5). A digest rather than a URI so the commitment a run was held to
    #: cannot be edited out from under the sealed capsule.
    commitment_digest: str
    #: What implicates the commitment: the run, an object, or an incident.
    subject_type: SubjectType = "run"
    #: Digest/URI of that subject. Optional: a run-level binding whose subject
    #: is the capsule itself is already identified by the capsule it lives in,
    #: and requiring a self-reference would be ceremony, not evidence.
    implicated_by_ref: str | None = None

    @field_validator("commitment_digest", mode="before")
    @classmethod
    def _check_commitment_digest(cls, v: object) -> str:
        return _validate_digest(v, field="commitment_digest")

    @field_validator("implicated_by_ref", mode="before")
    @classmethod
    def _check_implicated_by_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field="implicated_by_ref")

    @field_validator("commitment_id")
    @classmethod
    def _check_commitment_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("commitment_id must be non-empty (NF-353)")
        return v


class VerificationFlags(BaseModel):
    """What a verifier actually checked.

    Every flag defaults to ``None``, meaning *not checked* — distinct from
    ``False``, meaning *checked and failed*. P1 performs no seal verification,
    so ``sealed_into_root`` stays ``None``; defaulting it to ``True`` would
    launder an unperformed check into a safety record, and defaulting it to
    ``False`` would slander a seal nobody looked at.
    """

    model_config = ConfigDict(extra="allow")

    eval_ref_resolvable: bool | None = None
    verdict_by_reference: bool | None = None
    sealed_into_root: bool | None = None


class FrontierSafetyFacet(BaseModel):
    """The optional ``facets.frontier_safety`` block (NF-351, I-2).

    Singular ``threshold_eval`` / ``commitment_binding`` / ``control_decision``
    / ``tripwire_trigger`` members, matching the spec §4.1/§4.2/§4.3 wire
    shape exactly, so a document written to the published example validates.

    P2 adds two plural siblings, ``control_decisions`` and
    ``tripwire_triggers``. An external AI-control protocol governs every
    action of a run, so one run legitimately carries a *stream* of decisions,
    and more than one published indicator can fire. The plural lists are what
    :func:`~novafabric.frontier_safety.control.record_control_decision` and
    :func:`~novafabric.frontier_safety.control.record_tripwire_trigger` append
    to; readers should use :meth:`all_control_decisions` and
    :meth:`all_tripwire_triggers`, which merge both shapes so no caller has to
    know which one a producer chose. Both are additive and optional
    (``extra="allow"`` is unchanged), so a P1 facet is still a valid P2 facet.

    P3 follows the same singular + plural pattern for the four alignment-risk
    objects (``deception_signal(s)``, ``sandbagging_record(s)``,
    ``autonomy_attempt(s)``, ``elicitation_record(s)``). The singular members
    are declared, not left to ``extra="allow"``, so a spec-shaped singular
    object is validated — including the payload guard — rather than stored
    unchecked. Read them through the ``all_*`` merge methods.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    threshold_eval: ThresholdEval | None = None
    commitment_binding: CommitmentBinding | None = None
    #: NF-352 — the spec §4.2 single-decision wire shape.
    control_decision: ControlDecision | None = None
    #: NF-352 — every further decision the external protocol produced.
    control_decisions: list[ControlDecision] | None = None
    #: NF-357 — the spec §4.2 single-trigger wire shape.
    tripwire_trigger: TripwireTrigger | None = None
    #: NF-357 — every further published indicator that fired.
    tripwire_triggers: list[TripwireTrigger] | None = None
    #: NF-354 — external scheming / deception / eval-awareness signals.
    deception_signal: DeceptionSignal | None = None
    deception_signals: list[DeceptionSignal] | None = None
    #: NF-355 — sandbagging / under-elicitation evidence.
    sandbagging_record: SandbaggingRecord | None = None
    sandbagging_records: list[SandbaggingRecord] | None = None
    #: NF-356 — sandbox-escape / exfiltration / replication attempts (counts only).
    autonomy_attempt: AutonomyAttempt | None = None
    autonomy_attempts: list[AutonomyAttempt] | None = None
    #: NF-358 — capability elicitation during deployment, as reported.
    elicitation_record: ElicitationRecord | None = None
    elicitation_records: list[ElicitationRecord] | None = None
    verified: VerificationFlags | None = None

    def all_control_decisions(self) -> tuple[ControlDecision, ...]:
        """Every recorded control-protocol decision, singular member first."""
        single = (self.control_decision,) if self.control_decision is not None else ()
        return single + tuple(self.control_decisions or ())

    def all_tripwire_triggers(self) -> tuple[TripwireTrigger, ...]:
        """Every recorded tripwire trigger, singular member first."""
        single = (self.tripwire_trigger,) if self.tripwire_trigger is not None else ()
        return single + tuple(self.tripwire_triggers or ())

    def all_deception_signals(self) -> tuple[DeceptionSignal, ...]:
        """Every recorded NF-354 deception signal, singular member first."""
        single = (self.deception_signal,) if self.deception_signal is not None else ()
        return single + tuple(self.deception_signals or ())

    def all_sandbagging_records(self) -> tuple[SandbaggingRecord, ...]:
        """Every recorded NF-355 sandbagging record, singular member first."""
        single = (self.sandbagging_record,) if self.sandbagging_record is not None else ()
        return single + tuple(self.sandbagging_records or ())

    def all_autonomy_attempts(self) -> tuple[AutonomyAttempt, ...]:
        """Every recorded NF-356 autonomy attempt, singular member first."""
        single = (self.autonomy_attempt,) if self.autonomy_attempt is not None else ()
        return single + tuple(self.autonomy_attempts or ())

    def all_elicitation_records(self) -> tuple[ElicitationRecord, ...]:
        """Every recorded NF-358 elicitation record, singular member first."""
        single = (self.elicitation_record,) if self.elicitation_record is not None else ()
        return single + tuple(self.elicitation_records or ())

    @property
    def has_material(self) -> bool:
        """True when the facet carries safety evidence worth sealing.

        Verification flags alone do not count: a facet holding only "we
        checked nothing" adds a block, a schema version and a seal surface
        while answering none of the questions ADR-0167 exists to answer. An
        empty ``control_decisions: []`` list does not count either, for the
        same reason.
        """
        return bool(
            self.threshold_eval is not None
            or self.commitment_binding is not None
            or self.all_control_decisions()
            or self.all_tripwire_triggers()
            or self.all_deception_signals()
            or self.all_sandbagging_records()
            or self.all_autonomy_attempts()
            or self.all_elicitation_records()
        )


# ── Construction ──────────────────────────────────────────────────────────


def build_facet(
    *,
    threshold_eval: ThresholdEval | None = None,
    commitment_binding: CommitmentBinding | None = None,
    control_decisions: list[ControlDecision] | None = None,
    tripwire_triggers: list[TripwireTrigger] | None = None,
    deception_signals: list[DeceptionSignal] | None = None,
    sandbagging_records: list[SandbaggingRecord] | None = None,
    autonomy_attempts: list[AutonomyAttempt] | None = None,
    elicitation_records: list[ElicitationRecord] | None = None,
    verified: VerificationFlags | None = None,
) -> FrontierSafetyFacet:
    """Assemble the frontier-safety facet from external safety references.

    Keyword-only: every member is optional and the objects are easy to
    transpose positionally, which would silently bind a run to the wrong
    commitment. P2 decisions and triggers, and the P3 alignment-risk objects,
    go into the plural lists; an empty
    list is normalised to absent so it cannot make an empty facet look like
    material (I-2).
    """
    return FrontierSafetyFacet(
        threshold_eval=threshold_eval,
        commitment_binding=commitment_binding,
        control_decisions=list(control_decisions) if control_decisions else None,
        tripwire_triggers=list(tripwire_triggers) if tripwire_triggers else None,
        deception_signals=list(deception_signals) if deception_signals else None,
        sandbagging_records=list(sandbagging_records) if sandbagging_records else None,
        autonomy_attempts=list(autonomy_attempts) if autonomy_attempts else None,
        elicitation_records=list(elicitation_records) if elicitation_records else None,
        verified=verified,
    )


def attach_facet(capsule: dict[str, Any], facet: FrontierSafetyFacet) -> dict[str, Any]:
    """Attach the frontier-safety facet to a capsule dict, additively.

    Writes nothing when the facet carries no safety material: a run with no
    frontier-safety evidence must be byte-identical to one captured before
    this feature existed (I-2, fail-open). Returns a new dict; the input is
    not mutated.
    """
    if not facet.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an unchecked verification flag is *absent*, not `null`,
    # and so `verdict: null` does not have to be re-litigated by every reader:
    # absence and null both mean "not evaluated" (I-4), and the ADR's
    # verdict-by-reference contract is carried by `verdict_ref`.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> FrontierSafetyFacet | None:
    """Read the frontier-safety facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common
    case, and not an error (I-2 fail-open).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return FrontierSafetyFacet.model_validate(block)


# ── Verification ──────────────────────────────────────────────────────────


def verify_eval_binding(threshold_eval: ThresholdEval, eval_record: str | bytes) -> bool:
    """Re-verify a threshold eval's ``eval_ref`` against the NF-154 record.

    Offline and content-only: this checks that the capsule names the eval
    record the caller holds. It says nothing about what the eval concluded —
    that stays behind ``verdict_ref`` with its external source.
    """
    return threshold_eval.eval_ref == digest_ref(eval_record)


def verify_commitment_binding(binding: CommitmentBinding, commitment_text: str | bytes) -> bool:
    """Re-verify a commitment binding against the published commitment text.

    The point of the digest: an auditor can prove which version of the
    published framework text the run was bound to, even after the lab
    republishes it.
    """
    return binding.commitment_digest == digest_ref(commitment_text)
