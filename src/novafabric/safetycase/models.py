"""Pydantic models for the Claims-Arguments-Evidence (CAE) safety-case tree (ADR-0095, slice C0).

A :class:`SafetyCase` is a flat list of nodes (``Claim`` / ``ArgumentStrategy`` /
``Evidence``) reconstructed into a tree via ``Claim.children`` and
``Claim.evidence_ids``. The models serialise (via :meth:`to_record`) to objects that
validate against ``schemas/safety-case.schema.json``.

Two structural-honesty invariants (ADR-0095):

- **I1 — no naked claims.** An :class:`Evidence` node whose ``evidence_kind`` is
  ``eval_result`` or ``judge_aggregate`` MUST carry a non-null
  :class:`EvalContext` with a ``process_evidence_ref``. This is enforced both by a
  model validator here *and* by a JSON-Schema conditional — a bare pass/fail is a
  schema violation, not a warning.
- Residual risk defaults to ``not-quantified`` / ``null`` and never reproduces the
  literature's disavowed toy numbers as measurements.

The tree carries a ``case_hash`` computed last over the canonical (sorted-key,
compact) serialisation of everything *except* the hash itself.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAFETY_CASE_SCHEMA_VERSION = "0.1.0"

#: evidence kinds that, per invariant I1, MUST carry an eval_context.
EVAL_EVIDENCE_KINDS: frozenset[str] = frozenset({"eval_result", "judge_aggregate"})


class BackingState(str, Enum):
    """Mechanically-assigned backing state for a claim (never editorial)."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTESTED = "CONTESTED"
    UNKNOWN = "UNKNOWN"


class ClaimKind(str, Enum):
    SAFETY = "safety"
    SECURITY = "security"
    CAPABILITY = "capability"
    COMPLIANCE = "compliance"
    PROCESS = "process"


class InferenceType(str, Enum):
    DEDUCTIVE = "deductive"
    INDUCTIVE = "inductive"
    STATISTICAL = "statistical"
    APPEAL_TO_PROCESS = "appeal-to-process"


class ConfidenceMethod(str, Enum):
    WILSON = "wilson"
    SPRT = "sprt"
    NONE = "none"


class EvidenceKind(str, Enum):
    EVAL_RESULT = "eval_result"
    JUDGE_AGGREGATE = "judge_aggregate"
    REPLAY_ATTESTATION = "replay_attestation"
    SEAL = "seal"
    POLICY_DECISION = "policy_decision"
    AUDIT_ENTRY = "audit_entry"
    CRITERION_BINDING = "criterion_binding"
    COMPLETENESS_ASSERTION = "completeness_assertion"
    REDACTION_PROOF = "redaction_proof"
    LINEAGE_ATTESTATION = "lineage_attestation"
    ENERGY_RECEIPT = "energy_receipt"
    OPERATOR_DOCUMENT = "operator_document"


