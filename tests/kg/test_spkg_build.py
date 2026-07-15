"""P1 build orchestration: capsule -> canonical RDF (gated) + operational LPG (ADR-0111 R4).

Requires the optional ``spkg`` extra (rdflib + pyshacl + kuzu); skipped otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")
pytest.importorskip("kuzu")

from novafabric.kg.spkg.build import (  # noqa: E402
    SpkgBuildResult,
    SpkgValidationError,
    build_spkg,
)
from novafabric.kg.spkg.graph_store import SpkgGraphStore  # noqa: E402


def _capsule(tmp_path: Path, created_at: str = "2026-07-02T14:00:00.000000Z") -> Path:
    cap = tmp_path / "run-123"
    cap.mkdir()
    edges = [
        {"edge_type": "produces", "source": {"kind": "run", "ref": "run-123"},
         "target": {"kind": "artifact", "ref": "artifact:run-123:out.txt"},
         "created_at": created_at, "capsule_run_id": "run-123"},
        {"edge_type": "uses", "source": {"kind": "run", "ref": "run-123"},
         "target": {"kind": "dataset", "ref": "dataset:train"},
         "created_at": created_at, "capsule_run_id": "run-123"},
    ]
    (cap / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )
    return cap


def test_build_populates_both_layers(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    store = SpkgGraphStore()
    try:
        result = build_spkg(cap, store)
        assert isinstance(result, SpkgBuildResult)
        assert result.validated is True
        assert result.triples > 0
        assert result.edges_ingested == 2
        # operational LPG mirrors the capsule edges (no unique state — R4)
        assert store.edge_count() == 2
        assert store.node_count() == 3  # run, artifact, dataset
    finally:
        store.close()


def test_invalid_canonical_layer_blocks_lpg_write(tmp_path: Path) -> None:
    # R11: an ill-typed timestamp fails SHACL, so nothing is written to the LPG.
    cap = _capsule(tmp_path, created_at="not-a-date")
    store = SpkgGraphStore()
    try:
        with pytest.raises(SpkgValidationError, match="SHACL"):
            build_spkg(cap, store)
        assert store.edge_count() == 0  # store untouched on validation failure
    finally:
        store.close()


def test_no_validate_skips_gate(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    store = SpkgGraphStore()
    try:
        result = build_spkg(cap, store, validate=False)
        assert result.validated is False
        assert result.edges_ingested == 2
    finally:
        store.close()


def test_lpg_is_rebuildable_from_capsule(tmp_path: Path) -> None:
    # R4: rebuilding from the same capsule is deterministic in node/edge counts.
    cap = _capsule(tmp_path)
    s1, s2 = SpkgGraphStore(), SpkgGraphStore()
    try:
        r1 = build_spkg(cap, s1)
        r2 = build_spkg(cap, s2)
        assert (s1.node_count(), s1.edge_count()) == (s2.node_count(), s2.edge_count())
        assert r1.edges_ingested == r2.edges_ingested
    finally:
        s1.close()
        s2.close()
