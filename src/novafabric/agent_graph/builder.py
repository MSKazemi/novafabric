"""Reconstruct one capsule's agent execution graph (ADR-0124, normative algorithm).

A pure, read-only function of capsule contents: reads ``model-calls.jsonl``,
``tool-calls.jsonl``, and ``trace.jsonl``, applies the fixed rule set of
``the private design/spec/agent-execution-graph-v0.md`` §Reconstruction algorithm, and
never infers structure the records did not encode. Gaps (missing parent span,
orphan tool call, malformed self-parent/cycle) attach the node to a synthetic
``root`` with an explicit ``reconstruction_note`` — honesty over completeness.

Determinism: given the same capsule, two reconstructions yield byte-identical
canonical JSON and the same ``graph_digest``.

Pinned interpretation (v0, matches the spec's worked example and golden
fixtures): ``follows`` edges are emitted between sibling nodes that share both
the same resolved parent *and* the same ``kind`` — so the tie-break "by
source-file order" is always well-defined within one JSONL file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from novafabric.agent_graph.errors import CapsuleNotFoundError
from novafabric.agent_graph.model import (
    NODE_STATUS_VALUES,
    ROOT_NODE_ID,
    AgentExecutionGraph,
    GraphEdge,
    GraphNode,
    GraphStats,
    NodeStatus,
    ReconstructionNote,
    make_edge,
)

_MANIFEST_NAMES = ("capsule.yaml", "capsule.json")
_MODEL_CALLS = "model-calls.jsonl"
_TOOL_CALLS = "tool-calls.jsonl"
_TRACE = "trace.jsonl"


def _read_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Best-effort JSONL read: blank/unparsable/non-object lines are skipped."""
    if not path.is_file():
        return []
    records: list[tuple[int, dict[str, Any]]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append((lineno, record))
    return records


def _capsule_id(capsule_dir: Path) -> str:
    """The manifest's ``run_id`` when readable; the directory name otherwise."""
    for name in _MANIFEST_NAMES:
        path = capsule_dir / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            run_id = data.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
    return capsule_dir.name


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _duration_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _status(value: Any) -> NodeStatus | None:
    """Mirror the source record's status only when it is in the node domain."""
    if isinstance(value, str) and value in NODE_STATUS_VALUES:
        return value  # type: ignore[return-value]
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _break_cycles(
    parent_of: dict[str, str],
    notes: list[ReconstructionNote],
) -> None:
    """Break any malformed parent cycle so the output is always a DAG.

    ``parent_of`` maps each child to its single parent, so every connected
    component holds at most one cycle. The victim is the byte-smallest node id
    in the cycle (deterministic); it is re-attached to the synthetic root with
    an ``unlinked_span`` note. Never a silent repair.
    """
    settled: set[str] = set()
    for start in sorted(parent_of):
        if start in settled:
            continue
        position: dict[str, int] = {}
        path: list[str] = []
        current = start
        while current in parent_of and current not in settled:
            if current in position:
                cycle = path[position[current] :]
                victim = min(cycle, key=lambda nid: nid.encode("utf-8"))
                parent_of[victim] = ROOT_NODE_ID
                notes.append(
                    ReconstructionNote(
                        kind="unlinked_span",
                        node_id=victim,
                        detail=(
                            "span_parent chain forms a cycle; "
                            "broken by attaching this node to the synthetic root"
                        ),
                    )
                )
                break
            position[current] = len(path)
            path.append(current)
            current = parent_of[current]
        settled.update(path)


def build_agent_graph(capsule_dir: Path) -> AgentExecutionGraph:
    """Project one captured Run Capsule into its execution DAG (read-only).

    Raises :class:`CapsuleNotFoundError` when ``capsule_dir`` is not a capsule
    directory; anything readable is reconstructed best-effort, with gaps
    surfaced as ``reconstruction_notes`` rather than errors.
    """
    capsule_dir = Path(capsule_dir)
    if not capsule_dir.is_dir():
        raise CapsuleNotFoundError(f"not a directory: {capsule_dir}")
    markers = (*_MANIFEST_NAMES, _MODEL_CALLS, _TOOL_CALLS, _TRACE)
    if not any((capsule_dir / name).is_file() for name in markers):
        raise CapsuleNotFoundError(
            f"not a Run Capsule directory (no manifest or record files): {capsule_dir}"
        )

    nodes: dict[str, GraphNode] = {}
    raw_parent: dict[str, str | None] = {}
    agent_link: dict[str, str | None] = {}
    file_order: dict[str, int] = {}
    order = 0

    # Rule 1 — emit nodes (model calls, then tool calls, then remaining spans).
    for lineno, record in _read_jsonl(capsule_dir / _MODEL_CALLS):
        node_id = _opt_str(record.get("model_call_id"))
        if node_id is None or node_id == ROOT_NODE_ID or node_id in nodes:
            continue
        nodes[node_id] = GraphNode(
            id=node_id,
            kind="model_call",
            label=_opt_str(record.get("gen_ai.request.model")) or "model_call",
            started_at=_opt_str(record.get("started_at")),
            duration_ms=_duration_ms(record.get("duration_ms")),
            status=_status(record.get("status")),
            source_ref=f"{_MODEL_CALLS}#L{lineno}",
        )
        raw_parent[node_id] = _opt_str(record.get("parent_span_id"))
        file_order[node_id] = order
        order += 1

    for lineno, record in _read_jsonl(capsule_dir / _TOOL_CALLS):
        node_id = _opt_str(record.get("tool_call_id"))
        if node_id is None or node_id == ROOT_NODE_ID or node_id in nodes:
            continue
        nodes[node_id] = GraphNode(
            id=node_id,
            kind="tool_call",
            label=_opt_str(record.get("tool_name")) or "tool_call",
            started_at=_opt_str(record.get("started_at")),
            duration_ms=_duration_ms(record.get("duration_ms")),
            status=_status(record.get("status")),
            mutation_class=_opt_str(record.get("mutation_class")),
            source_ref=f"{_TOOL_CALLS}#L{lineno}",
        )
        raw_parent[node_id] = _opt_str(record.get("parent_span_id"))
        agent_link[node_id] = _opt_str(record.get("agent_call_id"))
        file_order[node_id] = order
        order += 1

    for lineno, record in _read_jsonl(capsule_dir / _TRACE):
        node_id = _opt_str(record.get("span_id"))
        if node_id is None or node_id == ROOT_NODE_ID or node_id in nodes:
            continue
        nodes[node_id] = GraphNode(
            id=node_id,
            kind="span",
            label=_opt_str(record.get("name")) or "span",
            started_at=_opt_str(record.get("started_at")),
            duration_ms=_duration_ms(record.get("duration_ms")),
            status=_status(record.get("status")),
            source_ref=f"{_TRACE}#L{lineno}",
        )
        raw_parent[node_id] = _opt_str(record.get("parent_span_id"))
        file_order[node_id] = order
        order += 1

    notes: list[ReconstructionNote] = []
    parent_of: dict[str, str] = {}

    # Rule 2 — span_parent edges. A parent must be a *span* node in this
    # capsule (OTel semantics; also rules out cross-type cycles). Absent,
    # self-referential, or non-span parents attach to the synthetic root with
    # an explicit note — never a heuristic reparenting.
    for node_id in nodes:
        parent = raw_parent.get(node_id)
        if parent is None:
            continue
        target = nodes.get(parent)
        if parent != node_id and target is not None and target.kind == "span":
            parent_of[node_id] = parent
            continue
        parent_of[node_id] = ROOT_NODE_ID
        if parent == node_id:
            detail = (
                "parent_span_id refers to the node itself; "
                "attached to the synthetic root"
            )
        elif target is None:
            detail = (
                f"parent span {parent!r} not found in capsule; "
                "attached to the synthetic root"
            )
        else:
            detail = (
                f"parent_span_id {parent!r} refers to a non-span record; "
                "attached to the synthetic root"
            )
        notes.append(
            ReconstructionNote(kind="missing_parent", node_id=node_id, detail=detail)
        )

    _break_cycles(parent_of, notes)

    # A model call with no parent link at all is a capture gap.
    for node_id, node in nodes.items():
        if node.kind == "model_call" and raw_parent.get(node_id) is None:
            parent_of[node_id] = ROOT_NODE_ID
            notes.append(
                ReconstructionNote(
                    kind="missing_parent",
                    node_id=node_id,
                    detail=(
                        "model call carries no parent_span_id; "
                        "attached to the synthetic root"
                    ),
                )
            )

    # Rule 3 — agent_invokes_tool edges; dangling/absent links are orphans.
    invoke_edges: list[GraphEdge] = []
    for node_id, node in nodes.items():
        if node.kind != "tool_call":
            continue
        agent_id = agent_link.get(node_id)
        if agent_id is not None:
            model_node = nodes.get(agent_id)
            if model_node is not None and model_node.kind == "model_call":
                invoke_edges.append(
                    make_edge("agent_invokes_tool", agent_id, node_id)
                )
                continue
            detail = (
                f"agent_call_id {agent_id!r} matches no model call in capsule"
            )
            if node_id not in parent_of and raw_parent.get(node_id) is None:
                parent_of[node_id] = ROOT_NODE_ID
                detail += "; attached to the synthetic root"
            notes.append(
                ReconstructionNote(
                    kind="orphan_tool_call", node_id=node_id, detail=detail
                )
            )
        elif node_id not in parent_of:
            parent_of[node_id] = ROOT_NODE_ID
            notes.append(
                ReconstructionNote(
                    kind="orphan_tool_call",
                    node_id=node_id,
                    detail=(
                        "agent_call_id null and no parent span in capsule; "
                        "attached to the synthetic root"
                    ),
                )
            )

    if any(parent == ROOT_NODE_ID for parent in parent_of.values()):
        nodes[ROOT_NODE_ID] = GraphNode(
            id=ROOT_NODE_ID,
            kind="root",
            label="root",
            started_at=None,
            duration_ms=None,
        )

    edges: list[GraphEdge] = [
        make_edge("span_parent", child, parent)
        for child, parent in parent_of.items()
    ]
    edges.extend(invoke_edges)

    # Rule 4 — follows edges among same-parent, same-kind siblings, ordered by
    # started_at ascending; ties break by source-file order (deterministic).
    groups: dict[tuple[str | None, str], list[str]] = {}
    for node_id, node in nodes.items():
        if node.kind == "root":
            continue
        groups.setdefault((parent_of.get(node_id), node.kind), []).append(node_id)

    def _sibling_key(node_id: str) -> tuple[int, float, int]:
        ts = _parse_ts(nodes[node_id].started_at)
        if ts is None:
            return (1, 0.0, file_order[node_id])
        return (0, ts.timestamp(), file_order[node_id])

    for group_key in sorted(groups, key=lambda k: (k[0] or "", k[1])):
        siblings = sorted(groups[group_key], key=_sibling_key)
        edges.extend(
            make_edge("follows", a, b)
            for a, b in zip(siblings, siblings[1:])
        )

    stats = GraphStats(
        node_count=len(nodes),
        edge_count=len(edges),
        max_depth=_max_depth(set(nodes), parent_of),
        max_fan_out=_max_fan_out(parent_of),
    )
    return AgentExecutionGraph.assemble(
        capsule_id=_capsule_id(capsule_dir),
        nodes=list(nodes.values()),
        edges=edges,
        notes=notes,
        stats=stats,
    )


def _max_depth(node_ids: set[str], parent_of: dict[str, str]) -> int:
    """Node count of the longest ``span_parent`` chain (0 for an empty graph)."""
    depth_of: dict[str, int] = {}

    def depth(node_id: str) -> int:
        cached = depth_of.get(node_id)
        if cached is not None:
            return cached
        parent = parent_of.get(node_id)
        value = 1 if parent is None else 1 + depth(parent)
        depth_of[node_id] = value
        return value

    return max((depth(nid) for nid in node_ids), default=0)


def _max_fan_out(parent_of: dict[str, str]) -> int:
    """Largest number of direct ``span_parent`` children under one node."""
    children: dict[str, int] = {}
    for parent in parent_of.values():
        children[parent] = children.get(parent, 0) + 1
    return max(children.values(), default=0)
