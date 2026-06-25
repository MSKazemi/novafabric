"""ADR-0095 slice C1 — resolver/branch coverage: seal, criterion-binding, sign, edges."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem
from novafabric.safetycase.compiler import (
    SAFETY_CASE_PREDICATE_TYPE,
    SafetyCaseCompiler,
)
from novafabric.safetycase.models import BackingState, EvidenceKind
from novafabric.safetycase.templates import ClaimSpec


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _capsule(tmp_path: Path, name: str = "01HXAY7M5JZ8R7K4P9DPBYK2WX") -> Path:
    cap = tmp_path / name
    cap.mkdir()
    (cap / "capsule.yaml").write_text(f"run_id: {name}\n")
    return cap


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


class TestSign:
    def test_sign_round_trips(self, tmp_path: Path, signer: LocalSigner) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "attestations" / "reperformance.json",
            {"run_id": cap.name, "match": "exact"},
        )
        compiler = SafetyCaseCompiler()
        case = compiler.build(cap, "clymer-generic-v0")
        envelope = compiler.sign(case, signer)

        def verify_fn(pae: bytes, sig: bytes) -> bool:
            return verify_with_pem(signer.public_pem, pae, sig)

        statement = dsse_verify(envelope, verify_fn)
        assert statement["predicateType"] == SAFETY_CASE_PREDICATE_TYPE
        # in-toto subject digest equals the case_hash hex
        assert statement["subject"][0]["digest"]["sha256"] == case.case_hash.removeprefix(
            "sha256:"
        )

    def test_build_accepts_signer_arg_without_signing(
        self, tmp_path: Path, signer: LocalSigner
    ) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0", signer=signer)
        assert case.case_hash


class TestSealResolver:
    def test_seal_bundle_supported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(cap / "seal-bundle.json", {"sig": "x"})
        # use a template whose leaf accepts seal
        spec = ClaimSpec.model_validate(
            {
                "id": "C-seal",
                "statement": "sealed",
                "claim_kind": "process",
                "binding_spec": {"accepts": ["seal"]},
            }
        )
        resolved = SafetyCaseCompiler()._resolve_leaf(spec, cap)
        assert resolved.evidence is not None
        assert resolved.evidence.evidence_kind is EvidenceKind.SEAL
        assert resolved.state is BackingState.SUPPORTED

    def test_seal_absent_is_unsupported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        spec = ClaimSpec.model_validate(
            {
                "id": "C-seal",
                "statement": "sealed",
                "claim_kind": "process",
                "binding_spec": {"accepts": ["seal"]},
            }
        )
        resolved = SafetyCaseCompiler()._resolve_leaf(spec, cap)
        assert resolved.evidence is None
        assert resolved.state is BackingState.UNSUPPORTED


class TestCriterionBindingResolver:
    def _spec(self) -> ClaimSpec:
        return ClaimSpec.model_validate(
            {
                "id": "C-bind",
                "statement": "bound",
                "claim_kind": "compliance",
                "binding_spec": {"accepts": ["criterion_binding"]},
            }
        )

    def test_all_satisfied_supported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "criterion-bindings.json",
            {"bindings": [{"binding_status": "satisfied"}, {"binding_status": "satisfied"}]},
        )
        resolved = SafetyCaseCompiler()._resolve_leaf(self._spec(), cap)
        assert resolved.state is BackingState.SUPPORTED

    def test_partial_contested(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "criterion-bindings.json",
            {"bindings": [{"binding_status": "satisfied"}, {"binding_status": "partial"}]},
        )
        resolved = SafetyCaseCompiler()._resolve_leaf(self._spec(), cap)
        assert resolved.state is BackingState.CONTESTED

    def test_none_satisfied_unsupported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "criterion-bindings.json",
            {"bindings": [{"binding_status": "unsatisfied"}]},
        )
        resolved = SafetyCaseCompiler()._resolve_leaf(self._spec(), cap)
        assert resolved.state is BackingState.UNSUPPORTED


class TestEdges:
    def test_non_ulid_capsule_name_gets_fresh_run_id(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path, name="not-a-ulid")
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        # subject run_id must still be a valid ULID
        from novafabric.capsule.ulid_util import is_valid_ulid

        assert is_valid_ulid(case.subject.run_ids[0])

    def test_eval_without_process_ref_falls_back_to_file(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "eval-results" / "e.json",
            {
                "judges": ["a", "b"],
                "inter_judge_agreement": 0.9,
                "consensus_verdict": "pass",
                "sample_size": 100,
                "successes": 95,
                # NO process_evidence_ref
            },
        )
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        ev = next(
            n
            for n in case.nodes
            if getattr(n, "evidence_kind", None) == EvidenceKind.JUDGE_AGGREGATE
        )
        # I1 still holds: the file path is used as the process ref
        assert ev.eval_context is not None
        assert ev.eval_context.process_evidence_ref.endswith("e.json")

    def test_corrupt_json_artifact_treated_as_absent(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        (cap / "attestations").mkdir()
        (cap / "attestations" / "reperformance.json").write_text("{ not json")
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        rec = case.get_claim("C-record-integrity")
        assert rec is not None
        assert rec.backing_state is BackingState.UNSUPPORTED

    def test_unknown_replay_match_contests(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        _write(
            cap / "attestations" / "reperformance.json",
            {"run_id": cap.name, "match": "weird-value"},
        )
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        rec = case.get_claim("C-record-integrity")
        assert rec is not None
        assert rec.backing_state is BackingState.CONTESTED

    def test_eval_stored_ci_without_counts_used(self, tmp_path: Path) -> None:
        # no successes/sample_size -> the stored confidence_interval is used directly
        cap = _capsule(tmp_path)
        _write(
            cap / "eval-results" / "e.json",
            {
                "judges": ["a", "b"],
                "inter_judge_agreement": 0.9,
                "consensus_verdict": "pass",
                "confidence_interval": [0.55, 0.85],  # straddles 0.7
                "process_evidence_ref": "tool-calls.jsonl",
            },
        )
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        harm = case.get_claim("C-harm-avoidance")
        assert harm is not None
        assert harm.backing_state is BackingState.CONTESTED

    def test_eval_empty_dir_is_unsupported(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        (cap / "eval-results").mkdir()
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        harm = case.get_claim("C-harm-avoidance")
        assert harm is not None
        assert harm.backing_state is BackingState.UNSUPPORTED
