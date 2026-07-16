"""Tests for drift root-cause linkage (ADR-0147 D5 / NF-157)."""
from __future__ import annotations

from novafabric.drift.root_cause import (
    RootCauseHypothesis,
    find_root_cause,
)


def _node(kind: str, ref: str) -> dict[str, str]:
    return {"kind": kind, "ref": ref}


def test_single_changed_model_is_sole_change() -> None:
    baseline = [_node("model", "gpt-4o@1"), _node("prompt", "p@1")]
    drifted = [_node("model", "gpt-4o@2"), _node("prompt", "p@1")]
    h = find_root_cause(baseline, drifted)
    assert isinstance(h, RootCauseHypothesis)
    assert h.confidence == "sole_change"
    assert len(h.changes) == 1
    change = h.changes[0]
    assert change.kind == "model"
    assert change.added == ["gpt-4o@2"]
    assert change.removed == ["gpt-4o@1"]


def test_correlation_only_is_forced_true() -> None:
    h = find_root_cause([_node("model", "m@1")], [_node("model", "m@2")])
    assert h.correlation_only is True


def test_no_change_when_provenance_identical() -> None:
    same = [_node("model", "m@1"), _node("prompt", "p@1")]
    h = find_root_cause(same, list(same))
    assert h.confidence == "no_change"
    assert h.changes == []


def test_multiple_changed_dimensions() -> None:
    baseline = [_node("model", "m@1"), _node("prompt", "p@1"), _node("tool", "t@1")]
    drifted = [_node("model", "m@2"), _node("prompt", "p@2"), _node("tool", "t@1")]
    h = find_root_cause(baseline, drifted)
    assert h.confidence == "multiple_changes"
    assert {c.kind for c in h.changes} == {"model", "prompt"}


def test_added_and_removed_tools_are_tracked() -> None:
    baseline = [_node("tool", "search@1")]
    drifted = [_node("tool", "search@1"), _node("tool", "browser@1")]
    h = find_root_cause(baseline, drifted)
    change = next(c for c in h.changes if c.kind == "tool")
    assert change.added == ["browser@1"]
    assert change.removed == []


def test_only_configured_kinds_considered() -> None:
    # A dataset change is ignored when kinds is restricted to model/prompt/tool.
    baseline = [_node("dataset", "d@1"), _node("model", "m@1")]
    drifted = [_node("dataset", "d@2"), _node("model", "m@1")]
    h = find_root_cause(baseline, drifted, kinds=("model", "prompt", "tool"))
    assert h.confidence == "no_change"


def test_refs_sorted_for_determinism() -> None:
    baseline = [_node("tool", "b@1"), _node("tool", "a@1")]
    drifted = [_node("tool", "a@1"), _node("tool", "b@1")]
    h = find_root_cause(baseline, drifted)
    # Same set → no change, regardless of input order.
    assert h.confidence == "no_change"


def test_no_verdict_or_causation_field() -> None:
    h = find_root_cause([_node("model", "m@1")], [_node("model", "m@2")])
    forbidden = {"caused", "cause_proven", "verdict", "root", "responsible", "blame"}
    assert forbidden.isdisjoint(h.model_dump().keys())
