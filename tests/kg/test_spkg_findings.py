"""P1 tests: anomaly finding -> ATT&CK/D3FEND-labelled RDF -> R2 SHACL gate (ADR-0111).

R2 (MUST): a finding must map to a MITRE ATT&CK technique and/or a D3FEND countermeasure —
a raw score alone is not a valid finding. Requires the optional ``spkg`` extra.
"""
from __future__ import annotations

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")

from rdflib import RDF, Namespace, URIRef  # noqa: E402

from novafabric.kg.spkg import ontology  # noqa: E402
from novafabric.kg.spkg.provo_mapping import finding_to_rdf, validate_provo  # noqa: E402

NF = Namespace(ontology.NF)


def test_finding_with_technique_conforms() -> None:
    g = finding_to_rdf(
        {
            "about_node": {"kind": "run", "ref": "run-123"},
            "techniques": ["T1059"],
            "score": 0.93,
            "detector": "pygod-dominant",
        }
    )
    conforms, report = validate_provo(g)
    assert conforms, report


def test_finding_with_countermeasure_only_conforms() -> None:
    g = finding_to_rdf(
        {"about_node": "node-abc", "countermeasures": ["D3-PA"], "score": 0.7}
    )
    conforms, report = validate_provo(g)
    assert conforms, report


def test_finding_with_score_only_fails_r2() -> None:
    # A raw score with no ATT&CK/D3FEND mapping must be rejected (R2).
    g = finding_to_rdf({"about_node": "node-xyz", "score": 0.99, "detector": "tgn"})
    conforms, report = validate_provo(g)
    assert not conforms
    assert "R2" in report or "ATT&CK" in report or "technique" in report


def test_technique_iri_is_attack_namespaced() -> None:
    g = finding_to_rdf({"about_node": "n1", "techniques": ["T1059.004"]})
    expected = URIRef(ontology.attack_technique_iri("T1059.004"))
    assert (None, NF.mapsToTechnique, expected) in g
    assert str(expected) == "https://attack.mitre.org/techniques/T1059.004"


def test_countermeasure_iri_is_d3fend_namespaced() -> None:
    g = finding_to_rdf({"about_node": "n1", "countermeasures": ["D3-PA"]})
    expected = URIRef(ontology.d3fend_iri("D3-PA"))
    assert (None, NF.mapsToCountermeasure, expected) in g


def test_finding_is_typed_and_points_at_subject() -> None:
    g = finding_to_rdf({"about_node": "n42", "techniques": ["T1204"]})
    findings = list(g.subjects(RDF.type, NF.Finding))
    assert len(findings) == 1
    assert (findings[0], NF.aboutNode, NF["node/n42"]) in g


def test_finding_id_is_deterministic() -> None:
    payload = {"about_node": "n1", "techniques": ["T1059"], "detector": "d"}
    ids1 = set(finding_to_rdf(payload).subjects(RDF.type, NF.Finding))
    ids2 = set(finding_to_rdf(payload).subjects(RDF.type, NF.Finding))
    assert ids1 == ids2
