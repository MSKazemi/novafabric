"""ADR-0095 slice C0 — CAE model serialisation, the I1 gauntlet, residual risk.

Every model must serialise to an object that validates against
``schemas/safety-case.schema.json``. The I1 invariant (no naked claims) is tested
from both ends: a naked eval leaf is rejected at construction *and* an eval leaf
missing ``process_evidence_ref`` is a schema violation; seal / energy_receipt /
replay_attestation leaves with a null eval_context are accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from novafabric.capsule.ulid_util import new_ulid
from novafabric.safetycase.models import (
    ArgumentStrategy,
    ArtifactRef,
    BackingState,
    Claim,
    ClaimKind,
    Confidence,
    ConfidenceMethod,
    EvalContext,
    Evidence,
    EvidenceKind,
    InferenceType,
    ProducerInfo,
    ResidualRisk,
    ResidualRiskBasis,
    SafetyCase,
    Subject,
    SubjectKind,
    canonical_json,
    not_quantified_risk,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "safety-case.schema.json"
)


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _hash() -> str:
    return "sha256:" + "a" * 64


def _replay_evidence(node_id: str = "E-replay") -> Evidence:
    return Evidence(
        id=node_id,
        evidence_kind=EvidenceKind.REPLAY_ATTESTATION,
        artifact_ref=ArtifactRef(
            kind="reperformance",
            path_in_bundle="attestations/reperformance.intoto.jsonl",
            sha256=_hash(),
        ),
        attestation_ref="attestations/reperformance.intoto.jsonl",
    )


def _eval_context() -> EvalContext:
    return EvalContext(
        judges=["judge-a", "judge-b", "judge-c"],
        inter_judge_agreement=0.41,
        consensus_verdict="pass (low agreement)",
        sample_size=120,
        confidence_interval=(0.52, 0.79),
        process_evidence_ref="run-capsule/tool-calls.jsonl#unsafe",
    )


def _minimal_case(nodes: list, residual: ResidualRisk | None = None) -> SafetyCase:
    return SafetyCase(
        case_id=new_ulid(),
        created_at="2026-06-19T12:00:00Z",
        created_by=ProducerInfo(name="novafabric", version="1.0.0"),
        template_id="clymer-generic-v0",
        subject=Subject(
            kind=SubjectKind.RUN_CAPSULE,
            run_ids=[new_ulid()],
            capsule_hash=_hash(),
        ),
        top_claim_id=nodes[0].id,
        nodes=nodes,
        residual_risk=residual,
    ).with_case_hash()


# --- I1 gauntlet -------------------------------------------------------------


class TestInvariantI1:
    def test_naked_eval_leaf_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError, match="invariant I1"):
            Evidence(
                id="E-eval",
                evidence_kind=EvidenceKind.EVAL_RESULT,
                artifact_ref=ArtifactRef(kind="eval_result", sha256=_hash()),
                eval_context=None,
            )

    def test_judge_aggregate_without_context_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invariant I1"):
            Evidence(
                id="E-eval",
                evidence_kind=EvidenceKind.JUDGE_AGGREGATE,
                artifact_ref=ArtifactRef(kind="eval_result", sha256=_hash()),
            )

    def test_eval_leaf_with_context_accepted(self, schema_validator) -> None:
        ev = Evidence(
            id="E-eval",
            evidence_kind=EvidenceKind.EVAL_RESULT,
            artifact_ref=ArtifactRef(kind="eval_result", sha256=_hash()),
            eval_context=_eval_context(),
        )
        # round-trips through the case and validates
        claim = Claim(
            id="C-root",
            statement="x",
            claim_kind=ClaimKind.SAFETY,
            backing_state=BackingState.CONTESTED,
            evidence_ids=["E-eval"],
            contest_reason="low kappa",
        )
        case = _minimal_case([claim, ev])
        schema_validator.validate(case.to_record())

    def test_non_eval_leaf_with_context_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be null"):
            Evidence(
                id="E-seal",
                evidence_kind=EvidenceKind.SEAL,
                artifact_ref=ArtifactRef(kind="seal", sha256=_hash()),
                eval_context=_eval_context(),
            )

    @pytest.mark.parametrize(
        "kind",
        [
            EvidenceKind.SEAL,
            EvidenceKind.ENERGY_RECEIPT,
            EvidenceKind.REPLAY_ATTESTATION,
        ],
    )
    def test_non_eval_leaves_null_context_accepted(
        self, kind: EvidenceKind, schema_validator
    ) -> None:
        ev = Evidence(
            id="E",
            evidence_kind=kind,
            artifact_ref=ArtifactRef(kind=kind.value, sha256=_hash()),
        )
        claim = Claim(
            id="C-root",
            statement="x",
            claim_kind=ClaimKind.PROCESS,
            backing_state=BackingState.SUPPORTED,
            evidence_ids=["E"],
        )
        case = _minimal_case([claim, ev])
        schema_validator.validate(case.to_record())

    def test_schema_itself_rejects_naked_eval_record(self, schema_validator) -> None:
        # bypass the pydantic validator by hand-building the record
        record = {
            "node_type": "evidence",
            "id": "E-eval",
            "evidence_kind": "eval_result",
            "artifact_ref": {
                "kind": "eval_result",
                "path_in_bundle": None,
                "external_id": None,
                "sha256": _hash(),
            },
            "eval_context": None,
            "attestation_ref": None,
        }
        claim = {
            "node_type": "claim",
            "id": "C-root",
            "statement": "x",
            "claim_kind": "safety",
            "backing_state": "CONTESTED",
            "children": [],
            "evidence_ids": ["E-eval"],
            "confidence": {"interval": None, "method": "none", "sprt_verdict": None},
            "contest_reason": "x",
        }
        case_record = {
            "schema_version": "0.1.0",
            "case_id": new_ulid(),
            "created_at": "2026-06-19T12:00:00Z",
            "created_by": {"name": "novafabric", "version": "1.0.0"},
            "template_id": "clymer-generic-v0",
            "subject": {
                "kind": "run-capsule",
                "run_ids": [new_ulid()],
                "capsule_hash": _hash(),
            },
            "top_claim_id": "C-root",
            "nodes": [claim, record],
            "residual_risk": None,
            "case_hash": _hash(),
        }
        errors = list(schema_validator.iter_errors(case_record))
        assert errors, "schema must reject a naked eval leaf"


# --- serialisation / round-trip ---------------------------------------------


class TestSerialisation:
    def test_full_tree_validates(self, schema_validator) -> None:
        root = Claim(
            id="C-root",
            statement="Agent is safe to deploy.",
            claim_kind=ClaimKind.COMPLIANCE,
            backing_state=BackingState.CONTESTED,
            children=["A-1"],
            confidence=Confidence(
                interval=(0.52, 0.79), method=ConfidenceMethod.WILSON
            ),
            contest_reason="sub-claim contested",
        )
        arg = ArgumentStrategy(
            id="A-1",
            strategy="decompose",
            inference_type=InferenceType.INDUCTIVE,
            premises=["covers obligations"],
            defeaters=["a sub-claim is CONTESTED"],
        )
        ev = _replay_evidence()
        case = _minimal_case([root, arg, ev])
        record = case.to_record()
        schema_validator.validate(record)
        assert record["case_hash"].startswith("sha256:")
        assert record["nodes"][0]["confidence"]["interval"] == [0.52, 0.79]

    def test_case_hash_is_deterministic_and_excludes_itself(self) -> None:
        nodes = [
            Claim(
                id="C-root",
                statement="x",
                claim_kind=ClaimKind.SAFETY,
                backing_state=BackingState.SUPPORTED,
                evidence_ids=["E"],
            ),
            _replay_evidence("E"),
        ]
        case = _minimal_case(nodes)
        # recomputing yields the same digest
        assert case.compute_case_hash() == case.case_hash
        # the digest pre-image does not contain the hash field
        assert "case_hash" not in canonical_json(case._body_record())

    def test_get_claim(self) -> None:
        root = Claim(
            id="C-root",
            statement="x",
            claim_kind=ClaimKind.SAFETY,
            backing_state=BackingState.SUPPORTED,
            evidence_ids=["E"],
        )
        case = _minimal_case([root, _replay_evidence("E")])
        assert case.get_claim("C-root") is root
        assert case.get_claim("E") is None
        assert case.get_claim("nope") is None


# --- residual risk -----------------------------------------------------------


class TestResidualRisk:
    def test_default_is_not_quantified_null(self) -> None:
        r = not_quantified_risk("not measured")
        assert r.value is None
        assert r.basis is ResidualRiskBasis.NOT_QUANTIFIED
        assert r.caveat == "not measured"

    def test_value_requires_measured_basis(self) -> None:
        with pytest.raises(ValidationError, match="measured"):
            ResidualRisk(value=0.0075, basis=ResidualRiskBasis.ESTIMATED, caveat="x")

    def test_disavowed_sketch_keeps_value_null(self, schema_validator) -> None:
        r = ResidualRisk(
            value=None,
            basis=ResidualRiskBasis.DISAVOWED_SKETCH,
            caveat="Clymer et al. illustrative 0.75% — disavowed toy number",
        )
        case = _minimal_case(
            [
                Claim(
                    id="C-root",
                    statement="x",
                    claim_kind=ClaimKind.SAFETY,
                    backing_state=BackingState.SUPPORTED,
                    evidence_ids=["E"],
                ),
                _replay_evidence("E"),
            ],
            residual=r,
        )
        schema_validator.validate(case.to_record())
        assert case.to_record()["residual_risk"]["value"] is None

    def test_measured_value_allowed(self) -> None:
        r = ResidualRisk(
            value=0.02, basis=ResidualRiskBasis.MEASURED, caveat="wilson upper bound"
        )
        assert r.value == 0.02
