"""Map NovaFabric lineage edges to PROV-O RDF and validate them (ADR-0111 SP-4 / P1).

This is the smallest ingest slice of the SPKG canonical layer: a single lineage edge
(the same dict shape NovaFabric already emits — see ``lineage/_types.py`` and
``schemas/lineage-edge.schema.json``) becomes W3C PROV-O triples, which are then checked
against the SHACL shapes in ``ontology.py``. NovaFabric already emits OpenLineage (v0.4),
which aligns with PROV-O, so this is a lossless re-typing, not a new capture path.

Requires the optional ``spkg`` extra (rdflib + pyshacl), imported lazily.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novafabric.lineage._types import node_id_for

from . import ontology

if TYPE_CHECKING:
    from rdflib import Graph, Namespace, URIRef


def _node_uri(NFNS: Namespace, node: dict[str, Any]) -> URIRef:
    """Stable SPKG URI for a lineage node (kind:ref -> deterministic id)."""
    node_id = node.get("node_id") or node_id_for(
        str(node.get("kind", "")), str(node.get("ref", ""))
    )
    return NFNS[f"node/{node_id}"]


def _about_node_id(about: Any) -> str:
    """Resolve the ``about_node`` field (dict node or bare node_id string) to a node id."""
    if isinstance(about, dict):
        return str(
            about.get("node_id")
            or node_id_for(str(about.get("kind", "")), str(about.get("ref", "")))
        )
    return str(about)


def lineage_edge_to_provo(edge: dict[str, Any]) -> Graph:
    """Return an rdflib.Graph of PROV-O triples for one lineage edge.

    Raises KeyError/ValueError-free: unknown edge types fall back to ``nf:relatedTo``;
    unknown node kinds default to ``prov:Entity``.
    """
    from rdflib import RDF, XSD, Graph, Literal, Namespace  # lazy import

    PROV = Namespace(ontology.PROV)
    NFNS = Namespace(ontology.NF)

    g = Graph()
    g.bind("prov", PROV)
    g.bind("nf", NFNS)

    source = edge.get("source", {}) or {}
    target = edge.get("target", {}) or {}
    s_uri = _node_uri(NFNS, source)
    t_uri = _node_uri(NFNS, target)

    # Type each endpoint by its lineage kind.
    s_cls = ontology.KIND_TO_PROV_CLASS.get(str(source.get("kind", "")), "Entity")
    t_cls = ontology.KIND_TO_PROV_CLASS.get(str(target.get("kind", "")), "Entity")
    g.add((s_uri, RDF.type, PROV[s_cls]))
    g.add((t_uri, RDF.type, PROV[t_cls]))

    # Relate the endpoints per the edge_type mapping.
    edge_type = str(edge.get("edge_type", ""))
    pred_name, direction = ontology.EDGE_TYPE_TO_PROV.get(
        edge_type, (ontology.DEFAULT_PREDICATE, "source_of_target")
    )
    predicate = (PROV[pred_name] if pred_name in _PROV_PREDICATES else NFNS[pred_name])
    if direction == "target_of_source":
        g.add((t_uri, predicate, s_uri))
        generated = t_uri
    else:
        g.add((s_uri, predicate, t_uri))
        generated = t_uri if t_cls == "Entity" else s_uri

    # Provenance pointer + generation time on the generated entity (SHACL-required).
    capsule_run_id = edge.get("capsule_run_id")
    if capsule_run_id:
        g.add((generated, NFNS.capsuleRunId, Literal(str(capsule_run_id), datatype=XSD.string)))
    created_at = edge.get("created_at")
    if created_at:
        g.add(
            (generated, PROV.generatedAtTime, Literal(str(created_at), datatype=XSD.dateTime))
        )
    if edge_type:
        g.add((generated, NFNS.edgeType, Literal(edge_type, datatype=XSD.string)))
    return g


def finding_to_rdf(finding: dict[str, Any]) -> Graph:
    """Return an rdflib.Graph of triples for one anomaly finding (ADR-0111 R2).

    ``finding`` shape (all keys optional except a subject + at least one label)::

        {"finding_id": str, "about_node": {"kind","ref"} | {"node_id"} | str,
         "techniques": ["T1059", ...], "countermeasures": ["D3-PA", ...],
         "score": float, "detector": str}

    The result is a ``nf:Finding`` node carrying its ATT&CK technique and/or D3FEND
    countermeasure IRIs. R2 (a finding MUST map to a technique and/or countermeasure) is
    enforced by ``nf:FindingShape`` at validation time, not fabricated here — a
    label-less finding is emitted as-is and then rejected by :func:`validate_provo`.
    """
    from rdflib import RDF, XSD, Graph, Literal, Namespace, URIRef  # lazy import

    NFNS = Namespace(ontology.NF)
    g = Graph()
    g.bind("nf", NFNS)

    about_id = _about_node_id(finding.get("about_node", {}))
    techniques = [str(t) for t in (finding.get("techniques") or [])]
    countermeasures = [str(c) for c in (finding.get("countermeasures") or [])]
    detector = str(finding.get("detector", ""))

    finding_id = finding.get("finding_id")
    if not finding_id:
        seed = f"{about_id}|{detector}|{','.join(sorted(techniques))}"
        finding_id = "f-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    f_uri = NFNS[f"finding/{finding_id}"]

    g.add((f_uri, RDF.type, NFNS.Finding))
    g.add((f_uri, NFNS.aboutNode, NFNS[f"node/{about_id}"]))
    for technique in techniques:
        g.add((f_uri, NFNS.mapsToTechnique, URIRef(ontology.attack_technique_iri(technique))))
    for countermeasure in countermeasures:
        g.add((f_uri, NFNS.mapsToCountermeasure, URIRef(ontology.d3fend_iri(countermeasure))))
    if finding.get("score") is not None:
        g.add((f_uri, NFNS.anomalyScore, Literal(float(finding["score"]), datatype=XSD.decimal)))
    if detector:
        g.add((f_uri, NFNS.detector, Literal(detector, datatype=XSD.string)))
    return g


# PROV-O predicates we emit into the prov: namespace (others go to nf:).
_PROV_PREDICATES = {
    "wasGeneratedBy",
    "used",
    "wasDerivedFrom",
    "wasAttributedTo",
    "wasAssociatedWith",
}


def read_lineage_edges(capsule_dir: str | Path) -> list[dict[str, Any]]:
    """Read a capsule's ``lineage.jsonl`` edge records (the shape the importer consumes).

    Each edge without an explicit ``capsule_run_id`` inherits the capsule directory name so
    the SHACL provenance-pointer requirement stays satisfiable and both the canonical RDF
    and the operational LPG are rebuilt from an identical edge set. A missing
    ``lineage.jsonl`` yields an empty list (an unlineaged capsule is valid).
    """
    capsule_dir = Path(capsule_dir)
    lineage_path = capsule_dir / "lineage.jsonl"
    edges: list[dict[str, Any]] = []
    if not lineage_path.exists():
        return edges
    for line in lineage_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        edge: dict[str, Any] = json.loads(line)
        edge.setdefault("capsule_run_id", capsule_dir.name)
        edges.append(edge)
    return edges


def capsule_lineage_to_provo(capsule_dir: str | Path) -> Graph:
    """Map an entire capsule's ``lineage.jsonl`` to one PROV-O RDF graph (P1 batch ingest).

    Merges each edge's PROV-O triples (via :func:`read_lineage_edges`) into a single graph.
    A missing ``lineage.jsonl`` yields an empty graph. Validate with :func:`validate_provo`.
    """
    from rdflib import Graph  # lazy import

    g = Graph()
    for edge in read_lineage_edges(capsule_dir):
        g += lineage_edge_to_provo(edge)
    return g


def validate_provo(data_graph: Graph) -> tuple[bool, str]:
    """Validate a PROV-O data graph against the SPKG SHACL shapes.

    Returns ``(conforms, human_readable_report)``.
    """
    from pyshacl import validate  # lazy import

    conforms, _results_graph, results_text = validate(
        data_graph,
        shacl_graph=ontology.shapes_graph(),
        inference="none",
        abort_on_first=False,
        meta_shacl=False,
        advanced=False,
    )
    return bool(conforms), str(results_text)
