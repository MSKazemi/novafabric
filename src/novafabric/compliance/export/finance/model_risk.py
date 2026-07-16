"""SR 26-2 / SR 11-7 model-risk evidence pack (ADR-0159 D2 / NF-271, first slice).

A pure **assembler** over already-sealed evidence: it gathers the four model-risk pillars a
supervisor expects — development, independent validation, ongoing monitoring, and the
model-inventory entry — and marks each ``complete`` / ``partial`` / ``missing`` with a source
reference or a machine-readable reason. Per ADR-0159 D1 it emits the shared completeness idiom
(present / partial / missing, no field silently omitted or **fabricated**) and per D2 it is *an
assembler over sealed facts, never a validation itself and never a model-risk rating* — there is no
verdict/score field.

SR 26-2 carries the SR 11-7 pillars forward
(<https://www.federalreserve.gov/supervisionreg/srletters/SR2602.pdf>); the regime tag is
version-pinned. This first slice is the pure exporter over supplied evidence refs; the collector
that gathers those refs (signed eval cards NF-002…010, the ADR-0058 validation record, drift runs
NF-151…160, the model-inventory entry) is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

#: Version-pinned regime tag (carries the SR 11-7 pillars forward).
SR_REGIME = "SR 26-2 (2026-04-17)"

#: The four model-risk pillars, in fixed order.
MODEL_RISK_PILLARS: tuple[str, ...] = (
    "development",
    "independent_validation",
    "ongoing_monitoring",
    "model_inventory",
)

_MISSING_REASON = "no {pillar} evidence found in sealed capsules"
_PARTIAL_REASON = "{pillar} evidence present but incomplete"


class PillarEvidence(BaseModel):
    pillar: str
    status: str  # "complete" | "partial" | "missing"
    source_refs: list[str] = []  # capsule-fact refs (empty when missing — never fabricated)
    reason: str | None = None  # machine-readable reason for partial/missing
    # Intentionally NO verdict/score field — this records presence, never an assessment.


class ModelRiskFile(BaseModel):
    regime: str
    model_id: str
    pillars: list[PillarEvidence]
    summary: dict[str, int]  # counts of complete / partial / missing
    # Intentionally NO rating field — assemble, never assess (ADR-0159 D2).


def _pillar(name: str, refs: Iterable[str], *, is_partial: bool) -> PillarEvidence:
    ref_list = list(refs)
    if not ref_list:
        return PillarEvidence(pillar=name, status="missing", source_refs=[],
                              reason=_MISSING_REASON.format(pillar=name))
    if is_partial:
        return PillarEvidence(pillar=name, status="partial", source_refs=ref_list,
                              reason=_PARTIAL_REASON.format(pillar=name))
    return PillarEvidence(pillar=name, status="complete", source_refs=ref_list)


def build_model_risk_file(
    *,
    model_id: str,
    development: Iterable[str] = (),
    independent_validation: Iterable[str] = (),
    ongoing_monitoring: Iterable[str] = (),
    model_inventory: Iterable[str] = (),
    partial: Iterable[str] = (),
) -> ModelRiskFile:
    """Assemble the model-risk evidence file for ``model_id`` from per-pillar source refs.

    Each pillar's refs mark it ``complete``; an empty pillar is ``missing`` (with a reason); a
    pillar named in ``partial`` (and non-empty) is ``partial``. Nothing is fabricated or
    adjudicated.
    """
    partial_set = set(partial)
    inputs = {
        "development": development,
        "independent_validation": independent_validation,
        "ongoing_monitoring": ongoing_monitoring,
        "model_inventory": model_inventory,
    }
    pillars = [
        _pillar(name, inputs[name], is_partial=name in partial_set)
        for name in MODEL_RISK_PILLARS
    ]
    summary = {"complete": 0, "partial": 0, "missing": 0}
    for p in pillars:
        summary[p.status] += 1
    return ModelRiskFile(regime=SR_REGIME, model_id=model_id, pillars=pillars, summary=summary)
