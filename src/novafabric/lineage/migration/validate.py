"""E-2: Divergence checker comparing two AbstractLineageStore instances."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novafabric.lineage.store import AbstractLineageStore


@dataclass
class DivergenceReport:
    """Single query divergence between source and target stores."""

    run_id: str
    query_type: str  # "provenance" | "blast_radius" | "replay_chain"
    source_count: int
    target_count: int
    is_diverged: bool
    #: ``(kind, ref)`` identities the source returned and the target did not, and the
    #: reverse; at most :data:`MAX_LISTED` each. Counts alone hid a target holding
    #: different nodes (review replay-lineage-registry-18).
    missing_in_target: list[tuple[str, str]] = field(default_factory=list)
    unexpected_in_target: list[tuple[str, str]] = field(default_factory=list)


#: Identities listed per side in a divergence report.
MAX_LISTED = 20


def _identities(rows: Any) -> set[tuple[str, str]]:
    """The ``(kind, ref)`` set of the rows a query returned (row order is not identity)."""
    return {(str(r.get("kind", "")), str(r.get("ref", ""))) for r in rows}


def validate_stores(
    source: AbstractLineageStore,
    target: AbstractLineageStore,
    run_ids: list[str],
    max_depth: int = 5,
) -> list[DivergenceReport]:
    """Run provenance+blast_radius+replay_chain on both stores, return divergence list."""
    reports: list[DivergenceReport] = []

    for run_id in run_ids:
        for query_type in ("provenance", "blast_radius", "replay_chain"):
            if query_type == "provenance":
                src_rows = source.provenance(run_id, max_depth)
                tgt_rows = target.provenance(run_id, max_depth)
            elif query_type == "blast_radius":
                src_rows = source.blast_radius(run_id, max_depth)
                tgt_rows = target.blast_radius(run_id, max_depth)
            else:
                src_rows = source.replay_chain(run_id)
                tgt_rows = target.replay_chain(run_id)

            src_count = len(src_rows)
            tgt_count = len(tgt_rows)
            src_ids = _identities(src_rows)
            tgt_ids = _identities(tgt_rows)
            is_diverged = src_count != tgt_count or src_ids != tgt_ids

            if is_diverged:
                reports.append(
                    DivergenceReport(
                        run_id=run_id,
                        query_type=query_type,
                        source_count=src_count,
                        target_count=tgt_count,
                        is_diverged=True,
                        missing_in_target=sorted(src_ids - tgt_ids)[:MAX_LISTED],
                        unexpected_in_target=sorted(tgt_ids - src_ids)[:MAX_LISTED],
                    )
                )

    return reports
