"""E-2: Divergence checker comparing two AbstractLineageStore instances."""

from __future__ import annotations

from dataclasses import dataclass

from novafabric.lineage.store import AbstractLineageStore


@dataclass
class DivergenceReport:
    """Single query divergence between source and target stores."""

    run_id: str
    query_type: str  # "provenance" | "blast_radius" | "replay_chain"
    source_count: int
    target_count: int
    is_diverged: bool


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
            is_diverged = src_count != tgt_count

            if is_diverged:
                reports.append(
                    DivergenceReport(
                        run_id=run_id,
                        query_type=query_type,
                        source_count=src_count,
                        target_count=tgt_count,
                        is_diverged=True,
                    )
                )

    return reports
