"""Drift root-cause linkage over captured lineage provenance (ADR-0147 D5 / NF-157).

When an output/behavioral drift is observed (see :mod:`novafabric.drift.detectors`), this module
links it to the **nearest changed input** by diffing the lineage provenance ancestors of a baseline
run against those of a drifted run — which model, prompt asset, or tool version changed between the
two. The result is a **correlation, not a cause**: ``correlation_only`` is forced ``True`` and there
is deliberately no caused/cause_proven/verdict/blame field. A changed input that co-occurs with a
drift is a hypothesis to investigate, never a proven root cause.

``confidence`` is a **descriptive category, not a grade**: ``no_change`` (provenance identical over
the configured kinds), ``sole_change`` (exactly one kind changed — the strongest hypothesis), or
``multiple_changes`` (several changed — the signal is diffuse).

This first slice diffs two supplied provenance-node lists (each ``{kind, ref}``); the collector that
reads them from the lineage store — ``LineageStore.provenance(run_ref, ...)`` — for a baseline and a
drifted run is a documented follow-on. No capsule mutation, no capture-path change, and it only
reads lineage; it never writes to the store.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

#: Provenance node kinds whose change is a candidate drift root cause, by default.
DEFAULT_KINDS: tuple[str, ...] = ("model", "prompt", "tool", "dataset")


class RootCauseChange(BaseModel):
    kind: str
    baseline_refs: list[str]  # sorted
    drifted_refs: list[str]  # sorted
    added: list[str]  # in drifted, not baseline (sorted)
    removed: list[str]  # in baseline, not drifted (sorted)


class RootCauseHypothesis(BaseModel):
    changes: list[RootCauseChange]
    confidence: str  # "no_change" | "sole_change" | "multiple_changes" — a category, not a grade
    correlation_only: bool = True  # forced True — a co-occurrence, never a proven cause
    # Intentionally NO caused/cause_proven/verdict/blame field — surfaces a hypothesis only.


def _refs_by_kind(nodes: Sequence[Mapping[str, object]], kinds: set[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {k: set() for k in kinds}
    for node in nodes:
        kind = node.get("kind")
        ref = node.get("ref")
        if not isinstance(kind, str) or not isinstance(ref, str):
            raise ValueError("each provenance node needs a string kind and ref")
        if kind in kinds:
            out[kind].add(ref)
    return out


def find_root_cause(
    baseline: Sequence[Mapping[str, object]],
    drifted: Sequence[Mapping[str, object]],
    *,
    kinds: Sequence[str] = DEFAULT_KINDS,
) -> RootCauseHypothesis:
    """Diff two runs' provenance ancestors to the input kinds that changed between them.

    ``baseline`` and ``drifted`` are provenance-node lists (each ``{kind, ref}``). For every kind in
    ``kinds`` whose ref set differs between the two runs, a :class:`RootCauseChange` records the
    before/after refs and the added/removed refs. ``confidence`` categorizes the diff (no_change /
    sole_change / multiple_changes). The hypothesis is ``correlation_only`` — a changed input that
    co-occurs with the drift, not a proven cause.
    """
    kind_set = set(kinds)
    base_by_kind = _refs_by_kind(baseline, kind_set)
    drift_by_kind = _refs_by_kind(drifted, kind_set)

    changes: list[RootCauseChange] = []
    for kind in sorted(kind_set):
        base_refs = base_by_kind[kind]
        drift_refs = drift_by_kind[kind]
        if base_refs == drift_refs:
            continue
        changes.append(RootCauseChange(
            kind=kind,
            baseline_refs=sorted(base_refs),
            drifted_refs=sorted(drift_refs),
            added=sorted(drift_refs - base_refs),
            removed=sorted(base_refs - drift_refs),
        ))

    if not changes:
        confidence = "no_change"
    elif len(changes) == 1:
        confidence = "sole_change"
    else:
        confidence = "multiple_changes"

    return RootCauseHypothesis(changes=changes, confidence=confidence)
