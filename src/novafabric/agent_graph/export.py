"""Deterministic text exports of an agent execution graph (ADR-0124 P2).

Pure stdlib, no rendering dependency. Both exports are faithful, deterministic
serialisations of the same canonical nodes and edges (the JSON document stays
the source of truth). Following the spec's worked example, ``span_parent``
edges — stored child→parent — are *drawn* parent→child for readability; the
underlying edge set is unchanged.
"""

from __future__ import annotations

from novafabric.agent_graph.model import AgentExecutionGraph, GraphEdge

_DOT_SHAPES = {
    "model_call": "box",
    "tool_call": "ellipse",
    "span": "folder",
    "root": "circle",
}


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def to_dot(graph: AgentExecutionGraph) -> str:
    """Graphviz DOT export (canonical node/edge order; stable output bytes)."""
    lines = ["digraph agent_execution_graph {", "  rankdir=TB;"]
    for node in graph.nodes:
        lines.append(
            f'  "{_dot_escape(node.id)}" '
            f'[label="{_dot_escape(node.label)}", '
            f'shape={_DOT_SHAPES[node.kind]}];'
        )
    for edge in graph.edges:
        lines.append(_dot_edge(edge))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _dot_edge(edge: GraphEdge) -> str:
    src, dst = _dot_escape(edge.from_), _dot_escape(edge.to)
    if edge.type == "span_parent":
        return f'  "{dst}" -> "{src}" [label="span_parent"];'
    if edge.type == "agent_invokes_tool":
        return f'  "{src}" -> "{dst}" [label="invokes", style=dashed];'
    return f'  "{src}" -> "{dst}" [label="follows", style=dotted];'


def _mermaid_escape(text: str) -> str:
    return text.replace('"', "#quot;").replace("[", "#91;").replace("]", "#93;")


def to_mermaid(graph: AgentExecutionGraph) -> str:
    """Mermaid ``graph TD`` export (canonical node/edge order; stable aliases)."""
    alias = {node.id: f"n{index}" for index, node in enumerate(graph.nodes)}
    lines = ["graph TD"]
    for node in graph.nodes:
        lines.append(f'  {alias[node.id]}["{_mermaid_escape(node.label)}"]')
    for edge in graph.edges:
        src, dst = alias[edge.from_], alias[edge.to]
        if edge.type == "span_parent":
            lines.append(f"  {dst} --> {src}")
        elif edge.type == "agent_invokes_tool":
            lines.append(f"  {src} -.->|invokes| {dst}")
        else:
            lines.append(f"  {src} ==>|follows| {dst}")
    return "\n".join(lines) + "\n"
