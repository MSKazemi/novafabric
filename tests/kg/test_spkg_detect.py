"""SP-2 tests: unsupervised edge-level anomaly detection (ADR-0111, BQ-SPKG-01).

Pure-Tier-A baseline (no torch/PyG). A benign fleet with a few injected CALDERA-style
attack edges: the injected edges must rank in the top-k, and every emitted finding must
validate against the AnomalyFinding schema (R2 explanation mandatory).
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from novafabric.kg.spkg.detect import StructuralAnomalyDetector, to_findings

SCHEMA = json.loads(Path("schemas/spkg-anomaly-finding-v1.schema.json").read_text())


def _edge(src_ref: str, et: str, tgt_kind: str, tgt_ref: str) -> dict:  # type: ignore[type-arg]
    return {
        "source": {"kind": "run", "ref": src_ref},
        "target": {"kind": tgt_kind, "ref": tgt_ref},
        "edge_type": et,
        "capsule_run_id": src_ref,
        "created_at": "2026-07-02T00:00:00.000Z",
    }


def _benign_fleet(n: int = 40) -> list:  # type: ignore[type-arg]
    edges = []
    for i in range(n):
        r = f"run-{i}"
        edges.append(_edge(r, "uses", "tool", "web_search"))
        edges.append(_edge(r, "uses", "model", "gpt-4o"))
        edges.append(_edge(r, "produces", "artifact", "report.md"))
    return edges


# CALDERA-style injected attack edges (structurally rare vs the benign fleet).
ATTACK_SHELL = _edge("run-attacker", "uses", "tool", "shell")
ATTACK_CREDS = _edge("run-attacker", "reads", "dataset", "aws_credentials")


def test_injected_attack_edges_rank_top_k() -> None:
    all_edges = _benign_fleet() + [ATTACK_SHELL, ATTACK_CREDS]
    det = StructuralAnomalyDetector().fit(all_edges)
    top = det.top_k(all_edges, k=3)
    top_refs = {se.edge["target"]["ref"] for se in top}
    assert "shell" in top_refs
    assert "aws_credentials" in top_refs


def test_benign_edges_score_lower_than_attacks() -> None:
    all_edges = _benign_fleet() + [ATTACK_SHELL, ATTACK_CREDS]
    det = StructuralAnomalyDetector().fit(all_edges)
    scored = {id(se.edge): se for se in det.score(all_edges)}
    attack_scores = [scored[id(ATTACK_SHELL)].surprisal, scored[id(ATTACK_CREDS)].surprisal]
    benign_score = next(
        se.surprisal for se in det.score(all_edges) if se.edge["target"]["ref"] == "web_search"
    )
    assert min(attack_scores) > benign_score


def test_findings_validate_against_schema_with_attack_mapping() -> None:
    all_edges = _benign_fleet() + [ATTACK_SHELL, ATTACK_CREDS]
    det = StructuralAnomalyDetector().fit(all_edges)
    findings = to_findings(det.top_k(all_edges, k=2))
    assert len(findings) == 2
    for f in findings:
        jsonschema.validate(f, SCHEMA, format_checker=jsonschema.FormatChecker())
    # heuristic ATT&CK mapping: shell -> T1059.004, creds -> T1078
    techniques = {f["explanation"]["attack_technique_id"] for f in findings}
    assert "T1059.004" in techniques
    assert "T1078" in techniques


def test_score_requires_fit() -> None:
    det = StructuralAnomalyDetector()
    try:
        det.score([ATTACK_SHELL])
    except RuntimeError:
        return
    raise AssertionError("score() must require fit()")
