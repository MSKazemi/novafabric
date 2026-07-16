"""Deterministic dot/mermaid text exports (ADR-0124 P2)."""

from __future__ import annotations

from agent_graph.conftest import CapsuleFactory, model_call, span, tool_call
from novafabric.agent_graph import build_agent_graph, to_dot, to_mermaid

MC = "01HXAY7M6FN9TQGE0V0M7PAY1Q"
TC1 = "01HXAY7M7QM4YZ2K7N9DPBYK2W"
TC2 = "01HXAY7M8RT2YZ2K7N9DPBYK2W"
SP = "9b2d04dac8e91f3a"


def _capsule(make_capsule: CapsuleFactory) -> object:
    return make_capsule(
        spans=[span(SP, None)],
        model_calls=[model_call(MC, SP)],
        tool_calls=[
            tool_call(TC1, SP, MC),
            tool_call(TC2, SP, MC, tool_name="git.status", started_at="2026-05-07T10:23:01.001Z"),
        ],
    )


def test_dot_export_is_deterministic_and_faithful(make_capsule: CapsuleFactory) -> None:
    capsule = _capsule(make_capsule)
    graph = build_agent_graph(capsule)  # type: ignore[arg-type]
    dot = to_dot(graph)
    assert dot == to_dot(build_agent_graph(capsule))  # type: ignore[arg-type]
    assert dot.startswith("digraph agent_execution_graph {")
    assert dot.endswith("}\n")
    for node_id in (MC, TC1, TC2, SP):
        assert node_id in dot
    assert 'label="claude-sonnet-4-7"' in dot
    assert dot.count('[label="span_parent"]') == 3
    assert dot.count('[label="invokes", style=dashed]') == 2
    assert dot.count('[label="follows", style=dotted]') == 1
    # span_parent is drawn parent -> child.
    assert f'"{SP}" -> "{MC}" [label="span_parent"];' in dot


def test_dot_escapes_quotes_in_labels(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(spans=[span(SP, None, name='evil "name"')])
    dot = to_dot(build_agent_graph(capsule))
    assert 'label="evil \\"name\\""' in dot


def test_mermaid_export_is_deterministic_and_faithful(
    make_capsule: CapsuleFactory,
) -> None:
    capsule = _capsule(make_capsule)
    graph = build_agent_graph(capsule)  # type: ignore[arg-type]
    mermaid = to_mermaid(graph)
    assert mermaid == to_mermaid(build_agent_graph(capsule))  # type: ignore[arg-type]
    lines = mermaid.splitlines()
    assert lines[0] == "graph TD"
    # One alias definition per node (canonical order), then the edges.
    node_lines = [line for line in lines if '["' in line]
    assert len(node_lines) == len(graph.nodes)
    assert mermaid.count("-.->|invokes|") == 2
    assert mermaid.count("==>|follows|") == 1
    assert mermaid.count("-->") >= 3  # span_parent drawn parent --> child


def test_mermaid_escapes_brackets_and_quotes(make_capsule: CapsuleFactory) -> None:
    capsule = make_capsule(spans=[span(SP, None, name='a["b"]')])
    mermaid = to_mermaid(build_agent_graph(capsule))
    assert '"a#91;#quot;b#quot;#93;"' in mermaid
