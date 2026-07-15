"""SPKG ontology: namespaces + SHACL shapes for the PROV-O provenance layer (ADR-0111).

The provenance vocabulary is W3C PROV-O; NovaFabric-specific terms live under the ``nf:``
namespace. SHACL shapes enforce the minimum integrity constraints on ingested provenance
triples (ADR-0111 R11): every generated entity must carry its capsule provenance pointer
and a well-typed generation time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rdflib import Graph

# Namespace IRIs (kept as plain strings so this module imports without rdflib).
PROV = "http://www.w3.org/ns/prov#"
NF = "https://novafabric.io/ns/spkg#"
XSD = "http://www.w3.org/2001/XMLSchema#"
# Security-label namespaces (ADR-0111 R2). ATT&CK technique pages and the D3FEND ontology.
ATTACK = "https://attack.mitre.org/techniques/"
D3FEND = "http://d3fend.mitre.org/ontologies/d3fend.owl#"


def attack_technique_iri(technique_id: str) -> str:
    """IRI for a MITRE ATT&CK technique id (e.g. ``T1059`` or ``T1059.004``)."""
    return ATTACK + technique_id.strip()


def d3fend_iri(countermeasure_id: str) -> str:
    """IRI for a D3FEND countermeasure/artifact id (e.g. ``D3-PA`` process analysis)."""
    return D3FEND + countermeasure_id.strip()

# Lineage node kind -> PROV-O class.
KIND_TO_PROV_CLASS: dict[str, str] = {
    "run": "Activity",
    "agent": "Agent",
    "asset": "Entity",
    "artifact": "Entity",
    "model": "Entity",
    "dataset": "Entity",
    "tool": "Entity",
    "prompt": "Entity",
}

# Lineage edge_type -> (predicate, direction) in PROV-O terms.
# direction "target_of_source": triple is (target) <predicate> (source).
# direction "source_of_target": triple is (source) <predicate> (target).
EDGE_TYPE_TO_PROV: dict[str, tuple[str, str]] = {
    "produces": ("wasGeneratedBy", "target_of_source"),
    "generates": ("wasGeneratedBy", "target_of_source"),
    "consumes": ("used", "source_of_target"),
    "uses": ("used", "source_of_target"),
    "derives": ("wasDerivedFrom", "target_of_source"),
    "derived_from": ("wasDerivedFrom", "target_of_source"),
    "attributed_to": ("wasAttributedTo", "source_of_target"),
    "associated_with": ("wasAssociatedWith", "source_of_target"),
}

# Fallback predicate for unknown edge types (kept in the nf: namespace, not PROV).
DEFAULT_PREDICATE = "relatedTo"

# SHACL shapes (Turtle). A generated PROV entity MUST carry exactly one nf:capsuleRunId
# and, if present, prov:generatedAtTime MUST be an xsd:dateTime.
SHACL_SHAPES_TTL = """
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix nf:   <https://novafabric.io/ns/spkg#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

nf:GeneratedEntityShape
    a sh:NodeShape ;
    sh:targetClass prov:Entity ;
    sh:property [
        sh:path nf:capsuleRunId ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:message "SPKG entity must carry exactly one nf:capsuleRunId." ;
    ] ;
    sh:property [
        sh:path prov:generatedAtTime ;
        sh:datatype xsd:dateTime ;
        sh:maxCount 1 ;
        sh:message "prov:generatedAtTime must be a single xsd:dateTime." ;
    ] .

# ADR-0111 R2 (MUST): an anomaly finding MUST carry an explanation mapped to a MITRE
# ATT&CK technique and/or a D3FEND countermeasure — a raw score alone is NOT a valid
# finding. Every finding must also point at the node it is about.
nf:FindingShape
    a sh:NodeShape ;
    sh:targetClass nf:Finding ;
    sh:property [
        sh:path nf:aboutNode ;
        sh:minCount 1 ;
        sh:message "A finding must reference the node it is about (nf:aboutNode)." ;
    ] ;
    sh:or (
        [ sh:path nf:mapsToTechnique ; sh:minCount 1 ]
        [ sh:path nf:mapsToCountermeasure ; sh:minCount 1 ]
    ) ;
    sh:message "R2: a finding must map to an ATT&CK technique and/or D3FEND countermeasure." .
""".strip()


def shapes_graph() -> Graph:
    """Return the SHACL shapes as a parsed rdflib Graph (requires the ``spkg`` extra)."""
    from rdflib import Graph  # lazy import

    g = Graph()
    g.parse(data=SHACL_SHAPES_TTL, format="turtle")
    return g
