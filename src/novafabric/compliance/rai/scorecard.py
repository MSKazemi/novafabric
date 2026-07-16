"""Responsible-AI scorecard (ADR-0158 D4 / NF-262, first slice).

The scorecard records **presence/coverage of evidence per RAI dimension** — each cell is
``supported`` / ``partial`` / ``unsupported`` / ``not_applicable`` — and **never a numeric
"responsibility score"** (ADR-0158 I-4). NovaFabric applies no threshold (no 4/5 rule) and assigns
no fair/unfair or pass/fail label; a cell only records whether evidence for that dimension exists.

This first slice is the pure coverage projection over supplied per-dimension evidence refs. The
collector that discovers those refs from sealed evidence (disaggregated fairness stats, declared
WCAG/EN-301-549 conformance, energy receipts, …) is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum

from pydantic import BaseModel

#: The Responsible-AI dimensions, in fixed order.
RAI_DIMENSIONS: tuple[str, ...] = (
    "fairness",
    "transparency",
    "accountability",
    "privacy",
    "safety_robustness",
    "human_oversight",
    "accessibility",
    "sustainability",
)


class CellCoverage(str, Enum):
    supported = "supported"  # evidence present
    partial = "partial"  # some evidence, declared incomplete
    unsupported = "unsupported"  # applicable but no evidence
    not_applicable = "not_applicable"  # declared N/A for this system


class ScorecardCell(BaseModel):
    dimension: str
    coverage: CellCoverage
    evidence_refs: list[str] = []
    # Intentionally NO score/rating/grade field — coverage, never a responsibility score.


class RAIScorecard(BaseModel):
    cells: list[ScorecardCell]
    summary: dict[str, int]  # counts per coverage state
    # Intentionally NO numeric responsibility score (ADR-0158 I-4).


def build_rai_scorecard(
    evidence: Mapping[str, list[str]] = {},
    *,
    not_applicable: Iterable[str] = (),
    partial: Iterable[str] = (),
) -> RAIScorecard:
    """Build the coverage scorecard from per-dimension evidence refs.

    A dimension declared ``not_applicable`` wins over any evidence; otherwise a dimension with
    refs is ``supported`` (or ``partial`` if flagged), and a dimension with no refs is
    ``unsupported``. Nothing is scored or graded.
    """
    na_set = set(not_applicable)
    partial_set = set(partial)

    cells: list[ScorecardCell] = []
    for dim in RAI_DIMENSIONS:
        refs = list(evidence.get(dim, []))
        if dim in na_set:
            coverage = CellCoverage.not_applicable
            refs = []
        elif refs:
            coverage = CellCoverage.partial if dim in partial_set else CellCoverage.supported
        else:
            coverage = CellCoverage.unsupported
        cells.append(ScorecardCell(dimension=dim, coverage=coverage, evidence_refs=refs))

    summary = {state.value: 0 for state in CellCoverage}
    for cell in cells:
        summary[cell.coverage.value] += 1
    return RAIScorecard(cells=cells, summary=summary)
