"""Guard: ``_max_depth`` is iterative — deep chains and malformed cycles terminate."""

from __future__ import annotations

from novafabric.agent_graph.builder import _max_depth


def test_empty_graph_has_depth_zero() -> None:
    assert _max_depth(set(), {}) == 0


def test_branching_graph_depth() -> None:
    parent_of = {"b": "a", "c": "b", "d": "a"}
    assert _max_depth({"a", "b", "c", "d"}, parent_of) == 3


def test_deep_chain_does_not_hit_recursion_limit() -> None:
    n = 20_000  # far past the default recursion limit of 1000
    ids = [f"n{i}" for i in range(n)]
    parent_of = {ids[i]: ids[i - 1] for i in range(1, n)}
    assert _max_depth(set(ids), parent_of) == n


def test_parent_cycle_terminates() -> None:
    parent_of = {"a": "b", "b": "c", "c": "a", "d": "a"}
    depth = _max_depth({"a", "b", "c", "d"}, parent_of)
    assert 3 <= depth <= 4
