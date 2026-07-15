"""Security & Provenance Knowledge Graph (SPKG) — ADR-0111, Phase P1.

The canonical semantic layer: map NovaFabric lineage facts to W3C PROV-O RDF and
validate them against SHACL shapes on ingest (ADR-0111 R1/R11). This subpackage is the
construction/ontology slice; detection (PyGOD/TGN) and the KùzuDB/AGE operational view
are later phases.

Optional dependency: install with ``pip install novafabric[spkg]`` (rdflib + pyshacl,
both Tier-A under ADR-0024). All imports here are lazy at call sites so the base package
does not require rdflib.
"""
from __future__ import annotations

__all__ = ["ontology", "provo_mapping"]