class ResidualRiskBasis(str, Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    DISAVOWED_SKETCH = "disavowed-sketch"
    NOT_QUANTIFIED = "not-quantified"


class SubjectKind(str, Enum):
    RUN_CAPSULE = "run-capsule"
    RUN_CAPSULE_SET = "run-capsule-set"


def _dump(model: BaseModel) -> dict[str, Any]:
    """Serialise a model to a JSON-ready dict (enums → their values)."""
    return model.model_dump(mode="json")


class ProducerInfo(BaseModel):
    """The tool that produced the case."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class Subject(BaseModel):
    """What the case argues about: a single run or a run set."""

    model_config = ConfigDict(extra="forbid")

    kind: SubjectKind
    run_ids: list[str] = Field(min_length=1)
    capsule_hash: str


class Confidence(BaseModel):
    """Statistical confidence attached to a claim."""

    model_config = ConfigDict(extra="forbid")

    interval: tuple[float, float] | None = None
    method: ConfidenceMethod = ConfidenceMethod.NONE
    sprt_verdict: str | None = None

    def to_record(self) -> dict[str, Any]:
        record = _dump(self)
        # tuple → list for JSON Schema (array) compatibility
        if record["interval"] is not None:
            record["interval"] = list(record["interval"])
        return record


class Claim(BaseModel):
    """A proposition with a mechanically-assigned backing state."""

    model_config = ConfigDict(extra="forbid")

    node_type: Literal["claim"] = "claim"
    id: str
    statement: str
    claim_kind: ClaimKind
    backing_state: BackingState
    children: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = Field(default_factory=Confidence)
    contest_reason: str | None = None

    def to_record(self) -> dict[str, Any]:
        record = _dump(self)
        record["confidence"] = self.confidence.to_record()
        return record


class ArgumentStrategy(BaseModel):
    """How children support a parent claim, with explicit defeaters."""

    model_config = ConfigDict(extra="forbid")

    node_type: Literal["argument"] = "argument"
    id: str
    strategy: str
    inference_type: InferenceType
    premises: list[str] = Field(default_factory=list)
    defeaters: list[str] = Field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return _dump(self)


class ArtifactRef(BaseModel):
    """A real bundle artifact bound by sha256."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    path_in_bundle: str | None = None
    external_id: str | None = None
    sha256: str

    def to_record(self) -> dict[str, Any]:
        return _dump(self)


class EvalContext(BaseModel):
    """The I1 anti-naked-claim block: process evidence + multi-judge consensus."""

    model_config = ConfigDict(extra="forbid")

    judges: list[str] = Field(default_factory=list)
    inter_judge_agreement: float | None = None
    consensus_verdict: str
    sample_size: int | None = None
    confidence_interval: tuple[float, float] | None = None
    process_evidence_ref: str = Field(min_length=1)

    def to_record(self) -> dict[str, Any]:
        record = _dump(self)
        if record["confidence_interval"] is not None:
            record["confidence_interval"] = list(record["confidence_interval"])
        return record


class Evidence(BaseModel):
    """A leaf bound to one real artifact; eval evidence MUST carry eval_context (I1)."""

    model_config = ConfigDict(extra="forbid")

    node_type: Literal["evidence"] = "evidence"
    id: str
    evidence_kind: EvidenceKind
    artifact_ref: ArtifactRef
    eval_context: EvalContext | None = None
    attestation_ref: str | None = None

    @model_validator(mode="after")
    def _enforce_i1(self) -> Evidence:
        if self.evidence_kind.value in EVAL_EVIDENCE_KINDS and self.eval_context is None:
            raise ValueError(
                "invariant I1 violated: evidence_kind "
                f"{self.evidence_kind.value!r} requires a non-null eval_context "
                "with a process_evidence_ref (a bare pass/fail is forbidden)"
            )
        if self.evidence_kind.value not in EVAL_EVIDENCE_KINDS and self.eval_context is not None:
            raise ValueError(
                "eval_context must be null for non-eval evidence_kind "
                f"{self.evidence_kind.value!r}"
            )
        return self

    def to_record(self) -> dict[str, Any]:
        record = _dump(self)
        record["artifact_ref"] = self.artifact_ref.to_record()
        record["eval_context"] = (
            self.eval_context.to_record() if self.eval_context is not None else None
        )
        return record


Node = Claim | ArgumentStrategy | Evidence


class ResidualRisk(BaseModel):
    """Honest residual-risk record; defaults to not-quantified/null."""

    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    basis: ResidualRiskBasis = ResidualRiskBasis.NOT_QUANTIFIED
    caveat: str = ""

    @model_validator(mode="after")
    def _value_requires_measured_basis(self) -> ResidualRisk:
        if self.value is not None and self.basis is not ResidualRiskBasis.MEASURED:
            raise ValueError(
                "residual_risk.value may only be non-null when basis is 'measured'; "
                f"got basis={self.basis.value!r}"
            )
        return self

    def to_record(self) -> dict[str, Any]:
        return _dump(self)


def not_quantified_risk(caveat: str = "") -> ResidualRisk:
    """The honest default: no number, not-quantified basis."""
    return ResidualRisk(value=None, basis=ResidualRiskBasis.NOT_QUANTIFIED, caveat=caveat)


def canonical_json(data: Any) -> str:
    """Deterministic JSON serialisation (sorted keys, compact separators).

    Matches the convention used by ``dsse_sign`` / ``outcome_digest_of`` so the
    same logical content always hashes identically.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class SafetyCase(BaseModel):
    """The root CAE tree. ``case_hash`` is computed last over the canonical body."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SAFETY_CASE_SCHEMA_VERSION
    case_id: str
    created_at: str
    created_by: ProducerInfo
    template_id: str
    subject: Subject
    top_claim_id: str
    nodes: list[Node] = Field(min_length=1)
    residual_risk: ResidualRisk | None = None
    case_hash: str = ""

    def _node_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for node in self.nodes:
            records.append(node.to_record())
        return records

    def _body_record(self) -> dict[str, Any]:
        """The full record minus the case_hash field (the digest pre-image)."""
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "created_at": self.created_at,
            "created_by": _dump(self.created_by),
            "template_id": self.template_id,
            "subject": _dump(self.subject),
            "top_claim_id": self.top_claim_id,
            "nodes": self._node_records(),
            "residual_risk": (
                self.residual_risk.to_record() if self.residual_risk is not None else None
            ),
        }

    def compute_case_hash(self) -> str:
        """SHA-256 (``sha256:<hex>``) over the canonical body (excludes case_hash)."""
        digest = hashlib.sha256(canonical_json(self._body_record()).encode()).hexdigest()
        return f"sha256:{digest}"

    def with_case_hash(self) -> SafetyCase:
        """Return a copy with ``case_hash`` set to the computed digest."""
        return self.model_copy(update={"case_hash": self.compute_case_hash()})

    def to_record(self) -> dict[str, Any]:
        """JSON-ready dict that validates against safety-case.schema.json."""
        record = self._body_record()
        record["case_hash"] = self.case_hash
        return record

    def get_claim(self, node_id: str) -> Claim | None:
        for node in self.nodes:
            if isinstance(node, Claim) and node.id == node_id:
                return node
        return None
