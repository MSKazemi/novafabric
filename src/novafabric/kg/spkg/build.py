"""Build the SPKG from a capsule: canonical RDF (gated) + operational LPG (ADR-0111 P1).

Orchestrates the two SPKG layers so the operational KùzuDB LPG carries no state that is
not derivable from a capsule (ADR-0111 R4): the canonical W3C PROV-O RDF is built and
SHACL-validated first (R1/R11 — invalid facts are rejected before anything is written to
the operational store), then the LPG is (re)built from the identical capsule edge set.
Both layers are pure projections of ``lineage.jsonl``, so the LPG is always rebuildable.

Requires the optional ``spkg`` extra (rdflib + pyshacl + kuzu), imported lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .provo_mapping import (
    capsule_lineage_to_provo,
    read_lineage_edges,
    validate_provo,
)

if TYPE_CHECKING:
    from .graph_store import SpkgGraphStore


class SpkgValidationError(ValueError):
    """Raised when the canonical PROV-O layer fails SHACL validation (R11 gate)."""


@dataclass(frozen=True)
class SpkgBuildResult:
    """Outcome of a capsule → SPKG build."""

    triples: int
    edges_ingested: int
    validated: bool


def build_spkg(
    capsule_dir: str | Path,
    store: SpkgGraphStore,
    *,
    validate: bool = True,
) -> SpkgBuildResult:
    """Build the canonical RDF (SHACL-gated) then rebuild the operational LPG from a capsule.

    The canonical layer is validated **before** the LPG is written (R11): on failure,
    :class:`SpkgValidationError` is raised and the operational store is left untouched.
    Returns counts for both layers. Idempotent per capsule at the RDF layer; the LPG append
    is a rebuild-from-capsule (R4 — no unique LPG state).
    """
    graph = capsule_lineage_to_provo(capsule_dir)
    validated = False
    if validate:
        conforms, report = validate_provo(graph)
        if not conforms:
            raise SpkgValidationError(f"SPKG canonical layer failed SHACL validation: {report}")
        validated = True

    edges = read_lineage_edges(capsule_dir)
    ingested = store.ingest_edges(edges)
    return SpkgBuildResult(triples=len(graph), edges_ingested=ingested, validated=validated)
