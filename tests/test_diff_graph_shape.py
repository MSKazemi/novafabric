"""Unit tests for the ADR-0124 P3 agent-graph shape pre-check (novafabric.diff.graph_shape)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.agent_graph import build_agent_graph
from novafabric.diff import graph_shape as gs
from novafabric.diff.graph_shape import (
    GraphShapeDiff,
    compare_graph_shapes,
    format_graph_shape_annotations,
    format_graph_shape_text,
    project_shape,
)


def _jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def make_run(
    root: Path,
    name: str,
    *,
    run_id: str,
    prefix: str,
    tools: tuple[str, ...] = ("web_search", "read_file"),
    t0: int = 0,
    model: str = "m-1",
) -> Path:
    """A capsule: one agent span > one model call > N tool calls (ids prefixed)."""
    cap = root / name
    cap.mkdir()
    (cap / "capsule.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    _jsonl(
        cap / "trace.jsonl",
        [
            {
                "span_id": f"{prefix}-s1",
                "parent_span_id": None,
                "name": "agent.turn",
                "started_at": f"2026-05-07T10:00:0{t0}.000Z",
                "duration_ms": 1000 + t0,
            }
        ],
    )
    _jsonl(
        cap / "model-calls.jsonl",
        [
            {
                "model_call_id": f"{prefix}-m1",
                "parent_span_id": f"{prefix}-s1",
                "gen_ai.request.model": model,
                "started_at": f"2026-05-07T10:00:0{t0}.100Z",
                "duration_ms": 500 + t0,
            }
        ],
    )
    _jsonl(
        cap / "tool-calls.jsonl",
        [
            {
                "tool_call_id": f"{prefix}-t{i}",
                "parent_span_id": f"{prefix}-s1",
                "agent_call_id": f"{prefix}-m1",
                "tool_name": tool,
                "started_at": f"2026-05-07T10:00:0{t0}.{200 + i:03d}Z",
                "duration_ms": 50 + t0,
            }
            for i, tool in enumerate(tools)
        ],
    )
    return cap


def test_same_capsule_twice_is_same_shape_via_graph_digest(tmp_path: Path) -> None:
    cap = make_run(tmp_path, "a", run_id="RA", prefix="a")
    diff = compare_graph_shapes(cap, cap)
    assert diff.status == "same_shape"
    assert diff.same_shape
    assert diff.graph_digest_equal is True
    assert diff.a.graph_digest == diff.b.graph_digest


def test_distinct_runs_same_topology_are_same_shape(tmp_path: Path) -> None:
    # Different run ids, record ids and timings: graph_digest differs, shape does not.
    a = make_run(tmp_path, "a", run_id="RA", prefix="a", t0=0)
    b = make_run(tmp_path, "b", run_id="RB", prefix="zz", t0=3)
    diff = compare_graph_shapes(a, b)
    assert diff.status == "same_shape"
    assert diff.graph_digest_equal is False
    assert diff.a.graph_digest != diff.b.graph_digest
    assert diff.a.shape_digest == diff.b.shape_digest
    assert diff.a.nodes_by_kind == {"model_call": 1, "span": 1, "tool_call": 2}
    assert not diff.nodes_added and not diff.edges_removed


def test_added_tool_call_is_reported_with_path_and_id(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", tools=("web_search", "read_file", "git"))
    diff = compare_graph_shapes(a, b)
    assert diff.status == "shape_changed"
    assert diff.counts.nodes_added == 1 and diff.counts.nodes_removed == 0
    added = diff.nodes_added[0]
    assert added.kind == "tool_call" and added.label == "git" and added.node_id == "b-t2"
    assert added.path == "span:agent.turn[0]/tool_call:git[0]"
    edge_types = sorted(e.type for e in diff.edges_added)
    assert edge_types == ["agent_invokes_tool", "follows", "span_parent"]
    assert diff.counts.edges_removed == 0  # appended last: read_file -> git follows is new
    follows = next(e for e in diff.edges_added if e.type == "follows")
    assert follows.from_path.endswith("tool_call:read_file[0]")
    assert not diff.truncated


def test_removed_and_relabelled_nodes(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a", model="m-1")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", model="m-2", tools=("web_search",))
    diff = compare_graph_shapes(a, b)
    assert diff.status == "shape_changed"
    removed = {(d.kind, d.label) for d in diff.nodes_removed}
    added = {(d.kind, d.label) for d in diff.nodes_added}
    assert ("model_call", "m-1") in removed and ("tool_call", "read_file") in removed
    # The surviving tool is re-keyed under the new model's subtree only via edges;
    # its structural position (span > tool_call:web_search[0]) is unchanged.
    assert added == {("model_call", "m-2")}


def test_deltas_are_bounded_and_counts_are_true_totals(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a", tools=())
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", tools=tuple(f"t{i:02d}" for i in range(7)))
    diff = compare_graph_shapes(a, b, limit=3)
    assert diff.counts.nodes_added == 7
    assert len(diff.nodes_added) == 3
    assert diff.truncated
    assert [d.label for d in diff.nodes_added] == ["t00", "t01", "t02"]
    text = format_graph_shape_text(diff)
    assert "… 4 more not shown (limit 3)" in text


def test_limit_is_clamped(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    assert compare_graph_shapes(a, a, limit=-5).limit == 0
    assert compare_graph_shapes(a, a, limit=10**9).limit == gs.MAX_DELTA_LIMIT


def test_output_is_deterministic(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", tools=("x", "y", "z", "web_search"))
    first = json.dumps(compare_graph_shapes(a, b).to_document(), sort_keys=True)
    second = json.dumps(compare_graph_shapes(a, b).to_document(), sort_keys=True)
    assert first == second


def test_not_a_capsule_is_unavailable_not_raised(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    empty = tmp_path / "empty"
    empty.mkdir()
    diff = compare_graph_shapes(a, empty)
    assert diff.status == "unavailable"
    assert diff.a.available and not diff.b.available
    assert diff.b.reason and "CapsuleNotFoundError" in diff.b.reason
    assert not diff.same_shape
    text = format_graph_shape_text(diff)
    assert "graph unavailable" in text and "B: graph unavailable" in text


def test_missing_path_is_unavailable(tmp_path: Path) -> None:
    diff = compare_graph_shapes(tmp_path / "nope", tmp_path / "nope2")
    assert diff.status == "unavailable"
    assert not diff.a.available and not diff.b.available


def test_oversize_sources_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    monkeypatch.setattr(gs, "MAX_SOURCE_BYTES", 10)
    diff = compare_graph_shapes(a, a)
    assert diff.status == "unavailable"
    assert diff.a.reason is not None and "byte cap" in diff.a.reason


def test_node_cap_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    monkeypatch.setattr(gs, "MAX_GRAPH_NODES", 2)
    diff = compare_graph_shapes(a, a)
    assert diff.status == "unavailable"
    assert diff.a.reason is not None and "node cap" in diff.a.reason


def test_unexpected_builder_failure_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")

    def boom(_: Path) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(gs, "build_agent_graph", boom)
    diff = compare_graph_shapes(a, a)
    assert diff.status == "unavailable"
    assert diff.a.reason == "RecursionError: maximum recursion depth exceeded"


def test_malformed_records_still_build_best_effort(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b")
    with (b / "tool-calls.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n[1,2]\n")
    diff = compare_graph_shapes(a, b)
    assert diff.status == "same_shape"


def test_orphan_tool_call_attaches_to_root_and_is_diffed(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b")
    with (b / "tool-calls.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool_call_id": "orphan", "tool_name": "rm"}) + "\n")
    diff = compare_graph_shapes(a, b)
    assert diff.status == "shape_changed"
    paths = {d.path for d in diff.nodes_added}
    assert "root:root[0]/tool_call:rm[0]" in paths
    assert "root:root[0]" in paths
    assert diff.b.reconstruction_note_count >= 1


def test_control_characters_and_long_labels_are_sanitised(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a", tools=())
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", tools=("evil\n::error::x" + "y" * 200,))
    diff = compare_graph_shapes(a, b)
    label = diff.nodes_added[0].label
    assert "\n" not in label and "\\x0a" in label
    assert len(label) == gs._MAX_LABEL_CHARS and label.endswith("…")
    assert "\n::error" not in format_graph_shape_text(diff)


def test_deep_path_is_elided(tmp_path: Path) -> None:
    cap = tmp_path / "deep"
    cap.mkdir()
    (cap / "capsule.json").write_text(json.dumps({"run_id": "R"}), encoding="utf-8")
    spans = [
        {"span_id": f"s{i:02d}", "parent_span_id": None if i == 0 else f"s{i - 1:02d}", "name": "n"}
        for i in range(20)
    ]
    _jsonl(cap / "trace.jsonl", spans)
    shape = project_shape(build_agent_graph(cap))
    path = gs._path(shape, "s19")
    assert path.startswith("…/")
    assert path.count("/") == gs._MAX_PATH_SEGMENTS  # "…" + 12 segments
    # Exactly-at-cap + 1 depth is also marked as elided.
    assert gs._path(shape, f"s{gs._MAX_PATH_SEGMENTS:02d}").startswith("…/")
    assert not gs._path(shape, f"s{gs._MAX_PATH_SEGMENTS - 1:02d}").startswith("…/")


def test_follows_cycle_is_rejected() -> None:
    from novafabric.agent_graph.model import AgentExecutionGraph, GraphNode, make_edge

    nodes = [
        GraphNode(id=i, kind="span", label="n", started_at=None, duration_ms=None)
        for i in ("x", "y")
    ]
    graph = AgentExecutionGraph.assemble(
        "c", nodes, [make_edge("follows", "x", "y"), make_edge("follows", "y", "x")]
    )
    with pytest.raises(gs.GraphShapeError):
        project_shape(graph)


def test_span_parent_cycle_is_rejected() -> None:
    from novafabric.agent_graph.model import AgentExecutionGraph, GraphNode, make_edge

    nodes = [
        GraphNode(id=i, kind="span", label="n", started_at=None, duration_ms=None)
        for i in ("x", "y")
    ]
    graph = AgentExecutionGraph.assemble(
        "c", nodes, [make_edge("span_parent", "x", "y"), make_edge("span_parent", "y", "x")]
    )
    with pytest.raises(gs.GraphShapeError, match="forest"):
        project_shape(graph)


def test_annotations(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", tools=("q",))
    same = format_graph_shape_annotations(compare_graph_shapes(a, a))
    assert same[0].startswith("::notice title=NovaFabric Graph Shape::")
    changed = format_graph_shape_annotations(compare_graph_shapes(a, b))
    assert changed[0].startswith("::error ") and "nodes +1/-2" in changed[0]
    unavailable = format_graph_shape_annotations(compare_graph_shapes(a, tmp_path / "missing"))
    assert unavailable[0].startswith("::warning ") and "B: " in unavailable[0]


def test_text_same_shape_mentions_basis(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b")
    text = format_graph_shape_text(compare_graph_shapes(a, b))
    assert text.startswith("Graph shape (ADR-0124, experimental): same shape")
    assert "equal shape_digest: sha256:" in text
    assert "identical graph_digest" in format_graph_shape_text(compare_graph_shapes(a, a))


def test_empty_graphs_are_same_shape(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    for name in ("x", "y"):
        cap = tmp_path / name
        cap.mkdir()
        (cap / "capsule.json").write_text(json.dumps({"run_id": name}), encoding="utf-8")
    diff = compare_graph_shapes(tmp_path / "x", tmp_path / "y")
    assert diff.status == "same_shape" and diff.a.node_count == 0
    assert "empty" in format_graph_shape_text(diff)
    assert compare_graph_shapes(a, tmp_path / "x").counts.nodes_removed == 4


def test_document_roundtrips_through_model(tmp_path: Path) -> None:
    a = make_run(tmp_path, "a", run_id="RA", prefix="a")
    b = make_run(tmp_path, "b", run_id="RB", prefix="b", tools=("q",))
    doc = compare_graph_shapes(a, b).to_document()
    assert GraphShapeDiff.model_validate(doc).to_document() == doc
    assert doc["version"] == gs.GRAPH_SHAPE_VERSION
