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


def test_neither_manifest_nor_lineage_yields_empty_graph(tmp_path: Path) -> None:
    # Only a capsule that knows *nothing* about itself maps to nothing. A missing
    # lineage.jsonl alone does not — see TestManifestProvenanceIsSeeded.
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


# --- the capsule's own provenance (ADR-0256) ------------------------------------------

MANIFEST = """run_id: 01ABCDEF
created_at: '2026-08-28T05:23:19.000000+00:00'
finished_at: '2026-08-28T05:23:24.000000+00:00'
command: [python, train.py]
status: completed
exit_code: 0
capture_mode: full
novafabric_version: 0.101.0
evidence_digests:
  outputs/model.pt:
    sha256: 'sha256:aa'
    size_bytes: 12
  trace.jsonl:
    sha256: 'sha256:bb'
    size_bytes: 34
"""


def _write_manifest(capsule_dir: Path, text: str = MANIFEST) -> Path:
    capsule_dir.mkdir(parents=True, exist_ok=True)
    (capsule_dir / "capsule.yaml").write_text(text, encoding="utf-8")
    return capsule_dir


class TestManifestProvenanceIsSeeded:
    """A first-in-chain capsule has no lineage edges but is not without provenance."""

    def test_empty_lineage_still_yields_the_run_and_its_evidence(self, tmp_path: Path) -> None:
        cap = _write_manifest(tmp_path / "run-seed")
        g = capsule_lineage_to_provo(cap)
        assert len(g) > 0
        activities = list(g.subjects(RDF.type, PROV.Activity))
        entities = list(g.subjects(RDF.type, PROV.Entity))
        assert len(activities) == 1
        assert len(entities) == 2  # one per evidence_digests entry
        for ent in entities:
            assert (ent, PROV.wasGeneratedBy, activities[0]) in g

    def test_the_seeded_graph_conforms_to_the_shacl_shapes(self, tmp_path: Path) -> None:
        cap = _write_manifest(tmp_path / "run-shacl")
        conforms, report = validate_provo(capsule_lineage_to_provo(cap))
        assert conforms, report

    def test_evidence_digests_are_carried_onto_the_entities(self, tmp_path: Path) -> None:
        cap = _write_manifest(tmp_path / "run-digests")
        g = capsule_lineage_to_provo(cap)
        NF = Namespace(ontology.NF)
        names = {str(o) for o in g.objects(None, NF.filename)}
        assert names == {"outputs/model.pt", "trace.jsonl"}
        assert {str(o) for o in g.objects(None, NF.sha256)} == {"sha256:aa", "sha256:bb"}

    def test_seed_merges_with_lineage_on_the_same_run_subject(self, tmp_path: Path) -> None:
        # Both paths derive the run URI from node_id_for("run", run_id), so the seeded
        # activity and the lineage edge's source are one subject, not two.
        cap = _write_manifest(tmp_path / "run-merge")
        _write_lineage(
            cap,
            [_edge("produces", {"kind": "run", "ref": "01ABCDEF"},
                   {"kind": "artifact", "ref": "a:1"}, capsule_run_id="01ABCDEF")],
        )
        g = capsule_lineage_to_provo(cap)
        assert len(list(g.subjects(RDF.type, PROV.Activity))) == 1
        conforms, report = validate_provo(g)
        assert conforms, report

    def test_an_unparseable_timestamp_is_dropped_not_emitted(self, tmp_path: Path) -> None:
        # A bad timestamp must not turn a valid capsule into a SHACL failure.
        cap = _write_manifest(
            tmp_path / "run-badtime",
            MANIFEST.replace("'2026-08-28T05:23:24.000000+00:00'", "'not a date'"),
        )
        g = capsule_lineage_to_provo(cap)
        assert not list(g.objects(None, PROV.generatedAtTime))
        assert not list(g.objects(None, PROV.endTime))
        conforms, report = validate_provo(g)
        assert conforms, report

    def test_manifest_without_run_id_seeds_nothing(self, tmp_path: Path) -> None:
        cap = _write_manifest(tmp_path / "run-noid", "created_at: '2026-08-28T05:23:19+00:00'\n")
        assert len(capsule_lineage_to_provo(cap)) == 0

    def test_unreadable_manifest_does_not_raise(self, tmp_path: Path) -> None:
        cap = _write_manifest(tmp_path / "run-broken", "run_id: [unclosed\n")
        assert len(capsule_lineage_to_provo(cap)) == 0
