"""ADR-0095 slice C1 — compiler: artifact resolution + mechanical backing states.

The compiler walks a template, resolves each leaf to a real capsule artifact, and
assigns backing states *mechanically* from evidence statistics. These tests exercise
each backing-state rule (the heaviest-tested, correctness-critical surface) over tiny
synthetic capsule fixtures.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from novafabric.safetycase.compiler import SafetyCaseCompiler
from novafabric.safetycase.models import (
    BackingState,
    EvidenceKind,
    InferenceType,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "safety-case.schema.json"
)


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    return Draft202012Validator(schema)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    return cap


def _exact_replay(cap: Path) -> None:
    _write(
        cap / "attestations" / "reperformance.json",
        {
            "schema_version": "0.1.0",
            "run_id": cap.name,
            "match": "exact",
            "replay_mode": "exact",
            "outcome_digest": "d" * 64,
        },
    )


def _contested_judge_eval(cap: Path, kappa: float = 0.41) -> None:
    _write(
        cap / "eval-results" / "harm-avoidance.json",
        {
            "suite": "harm-avoidance",
            "judges": ["judge-a", "judge-b", "judge-c"],
            "inter_judge_agreement": kappa,
            "consensus_verdict": "pass (low agreement)",
            "sample_size": 120,
            "successes": 80,
            "confidence_interval": [0.52, 0.79],
            "process_evidence_ref": "tool-calls.jsonl#unsafe",
        },
    )


def _completeness(cap: Path) -> None:
    _write(
        cap / "completeness.json",
        {"schema_version": "0.1.0", "run_id": cap.name, "event_counts": {"x": 1}},
    )


class TestSupportedAndContested:
    def test_exact_replay_is_supported_and_deductive(
        self, tmp_path: Path, schema_validator
    ) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        _contested_judge_eval(cap)
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        schema_validator.validate(case.to_record())

        record_claim = case.get_claim("C-record-integrity")
        assert record_claim is not None
        assert record_claim.backing_state is BackingState.SUPPORTED
        # the replay strategy is deductive for an exact match
        arg = next(n for n in case.nodes if getattr(n, "id", None) == "A-replay")
        assert arg.inference_type is InferenceType.DEDUCTIVE

    def test_low_kappa_forces_contested(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        _contested_judge_eval(cap, kappa=0.41)
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        harm = case.get_claim("C-harm-avoidance")
        assert harm is not None
        assert harm.backing_state is BackingState.CONTESTED
        assert harm.contest_reason is not None
        assert "0.41" in harm.contest_reason or "agreement" in harm.contest_reason

    def test_weakest_child_propagates_to_root(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        _contested_judge_eval(cap, kappa=0.41)
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        root = case.get_claim("C-root")
        assert root is not None
        assert root.backing_state is BackingState.CONTESTED


class TestHighAgreement:
    def test_high_kappa_eval_supported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        # high agreement, CI clearly above threshold -> SUPPORTED
        _write(
            cap / "eval-results" / "harm-avoidance.json",
            {
                "judges": ["a", "b"],
                "inter_judge_agreement": 0.92,
                "consensus_verdict": "pass",
                "sample_size": 200,
                "successes": 190,
                "confidence_interval": [0.91, 0.98],
                "process_evidence_ref": "tool-calls.jsonl",
            },
        )
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        harm = case.get_claim("C-harm-avoidance")
        assert harm is not None
        assert harm.backing_state is BackingState.SUPPORTED


class TestWilsonStraddle:
    def test_ci_straddling_threshold_contested(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        _write(
            cap / "eval-results" / "harm-avoidance.json",
            {
                "judges": ["a", "b"],
                "inter_judge_agreement": 0.9,  # high agreement, not the cause
                "consensus_verdict": "pass",
                "sample_size": 20,
                "successes": 14,
                # Wilson CI straddles the default 0.7 pass threshold
                "confidence_interval": [0.48, 0.85],
                "process_evidence_ref": "tool-calls.jsonl",
            },
        )
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        harm = case.get_claim("C-harm-avoidance")
        assert harm is not None
        assert harm.backing_state is BackingState.CONTESTED


class TestDanglingArtifact:
    def test_missing_artifact_is_unsupported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        # nothing written: every leaf is an honest hole
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        for cid in ("C-record-integrity", "C-harm-avoidance", "C-logging"):
            claim = case.get_claim(cid)
            assert claim is not None
            assert claim.backing_state is BackingState.UNSUPPORTED
        root = case.get_claim("C-root")
        assert root is not None
        assert root.backing_state is BackingState.UNSUPPORTED


class TestMismatchReplay:
    def test_replay_mismatch_contests(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "attestations" / "reperformance.json",
            {"run_id": cap.name, "match": "mismatch", "replay_mode": "exact"},
        )
        _contested_judge_eval(cap, kappa=0.9)
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        rec = case.get_claim("C-record-integrity")
        assert rec is not None
        assert rec.backing_state is BackingState.CONTESTED

    def test_semantic_match_is_inductive(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "attestations" / "reperformance.json",
            {"run_id": cap.name, "match": "semantic-match", "replay_mode": "semantic"},
        )
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        arg = next(n for n in case.nodes if getattr(n, "id", None) == "A-replay")
        assert arg.inference_type is InferenceType.INDUCTIVE
        rec = case.get_claim("C-record-integrity")
        assert rec is not None
        # semantic-match still supports (weaker inference), not contested
        assert rec.backing_state is BackingState.SUPPORTED


class TestResidualRiskHonesty:
    def test_residual_risk_defaults_not_quantified_null(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        assert case.residual_risk is not None
        assert case.residual_risk.value is None
        assert case.residual_risk.basis.value == "not-quantified"
        assert "0.75" in case.residual_risk.caveat or case.residual_risk.caveat


class TestArtifactBinding:
    def test_evidence_ref_digest_matches_file(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _exact_replay(cap)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        ev = next(
            n
            for n in case.nodes
            if getattr(n, "evidence_kind", None) == EvidenceKind.REPLAY_ATTESTATION
        )
        artifact = cap / ev.artifact_ref.path_in_bundle
        actual = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert ev.artifact_ref.sha256 == actual


class TestTemplates:
    @pytest.mark.parametrize(
        "template_id",
        ["clymer-generic-v0", "eu-ai-act-annex-iv-v0", "nist-ai-rmf-v0"],
    )
    def test_all_templates_round_trip(
        self, tmp_path: Path, template_id: str, schema_validator
    ) -> None:
        cap = _capsule(tmp_path)
        _completeness(cap)
        case = SafetyCaseCompiler().build(cap, template_id)
        schema_validator.validate(case.to_record())
        assert case.template_id == template_id
        # case_hash is set and recomputes
        assert case.case_hash == case.compute_case_hash()

    def test_unknown_template_raises(self, tmp_path: Path) -> None:
        from novafabric.safetycase.templates import TemplateError

        with pytest.raises(TemplateError):
            SafetyCaseCompiler().build(_capsule(tmp_path), "no-such-v9")


class TestNoNakedClaims:
    def test_compiled_eval_leaf_always_carries_process_evidence(
        self, tmp_path: Path
    ) -> None:
        cap = _capsule(tmp_path)
        _contested_judge_eval(cap, kappa=0.8)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        ev = next(
            n
            for n in case.nodes
            if getattr(n, "evidence_kind", None) == EvidenceKind.JUDGE_AGGREGATE
        )
        assert ev.eval_context is not None
        assert ev.eval_context.process_evidence_ref
