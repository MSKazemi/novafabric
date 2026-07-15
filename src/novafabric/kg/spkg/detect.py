"""Unsupervised edge-level anomaly detection for the SPKG (ADR-0111 D3, spike SP-2).

The v0.1 detector is a **self-contained, Tier-A, dependency-free structural outlier
scorer**: it learns the fleet's own edge distribution (edge-type / entity / kind-triple
frequencies) and flags edges whose combined surprisal is high — no labels, edge-level,
matching the "capture everything, most runs are benign" reality (StreamSpot/Unicorn model).

Why not PyGOD/TGN here (yet): PyGOD (BSD-2) and its numpy/scipy/networkx/scikit-learn deps
are Tier-A, but the GNN detectors ultimately require torch + torch_geometric, whose binary
wheels bundle third-party components (e.g. Intel MKL) needing a full distribution-license
audit under ADR-0024, and torch is a ~1 GB install. The GNN autoencoder (DOMINANT via
PyGOD) and continuous-time model (TGN) are the production upgrade — a resource-gated slice.
This baseline proves the detection → AnomalyFinding → ATT&CK/D3FEND explanation pipeline
end-to-end with zero non-Tier-A dependencies.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from novafabric.capture._ulid import new_ulid
from novafabric.lineage._types import node_id_for


def _kind(node: dict[str, Any]) -> str:
    return str(node.get("kind", ""))


def _ref(node: dict[str, Any]) -> str:
    return str(node.get("ref", ""))


@dataclass
class ScoredEdge:
    edge: dict[str, Any]
    score: float  # normalized [0,1]; higher = more anomalous
    surprisal: float  # raw combined surprisal (bits)


class StructuralAnomalyDetector:
    """Learn the fleet's normal edge distribution; score edges by combined surprisal."""

    def __init__(self) -> None:
        self._edge_type = Counter[str]()
        self._target_ref = Counter[str]()
        self._source_ref = Counter[str]()
        self._triple = Counter[str]()  # (source_kind, edge_type, target_kind)
        self._total = 0
        self._fitted = False

    def fit(self, edges: Iterable[dict[str, Any]]) -> StructuralAnomalyDetector:
        for e in edges:
            src, tgt = e.get("source", {}) or {}, e.get("target", {}) or {}
            self._edge_type[str(e.get("edge_type", ""))] += 1
            self._target_ref[f"{_kind(tgt)}:{_ref(tgt)}"] += 1
            self._source_ref[f"{_kind(src)}:{_ref(src)}"] += 1
            self._triple[f"{_kind(src)}|{e.get('edge_type', '')}|{_kind(tgt)}"] += 1
            self._total += 1
        self._fitted = True
        return self

    def _surprisal(self, counter: Counter[str], key: str) -> float:
        # Laplace-smoothed self-information in bits: rarer -> larger.
        p = (counter.get(key, 0) + 1) / (self._total + len(counter) + 1)
        return -math.log2(p)

    def edge_surprisal(self, edge: dict[str, Any]) -> float:
        src, tgt = edge.get("source", {}) or {}, edge.get("target", {}) or {}
        return (
            self._surprisal(self._edge_type, str(edge.get("edge_type", "")))
            + self._surprisal(self._target_ref, f"{_kind(tgt)}:{_ref(tgt)}")
            + self._surprisal(self._triple,
                              f"{_kind(src)}|{edge.get('edge_type', '')}|{_kind(tgt)}")
        )

    def score(self, edges: Iterable[dict[str, Any]]) -> list[ScoredEdge]:
        """Score edges; the returned score is min-max normalized to [0,1] over the batch."""
        if not self._fitted:
            raise RuntimeError("call fit() before score()")
        raw = [(e, self.edge_surprisal(e)) for e in edges]
        if not raw:
            return []
        lo = min(s for _, s in raw)
        hi = max(s for _, s in raw)
        span = (hi - lo) or 1.0
        return [
            ScoredEdge(edge=e, score=(s - lo) / span, surprisal=s)
            for e, s in raw
        ]

    def top_k(self, edges: Iterable[dict[str, Any]], k: int = 5) -> list[ScoredEdge]:
        scored = self.score(edges)
        scored.sort(key=lambda se: se.surprisal, reverse=True)
        return scored[:k]


# Minimal, honest ATT&CK mapping heuristics for the baseline explanation (ADR-0111 R2).
def _explain(edge: dict[str, Any]) -> dict[str, str]:
    tgt_ref = _ref(edge.get("target", {}) or {}).lower()
    if "shell" in tgt_ref or "bash" in tgt_ref or "exec" in tgt_ref:
        tech = "T1059.004"  # Command and Scripting Interpreter: Unix Shell
    elif "cred" in tgt_ref or "secret" in tgt_ref or "token" in tgt_ref:
        tech = "T1078"  # Valid Accounts
    else:
        tech = "T1204"  # User Execution (generic anomalous action)
    return {
        "attack_technique_id": tech,
        "rationale": (
            "Edge is a structural outlier vs the learned fleet distribution "
            f"(edge_type={edge.get('edge_type', '')!r}, target={tgt_ref!r}); "
            "rare edge-type / entity / kind-triple combination."
        ),
    }


def to_findings(
    scored: list[ScoredEdge], detector_id: str = "spkg-structural-v0", created_at: str = ""
) -> list[dict[str, Any]]:
    """Turn scored edges into schema-valid AnomalyFinding dicts (ADR-0111 R2)."""
    findings: list[dict[str, Any]] = []
    for se in scored:
        src, tgt = se.edge.get("source", {}) or {}, se.edge.get("target", {}) or {}
        s_id = node_id_for(_kind(src), _ref(src))
        t_id = node_id_for(_kind(tgt), _ref(tgt))
        findings.append(
            {
                "schema_version": "0.1.0",
                "finding_id": new_ulid(),
                "created_at": created_at or se.edge.get("created_at", "1970-01-01T00:00:00.000Z"),
                "subject_kind": "edge",
                "subject_ref": f"spkg:edge:{s_id}->{t_id}",
                "score": round(se.score, 6),
                "method": "structural_outlier",
                "unsupervised": True,
                "detector_id": detector_id,
                "explanation": _explain(se.edge),
            }
        )
    return findings
