"""Behavioral tests for the ADR-0124 reconstruction algorithm (normative rules)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from agent_graph.conftest import (
    CAPSULE_RUN_ID,
    CapsuleFactory,
    model_call,
    span,
    tool_call,
)
from novafabric.agent_graph import (
    AgentExecutionGraph,
    CapsuleNotFoundError,
    build_agent_graph,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "agent-execution-graph.schema.json"
)

MC = "01HXAY7M6FN9TQGE0V0M7PAY1Q"
TC1 = "01HXAY7M7QM4YZ2K7N9DPBYK2W"
TC2 = "01HXAY7M8RT2YZ2K7N9DPBYK2W"
SP = "9b2d04dac8e91f3a"


def _edges(graph: AgentExecutionGraph, edge_type: str) -> set[tuple[str, str]]:
    return {(e.from_, e.to) for e in graph.edges if e.type == edge_type}


def _assert_dag(graph: AgentExecutionGraph) -> None:
    adjacency: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for edge in graph.edges:
        adjacency[edge.from_].append(edge.to)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adjacency, WHITE)

    def visit(node: str) -> None:
        color[node] = GRAY
        for succ in adjacency[node]:
            assert color[succ] != GRAY, f"cycle through {node} -> {succ}"
            if color[succ] == WHITE:
                visit(succ)
        color[node] = BLACK

    for node in adjacency:
        if color[node] == WHITE:
            visit(node)


def test_linear_run(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        spans=[span(SP, None)],
        model_calls=[model_call(MC, SP)],
        tool_calls=[tool_call(TC1, SP, MC)],
    )
    graph = build_agent_graph(capsule)

    assert graph.capsule_id == CAPSULE_RUN_ID
    assert {n.id: n.kind for n in graph.nodes} == {
        SP: "span",
        MC: "model_call",
        TC1: "tool_call",
    }
    assert _edges(graph, "span_parent") == {(MC, SP), (TC1, SP)}
    assert _edges(graph, "agent_invokes_tool") == {(MC, TC1)}
    assert _edges(graph, "follows") == set()  # follows never crosses kinds
    assert graph.reconstruction_notes is None  # fully linked capsule
    assert not any(n.id == "root" for n in graph.nodes)  # no synthetic root needed

    by_id = {n.id: n for n in graph.nodes}
    assert by_id[MC].label == "claude-sonnet-4-7"
    assert by_id[MC].status == "success"
    assert by_id[MC].source_ref == "model-calls.jsonl#L1"
    assert by_id[TC1].label == "web_search"
    assert by_id[TC1].mutation_class == "read-only"
    assert by_id[TC1].source_ref == "tool-calls.jsonl#L1"
    assert by_id[SP].label == "agent.turn"
    assert by_id[SP].status is None  # span "ok" is outside the node status domain
    assert by_id[SP].source_ref == "trace.jsonl#L1"
    _assert_dag(graph)


def test_branching_fan_out_orders_follows_by_started_at(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(
        spans=[span(SP, None)],
        model_calls=[model_call(MC, SP)],
        tool_calls=[
            tool_call(TC1, SP, MC, started_at="2026-05-07T10:23:00.234Z"),
            tool_call(TC2, SP, MC, tool_name="git.status", started_at="2026-05-07T10:23:01.001Z"),
        ],
    )
    graph = build_agent_graph(capsule)
    assert _edges(graph, "agent_invokes_tool") == {(MC, TC1), (MC, TC2)}
    assert _edges(graph, "follows") == {(TC1, TC2)}
    assert graph.stats is not None
    assert graph.stats.node_count == 4
    assert graph.stats.edge_count == 6
    assert graph.stats.max_depth == 2
    assert graph.stats.max_fan_out == 3
    _assert_dag(graph)


def test_follows_tie_breaks_by_source_file_order(make_capsule: CapsuleFactory) -> None:
    same_ts = "2026-05-07T10:23:00.000Z"
    capsule = make_capsule(
        spans=[span(SP, None)],
        tool_calls=[
            tool_call(TC2, SP, None, started_at=same_ts),
            tool_call(TC1, SP, None, started_at=same_ts),
        ],
    )
    graph = build_agent_graph(capsule)
    # TC2 appears first in the file, so it precedes TC1 despite equal started_at.
    assert _edges(graph, "follows") == {(TC2, TC1)}


def test_missing_parent_span_attaches_to_root_with_note(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(model_calls=[model_call(MC, "deadbeefdeadbeef")])
    graph = build_agent_graph(capsule)
    assert any(n.id == "root" and n.kind == "root" for n in graph.nodes)
    assert _edges(graph, "span_parent") == {(MC, "root")}
    assert graph.reconstruction_notes is not None
    (note,) = graph.reconstruction_notes
    assert note.kind == "missing_parent"
    assert note.node_id == MC
    assert "deadbeefdeadbeef" in note.detail
    _assert_dag(graph)


def test_model_call_without_any_parent_is_flagged(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(model_calls=[model_call(MC, None)])
    graph = build_agent_graph(capsule)
    assert _edges(graph, "span_parent") == {(MC, "root")}
    assert graph.reconstruction_notes is not None
    assert graph.reconstruction_notes[0].kind == "missing_parent"


def test_orphan_tool_call_null_agent_and_no_parent(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        tool_calls=[
            tool_call(TC1, None, None, tool_name="send_email", mutation_class="external-side-effect")
        ]
    )
    graph = build_agent_graph(capsule)
    assert _edges(graph, "span_parent") == {(TC1, "root")}
    assert _edges(graph, "agent_invokes_tool") == set()
    assert graph.reconstruction_notes is not None
    (note,) = graph.reconstruction_notes
    assert note.kind == "orphan_tool_call"
    assert note.node_id == TC1
    _assert_dag(graph)


def test_dangling_agent_call_id_yields_orphan_note(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(tool_calls=[tool_call(TC1, None, MC)])  # MC not in capsule
    graph = build_agent_graph(capsule)
    assert _edges(graph, "agent_invokes_tool") == set()
    assert _edges(graph, "span_parent") == {(TC1, "root")}
    assert graph.reconstruction_notes is not None
    (note,) = graph.reconstruction_notes
    assert note.kind == "orphan_tool_call"
    assert MC in note.detail


def test_dangling_agent_with_real_parent_keeps_span_attachment(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(
        spans=[span(SP, None)],
        tool_calls=[tool_call(TC1, SP, MC)],  # agent link dangles, parent is real
    )
    graph = build_agent_graph(capsule)
    assert _edges(graph, "span_parent") == {(TC1, SP)}
    assert not any(n.id == "root" for n in graph.nodes)
    assert graph.reconstruction_notes is not None
    assert graph.reconstruction_notes[0].kind == "orphan_tool_call"


def test_tool_call_null_agent_with_real_parent_is_not_orphan(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(spans=[span(SP, None)], tool_calls=[tool_call(TC1, SP, None)])
    graph = build_agent_graph(capsule)
    assert _edges(graph, "span_parent") == {(TC1, SP)}
    assert graph.reconstruction_notes is None


def test_self_parent_span_is_broken_to_root(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(spans=[span(SP, SP)])
    graph = build_agent_graph(capsule)
    assert _edges(graph, "span_parent") == {(SP, "root")}
    assert graph.reconstruction_notes is not None
    (note,) = graph.reconstruction_notes
    assert note.kind == "missing_parent"
    assert "itself" in note.detail
    _assert_dag(graph)


def test_two_span_cycle_is_broken_deterministically(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        spans=[span("aaaa000000000001", "bbbb000000000002"), span("bbbb000000000002", "aaaa000000000001")]
    )
    graph = build_agent_graph(capsule)
    _assert_dag(graph)
    # The byte-smallest node in the cycle is re-attached to root.
    assert ("aaaa000000000001", "root") in _edges(graph, "span_parent")
    assert ("bbbb000000000002", "aaaa000000000001") in _edges(graph, "span_parent")
    assert graph.reconstruction_notes is not None
    assert any(n.kind == "unlinked_span" for n in graph.reconstruction_notes)


def test_parent_pointing_at_non_span_record_is_flagged(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(
        model_calls=[model_call(MC, None)],
        tool_calls=[tool_call(TC1, MC, MC)],  # parent_span_id points at a model call
    )
    graph = build_agent_graph(capsule)
    _assert_dag(graph)
    assert (TC1, "root") in _edges(graph, "span_parent")
    assert graph.reconstruction_notes is not None
    assert any(
        n.kind == "missing_parent" and n.node_id == TC1 and "non-span" in n.detail
        for n in graph.reconstruction_notes
    )
    # The model->tool causal link is still real and kept.
    assert _edges(graph, "agent_invokes_tool") == {(MC, TC1)}


def test_determinism_same_capsule_identical_output(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        spans=[span(SP, None)],
        model_calls=[model_call(MC, SP)],
        tool_calls=[tool_call(TC1, SP, MC), tool_call(TC2, SP, MC)],
    )
    first = build_agent_graph(capsule)
    second = build_agent_graph(capsule)
    assert first.graph_digest == second.graph_digest
    assert json.dumps(first.to_document(), sort_keys=True) == json.dumps(
        second.to_document(), sort_keys=True
    )


def test_empty_capsule_yields_empty_graph(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule()
    graph = build_agent_graph(capsule)
    assert graph.nodes == []
    assert graph.edges == []
    assert graph.reconstruction_notes is None
    assert graph.stats is not None
    assert graph.stats.node_count == 0
    assert graph.stats.max_depth == 0
    assert graph.graph_digest.startswith("sha256:")


def test_capsule_without_manifest_uses_directory_name(tmp_path: Path) -> None:
    capsule = tmp_path / "01OLDCAPSULE00000000000000"
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text(
        json.dumps(model_call(MC, None)) + "\n", encoding="utf-8"
    )
    graph = build_agent_graph(capsule)
    assert graph.capsule_id == "01OLDCAPSULE00000000000000"


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(CapsuleNotFoundError):
        build_agent_graph(tmp_path / "nope")


def test_non_capsule_directory_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CapsuleNotFoundError):
        build_agent_graph(empty)


def test_malformed_jsonl_lines_are_skipped(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        raw_lines={
            "model-calls.jsonl": [
                "{not json",
                json.dumps(model_call(MC, None)),
                '"just a string"',
                "",
            ]
        }
    )
    graph = build_agent_graph(capsule)
    assert [n.id for n in graph.nodes if n.kind == "model_call"] == [MC]
    by_id = {n.id: n for n in graph.nodes}
    assert by_id[MC].source_ref == "model-calls.jsonl#L2"


def test_missing_fields_become_null_and_fallback_labels(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(
        raw_lines={
            "tool-calls.jsonl": [json.dumps({"tool_call_id": TC1})],
            "trace.jsonl": [json.dumps({"span_id": SP})],
        }
    )
    graph = build_agent_graph(capsule)
    by_id = {n.id: n for n in graph.nodes}
    assert by_id[TC1].label == "tool_call"
    assert by_id[TC1].started_at is None
    assert by_id[TC1].duration_ms is None
    assert by_id[SP].label == "span"


def test_span_error_status_is_mirrored(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(spans=[span(SP, None, status="error")])
    graph = build_agent_graph(capsule)
    assert graph.nodes[0].status == "error"


def test_duplicate_ids_keep_first_record(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        model_calls=[model_call(MC, None, model="first"), model_call(MC, None, model="second")]
    )
    graph = build_agent_graph(capsule)
    assert [n.label for n in graph.nodes if n.kind == "model_call"] == ["first"]


def test_builder_output_validates_against_graduated_schema(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(
        spans=[span(SP, None)],
        model_calls=[model_call(MC, SP)],
        tool_calls=[tool_call(TC1, SP, MC), tool_call(TC2, None, None)],
    )
    graph = build_agent_graph(capsule)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(graph.to_document())


def test_bool_duration_and_unparsable_timestamps(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(
        spans=[span(SP, None)],
        tool_calls=[
            tool_call(TC1, SP, None, started_at="not-a-date", duration_ms=True),  # type: ignore[arg-type]
            tool_call(TC2, SP, None, started_at="also-bad"),
        ],
    )
    graph = build_agent_graph(capsule)
    by_id = {n.id: n for n in graph.nodes}
    assert by_id[TC1].duration_ms is None  # bool is not an integer duration
    # Unparsable timestamps fall back to source-file order, deterministically.
    assert _edges(graph, "follows") == {(TC1, TC2)}


def test_corrupt_manifest_falls_back_to_directory_name(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(name="01FALLBACK0000000000000000")
    (capsule / "capsule.json").write_text("{not: valid: json:", encoding="utf-8")
    graph = build_agent_graph(capsule)
    assert graph.capsule_id == "01FALLBACK0000000000000000"


def test_records_claiming_the_reserved_root_id_are_skipped(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = make_capsule(
        tool_calls=[tool_call("root", None, None)],
        spans=[span("root", None), span(SP, None)],
    )
    graph = build_agent_graph(capsule)
    assert {n.id for n in graph.nodes} == {SP}
