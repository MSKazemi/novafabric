"""P1 batch ingest: a capsule's lineage.jsonl -> one PROV-O graph -> SHACL (ADR-0111).

Requires the optional ``spkg`` extra (rdflib + pyshacl); skipped otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")

from rdflib import RDF, Namespace  # noqa: E402

from novafabric.kg.spkg import ontology  # noqa: E402
from novafabric.kg.spkg.provo_mapping import (  # noqa: E402
    capsule_lineage_to_provo,
    validate_provo,
)

PROV = Namespace(ontology.PROV)


def _write_lineage(capsule_dir: Path, edges: list[dict]) -> None:  # type: ignore[type-arg]
    capsule_dir.mkdir(parents=True, exist_ok=True)
    (capsule_dir / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )


def _edge(edge_type: str, source: dict, target: dict, **extra: object) -> dict:  # type: ignore[type-arg]
    e = {"edge_type": edge_type, "source": source, "target": target,
         "created_at": "2026-07-02T14:00:00.000000Z"}
    e.update(extra)
    return e


def test_batch_ingest_merges_all_edges_and_is_valid(tmp_path: Path) -> None:
    cap = tmp_path / "run-123"
    _write_lineage(
        cap,
        [
            _edge("produces", {"kind": "run", "ref": "run-123"},
                  {"kind": "artifact", "ref": "artifact:run-123:out.txt"}, capsule_run_id="run-123"),
            _edge("uses", {"kind": "run", "ref": "run-123"},
                  {"kind": "dataset", "ref": "dataset:train"}, capsule_run_id="run-123"),
        ],
    )
    g = capsule_lineage_to_provo(cap)
    # Both edges contributed: at least one Entity and one Activity, and >1 triple.
    assert list(g.subjects(RDF.type, PROV.Activity))
    assert list(g.subjects(RDF.type, PROV.Entity))
    conforms, report = validate_provo(g)
    assert conforms, report


def test_capsule_run_id_defaults_to_dir_name(tmp_path: Path) -> None:
    # An edge without capsule_run_id inherits the capsule dir name -> stays SHACL-valid.
    cap = tmp_path / "run-999"
    _write_lineage(
        cap,
        [_edge("produces", {"kind": "run", "ref": "r"},
               {"kind": "artifact", "ref": "artifact:r:x"})],
    )
    g = capsule_lineage_to_provo(cap)
    conforms, report = validate_provo(g)
    assert conforms, report
    # the injected capsuleRunId literal is the directory name
    assert any("run-999" in str(o) for _, _, o in g.triples((None, None, None)))


def test_missing_lineage_file_yields_empty_graph(tmp_path: Path) -> None:
    cap = tmp_path / "empty"
    cap.mkdir()
    g = capsule_lineage_to_provo(cap)
    assert len(g) == 0
    conforms, _ = validate_provo(g)  # empty graph vacuously conforms
    assert conforms


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    cap = tmp_path / "run-1"
    cap.mkdir()
    (cap / "lineage.jsonl").write_text(
        json.dumps(_edge("produces", {"kind": "run", "ref": "r"},
                         {"kind": "artifact", "ref": "a:1"}, capsule_run_id="run-1"))
        + "\n\n   \n",
        encoding="utf-8",
    )
    g = capsule_lineage_to_provo(cap)
    conforms, report = validate_provo(g)
    assert conforms, report
