"""ADR-0095 slice C1 — verifier: re-verify artifact hashes, recompute case_hash, I1.

The verifier re-checks each Evidence ``artifact_ref`` digest against the capsule,
recomputes the ``case_hash``, and asserts invariant I1 (no naked claims). A tampered
artifact or a mutated case body yields a non-zero exit code; an honest case yields 0.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.safetycase.compiler import SafetyCaseCompiler
from novafabric.safetycase.models import (
    ArtifactRef,
    BackingState,
    Claim,
    ClaimKind,
    Evidence,
    EvidenceKind,
)
from novafabric.safetycase.verifier import verify_exit_code, verify_safety_case


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    _write(
        cap / "attestations" / "reperformance.json",
        {"run_id": cap.name, "match": "exact", "replay_mode": "exact"},
    )
    _write(cap / "completeness.json", {"run_id": cap.name, "event_counts": {}})
    return cap


class TestHonestCase:
    def test_clean_case_verifies(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        verdict = verify_safety_case(case, cap)
        assert verdict.ok
        assert verdict.artifact_failures == []
        assert verdict.case_hash_ok
        assert verdict.i1_ok
        assert verify_exit_code(verdict) == 0


class TestTamper:
    def test_tampered_artifact_hash_fails(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        # tamper with the bound replay artifact after compilation
        (cap / "attestations" / "reperformance.json").write_text('{"match": "exact"}')
        verdict = verify_safety_case(case, cap)
        assert not verdict.ok
        assert verdict.artifact_failures
        assert verify_exit_code(verdict) != 0

    def test_missing_artifact_fails(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        (cap / "completeness.json").unlink()
        verdict = verify_safety_case(case, cap)
        assert not verdict.ok
        assert any("completeness" in f for f in verdict.artifact_failures)

    def test_mutated_case_hash_fails(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        # mutate the body without recomputing the hash
        tampered = case.model_copy(update={"top_claim_id": "C-evil"})
        verdict = verify_safety_case(tampered, cap)
        assert not verdict.case_hash_ok
        assert not verdict.ok


class TestI1Gauntlet:
    def test_naked_claim_detected(self, tmp_path: Path) -> None:
        # construct a case whose claim references no evidence and no children but is
        # not flagged UNSUPPORTED — the verifier must catch this naked claim.
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "clymer-generic-v0")
        leaf = case.get_claim("C-logging")
        assert leaf is not None
        # forge a SUPPORTED naked claim
        forged = leaf.model_copy(
            update={
                "backing_state": BackingState.SUPPORTED,
                "children": [],
                "evidence_ids": [],
            }
        )
        nodes = [forged if getattr(n, "id", None) == "C-logging" else n for n in case.nodes]
        tampered = case.model_copy(update={"nodes": nodes}).with_case_hash()
        verdict = verify_safety_case(tampered, cap)
        assert not verdict.i1_ok
        assert not verdict.ok

    def test_external_evidence_skipped(self, tmp_path: Path) -> None:
        # an evidence node with path_in_bundle=None (external) is not hash-checked
        cap = _capsule(tmp_path)
        ev = Evidence(
            id="E-ext",
            evidence_kind=EvidenceKind.SEAL,
            artifact_ref=ArtifactRef(
                kind="seal", path_in_bundle=None, external_id="rekor:1", sha256="sha256:" + "a" * 64
            ),
        )
        claim = Claim(
            id="C-root",
            statement="x",
            claim_kind=ClaimKind.PROCESS,
            backing_state=BackingState.SUPPORTED,
            evidence_ids=["E-ext"],
        )
        from novafabric.safetycase.models import (
            ProducerInfo,
            SafetyCase,
            Subject,
            SubjectKind,
        )

        case = SafetyCase(
            case_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
            created_at="2026-06-19T12:00:00Z",
            created_by=ProducerInfo(name="novafabric", version="0.1.0"),
            template_id="clymer-generic-v0",
            subject=Subject(
                kind=SubjectKind.RUN_CAPSULE,
                run_ids=["01HXAY7M5JZ8R7K4P9DPBYK2WX"],
                capsule_hash="sha256:" + "b" * 64,
            ),
            top_claim_id="C-root",
            nodes=[claim, ev],
        ).with_case_hash()
        verdict = verify_safety_case(case, cap)
        assert verdict.ok
