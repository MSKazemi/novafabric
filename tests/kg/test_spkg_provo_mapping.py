"""SP-4 / P1 tests: lineage edge -> PROV-O RDF -> SHACL-valid (ADR-0111, BQ-SPKG-01).

Requires the optional ``spkg`` extra (rdflib + pyshacl); skipped otherwise.
"""
from __future__ import annotations

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")

from rdflib import RDF, Namespace  # noqa: E402

from novafabric.kg.spkg import ontology  # noqa: E402
from novafabric.kg.spkg.provo_mapping import (  # noqa: E402
    lineage_edge_to_provo,
    validate_provo,
)

PROV = Namespace(ontology.PROV)
NF = Namespace(ontology.NF)


def _produces_edge(**overrides: object) -> dict:  # type: ignore[type-arg]
    edge = {
        "schema_version": "0.1.0",
        "edge_id": "01HZY8Q9K5M3N7P0R2S4T6V8W0",
        "edge_type": "produces",
        "source": {"kind": "run", "ref": "run-123"},
        "target": {"kind": "artifact", "ref": "artifact:run-123:out.txt"},
        "direction": "source_to_target",
        "created_at": "2026-07-02T14:00:00.000000Z",
        "emitter": {"name": "novafabric", "version": "0.4.0"},
        "capsule_run_id": "run-123",
    }
    edge.update(overrides)
    return edge


def test_produces_edge_maps_to_provo_and_is_shacl_valid() -> None:
    """A run->artifact 'produces' edge round-trips to PROV-O and passes SHACL (SP-4)."""
    g = lineage_edge_to_provo(_produces_edge())

    # The artifact is typed prov:Entity and wasGeneratedBy the run activity.
    entities = list(g.subjects(RDF.type, PROV.Entity))
    activities = list(g.subjects(RDF.type, PROV.Activity))
    assert len(entities) == 1
    assert len(activities) == 1
    artifact = entities[0]
    run = activities[0]
    assert (artifact, PROV.wasGeneratedBy, run) in g

    # Provenance pointer + generation time present on the generated entity.
    assert (artifact, NF.capsuleRunId, None) in _triples_with(g, artifact, NF.capsuleRunId)
    assert (artifact, PROV.generatedAtTime, None) in _triples_with(
        g, artifact, PROV.generatedAtTime
    )

    conforms, report = validate_provo(g)
    assert conforms, report


def test_missing_capsule_run_id_fails_shacl() -> None:
    """R11: a generated entity without a capsule provenance pointer is rejected."""
    edge = _produces_edge()
    edge.pop("capsule_run_id")
    g = lineage_edge_to_provo(edge)
    conforms, report = validate_provo(g)
    assert not conforms
    assert "capsuleRunId" in report


def test_unknown_edge_type_falls_back_to_nf_relatedto() -> None:
    """Unknown edge types degrade to nf:relatedTo without crashing and stay valid."""
    edge = _produces_edge(edge_type="mysterious_link")
    g = lineage_edge_to_provo(edge)
    # nf:relatedTo predicate is used; no prov generation predicate.
    assert any(p == NF.relatedTo for _, p, _ in g)
    conforms, report = validate_provo(g)
    assert conforms, report


def test_uses_edge_types_run_as_activity_and_asset_as_entity() -> None:
    edge = _produces_edge(
        edge_type="uses",
        source={"kind": "run", "ref": "run-9"},
        target={"kind": "dataset", "ref": "reg:ds@1"},
        capsule_run_id="run-9",
    )
    g = lineage_edge_to_provo(edge)
    assert list(g.subjects(RDF.type, PROV.Activity))  # run
    dataset = list(g.subjects(RDF.type, PROV.Entity))
    assert len(dataset) == 1
    # run used dataset
    run = list(g.subjects(RDF.type, PROV.Activity))[0]
    assert (run, PROV.used, dataset[0]) in g
    conforms, report = validate_provo(g)
    assert conforms, report


def _triples_with(g: object, subj: object, pred: object):  # noqa: ANN202
    """Helper: a set-like containing (subj, pred, None) if any object exists."""
    found = list(g.triples((subj, pred, None)))  # type: ignore[attr-defined]
    return {(subj, pred, None)} if found else set()
