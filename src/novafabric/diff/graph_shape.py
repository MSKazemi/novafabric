"""Agent-graph shape-change pre-check for structural diff (ADR-0124 P3, diff half).

``nova diff A B --graph-shape`` reconstructs the ADR-0124 agent execution graph of
both capsules (:func:`novafabric.agent_graph.build_agent_graph`) and answers one
cheap question before the deep diff: *did the control-flow shape change?*

Two digests are reported, deliberately:

* ``graph_digest`` (per side) — the ADR-0124 content address. It binds the
  capsule id, the source record ids, and node timings, so it is equal **only**
  when both sides reconstruct the byte-identical graph (e.g. the same capsule
  twice). Two distinct runs essentially never share it, so on its own it cannot
  answer "same shape".
* ``shape_digest`` (per side) — an id-, timing- and capsule-independent digest of
  the *topology*: every node is identified by its structural position
  (tree-parent position + ``kind`` + ``label`` + ordinal among same-kind,
  same-label siblings in ``follows`` order), every edge by
  ``(type, from-position, to-position)``. Equal shape digests ⇒ same shape.

When the shapes differ, the node/edge deltas are summarised by structural path,
bounded (``limit`` items per list, with the true totals reported) and ordered
deterministically. A capsule whose graph cannot be built (not a capsule, oversize
sources, an unreadable or non-UTF-8 source file, malformed beyond best-effort
reconstruction) yields ``status: "unavailable"`` with a reason — this annotation
never crashes the diff.

Malformed source lines (ADR-0303 Amendment 2). Reconstruction is best-effort: a
line that is not JSON, or JSON that is not an object, is skipped. Each available
side counts those lines per source file in ``skipped_malformed_lines``; any
non-zero count makes the comparison incomplete (:attr:`GraphShapeDiff.is_complete`),
and ``nova diff --assert-same-shape`` then exits 2, "cannot compare", before the
shape verdict — a shape over partial records is neither certified nor reported
as a change.

Status: **experimental**. Read-only; stdlib + existing Pydantic only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.agent_graph import AgentExecutionGraph, GraphNode, build_agent_graph
from novafabric.agent_graph.builder import parse_jsonl_text

#: Version of the ``graph_shape`` block and of the shape-digest projection.
GRAPH_SHAPE_VERSION = "0.1.0"

#: Default cap on items per delta list (nodes added/removed, edges added/removed).
DEFAULT_DELTA_LIMIT = 50

#: Hard ceiling a caller may request for ``limit``.
MAX_DELTA_LIMIT = 1000

#: Combined size cap of the three graph source files per capsule (64 MiB).
MAX_SOURCE_BYTES = 64 * 1024 * 1024

#: Node-count cap per reconstructed graph; larger graphs are reported unavailable.
MAX_GRAPH_NODES = 100_000

#: Display caps for one structural path (segments) and one label (characters).
_MAX_PATH_SEGMENTS = 12
_MAX_LABEL_CHARS = 80

_SOURCE_FILES = ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl")

#: ``skipped_malformed_lines`` key -> the graph source file it counts lines of.
SOURCE_FILE_KEYS = {
    "model_calls": "model-calls.jsonl",
    "tool_calls": "tool-calls.jsonl",
    "trace": "trace.jsonl",
}

ShapeStatus = Literal["same_shape", "shape_changed", "unavailable"]


class GraphShapeError(Exception):
    """A graph was reconstructed but could not be projected to a shape."""


class GraphSide(BaseModel):
    """Per-capsule summary of the reconstructed graph (or why it is unavailable)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    reason: str | None = None
    capsule_id: str | None = None
    graph_digest: str | None = None
    shape_digest: str | None = None
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    nodes_by_kind: dict[str, int] = Field(default_factory=dict)
    reconstruction_note_count: int = Field(default=0, ge=0)
    #: Source lines reconstruction skipped (not JSON, or not a JSON object), per
    #: ``SOURCE_FILE_KEYS`` key. Every key, zeros included, on an available side;
    #: empty on an unavailable one, whose sources were not (fully) read.
    skipped_malformed_lines: dict[str, int] = Field(default_factory=dict)


class NodeDelta(BaseModel):
    """One node present on only one side, located by its structural path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: str
    label: str
    node_id: str


class EdgeDelta(BaseModel):
    """One edge present on only one side, endpoints located by structural path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    from_path: str
    to_path: str


class DeltaCounts(BaseModel):
    """True totals of each delta list (lists themselves are capped at ``limit``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes_added: int = Field(default=0, ge=0)
    nodes_removed: int = Field(default=0, ge=0)
    edges_added: int = Field(default=0, ge=0)
    edges_removed: int = Field(default=0, ge=0)


class GraphShapeDiff(BaseModel):
    """The additive ``graph_shape`` block of ``nova diff --graph-shape``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = GRAPH_SHAPE_VERSION
    status: ShapeStatus
    graph_digest_equal: bool | None = None
    a: GraphSide
    b: GraphSide
    counts: DeltaCounts = Field(default_factory=DeltaCounts)
    nodes_added: list[NodeDelta] = Field(default_factory=list)
    nodes_removed: list[NodeDelta] = Field(default_factory=list)
    edges_added: list[EdgeDelta] = Field(default_factory=list)
    edges_removed: list[EdgeDelta] = Field(default_factory=list)
    limit: int = Field(default=DEFAULT_DELTA_LIMIT, ge=0)
    truncated: bool = False

    @property
    def same_shape(self) -> bool:
        """True only when both graphs were built and their shapes match."""
        return self.status == "same_shape"

    @property
    def is_complete(self) -> bool:
        """False when either side's reconstruction skipped a malformed source line.

        ``status`` is then computed over the records that parsed only, so
        neither "same shape" nor "shape changed" is established and
        ``--assert-same-shape`` exits 2 (ADR-0303 Amendment 2).
        """
        return not any(
            count for side in (self.a, self.b) for count in side.skipped_malformed_lines.values()
        )

    def to_document(self) -> dict[str, Any]:
        """JSON-ready dict (stable key set; deterministic list order)."""
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class _Shape:
    """Internal projection of one graph onto structural positions."""

    graph: AgentExecutionGraph
    key_of: dict[str, str]
    parent_of: dict[str, str]
    segment_of: dict[str, str]
    edges: frozenset[tuple[str, str, str]]
    digest: str


def _sha256_json(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _display_label(label: str) -> str:
    """Printable, bounded label: control characters escaped, long labels elided."""
    cleaned = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in label)
    if len(cleaned) > _MAX_LABEL_CHARS:
        cleaned = cleaned[: _MAX_LABEL_CHARS - 1] + "…"
    return cleaned


def _follows_positions(graph: AgentExecutionGraph) -> dict[str, int]:
    """Position of each node in its ``follows`` chain (0 = first; bounded walk)."""
    pred: dict[str, str] = {}
    for edge in graph.edges:
        if edge.type == "follows":
            pred[edge.to] = edge.from_
    positions: dict[str, int] = {}
    limit = len(graph.nodes) + 1
    for node in graph.nodes:
        chain: list[str] = []
        current: str | None = node.id
        while current is not None and current not in positions:
            if len(chain) > limit:
                raise GraphShapeError("follows chain does not terminate")
            chain.append(current)
            current = pred.get(current)
        base = -1 if current is None else positions[current]
        for offset, node_id in enumerate(reversed(chain), start=1):
            positions[node_id] = base + offset
    return positions


def project_shape(graph: AgentExecutionGraph) -> _Shape:
    """Project a graph onto id-/timing-/capsule-independent structural positions.

    A node's key is ``sha256([parent_key, kind, label, ordinal])`` where the
    parent is its ``span_parent`` target and the ordinal ranks it among
    siblings with the same parent, kind and label by ``follows`` position (node
    id as the final tie-break). Keys are Merkle-style, so each is O(1) to
    compute and unique within one graph.
    """
    nodes: dict[str, GraphNode] = {n.id: n for n in graph.nodes}
    parent_of: dict[str, str] = {}
    for edge in graph.edges:
        if edge.type == "span_parent" and edge.from_ in nodes and edge.to in nodes:
            parent_of[edge.from_] = edge.to
    positions = _follows_positions(graph)

    children: dict[str | None, list[str]] = {}
    for node_id in nodes:
        children.setdefault(parent_of.get(node_id), []).append(node_id)

    key_of: dict[str, str] = {}
    segment_of: dict[str, str] = {}
    frontier: list[str | None] = [None]
    while frontier:
        parent = frontier.pop()
        parent_key = "" if parent is None else key_of[parent]
        ranked = sorted(
            children.get(parent, []),
            key=lambda nid: (
                nodes[nid].kind,
                nodes[nid].label.encode("utf-8"),
                positions.get(nid, 0),
                nid.encode("utf-8"),
            ),
        )
        ordinal: dict[tuple[str, str], int] = {}
        for node_id in ranked:
            node = nodes[node_id]
            group = (node.kind, node.label)
            index = ordinal.get(group, 0)
            ordinal[group] = index + 1
            key_of[node_id] = _sha256_json([parent_key, node.kind, node.label, index])
            segment_of[node_id] = f"{node.kind}:{_display_label(node.label)}[{index}]"
            frontier.append(node_id)

    if len(key_of) != len(nodes):
        raise GraphShapeError("span_parent structure is not a forest")

    edges = frozenset((e.type, key_of[e.from_], key_of[e.to]) for e in graph.edges)
    digest = "sha256:" + _sha256_json(
        {
            "version": GRAPH_SHAPE_VERSION,
            "nodes": sorted(key_of.values()),
            "edges": sorted(list(e) for e in edges),
        }
    )
    return _Shape(graph, key_of, parent_of, segment_of, edges, digest)


def _path(shape: _Shape, node_id: str) -> str:
    """Human-readable structural path, capped at the last few segments."""
    segments: list[str] = []
    current: str | None = node_id
    while current is not None and len(segments) <= _MAX_PATH_SEGMENTS:
        segments.append(shape.segment_of[current])
        current = shape.parent_of.get(current)
    elided = current is not None or len(segments) > _MAX_PATH_SEGMENTS
    segments = segments[:_MAX_PATH_SEGMENTS]
    text = "/".join(reversed(segments))
    return "…/" + text if elided else text


def _source_bytes(capsule: Path) -> int:
    total = 0
    for name in _SOURCE_FILES:
        try:
            total += (capsule / name).stat().st_size
        except OSError:
            continue
    return total


def _skipped_source_lines(capsule: Path) -> dict[str, int]:
    """Lines of each graph source file the builder skips, by its own parse rule.

    Reads with the builder's rule (:func:`parse_jsonl_text`), so the count is
    what reconstruction actually dropped. Unlike the builder, which reads an
    unreadable file as empty, this raises — the side is then unavailable rather
    than a graph silently missing that file's records.
    """
    counts: dict[str, int] = {}
    for key, name in SOURCE_FILE_KEYS.items():
        path = capsule / name
        counts[key] = (
            parse_jsonl_text(path.read_text(encoding="utf-8"))[1] if path.is_file() else 0
        )
    return counts


def _build(capsule: Path) -> tuple[_Shape | None, GraphSide]:
    """Build + project one side; any failure becomes an ``available=False`` side."""
    size = _source_bytes(capsule)
    if size > MAX_SOURCE_BYTES:
        reason = f"graph sources total {size} bytes, over the {MAX_SOURCE_BYTES}-byte cap"
        return None, GraphSide(available=False, reason=reason)
    try:
        graph = build_agent_graph(capsule)
        if len(graph.nodes) > MAX_GRAPH_NODES:
            raise GraphShapeError(f"{len(graph.nodes)} nodes, over the {MAX_GRAPH_NODES}-node cap")
        shape = project_shape(graph)
        skipped = _skipped_source_lines(capsule)
    # Fail-open by design (ADR-0124 CLI surface: "never a crash of surrounding
    # commands"): whatever goes wrong reconstructing one side — named errors,
    # malformed records, recursion on pathological depth — is reported, not raised.
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}".strip()
        return None, GraphSide(available=False, reason=reason[:500])
    by_kind: dict[str, int] = {}
    for node in graph.nodes:
        by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
    side = GraphSide(
        available=True,
        capsule_id=graph.capsule_id,
        graph_digest=graph.graph_digest,
        shape_digest=shape.digest,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
        nodes_by_kind=dict(sorted(by_kind.items())),
        reconstruction_note_count=len(graph.reconstruction_notes or []),
        skipped_malformed_lines=skipped,
    )
    return shape, side


def _node_deltas(present: _Shape, other_keys: set[str]) -> list[NodeDelta]:
    nodes = {n.id: n for n in present.graph.nodes}
    deltas = [
        NodeDelta(
            path=_path(present, node_id),
            kind=nodes[node_id].kind,
            label=_display_label(nodes[node_id].label),
            node_id=node_id,
        )
        for node_id, key in present.key_of.items()
        if key not in other_keys
    ]
    return sorted(deltas, key=lambda d: (d.path.encode("utf-8"), d.node_id.encode("utf-8")))


def _edge_deltas(present: _Shape, other_edges: frozenset[tuple[str, str, str]]) -> list[EdgeDelta]:
    id_of = {key: node_id for node_id, key in present.key_of.items()}
    deltas = {
        EdgeDelta(
            type=edge_type,
            from_path=_path(present, id_of[src]),
            to_path=_path(present, id_of[dst]),
        )
        for edge_type, src, dst in present.edges
        if (edge_type, src, dst) not in other_edges
    }
    return sorted(
        deltas,
        key=lambda d: (d.type, d.from_path.encode("utf-8"), d.to_path.encode("utf-8")),
    )


def compare_graph_shapes(
    capsule_a: Path, capsule_b: Path, *, limit: int = DEFAULT_DELTA_LIMIT
) -> GraphShapeDiff:
    """Compare the agent-graph shapes of two capsules (read-only, never raises).

    ``limit`` caps each delta list (clamped to ``[0, MAX_DELTA_LIMIT]``); the
    ``counts`` block always carries the true totals and ``truncated`` says
    whether anything was cut.
    """
    limit = max(0, min(limit, MAX_DELTA_LIMIT))
    shape_a, side_a = _build(Path(capsule_a))
    shape_b, side_b = _build(Path(capsule_b))
    if shape_a is None or shape_b is None:
        return GraphShapeDiff(status="unavailable", a=side_a, b=side_b, limit=limit)

    digest_equal = shape_a.graph.graph_digest == shape_b.graph.graph_digest
    if digest_equal or shape_a.digest == shape_b.digest:
        return GraphShapeDiff(
            status="same_shape",
            graph_digest_equal=digest_equal,
            a=side_a,
            b=side_b,
            limit=limit,
        )

    keys_a, keys_b = set(shape_a.key_of.values()), set(shape_b.key_of.values())
    added = _node_deltas(shape_b, keys_a)
    removed = _node_deltas(shape_a, keys_b)
    e_added = _edge_deltas(shape_b, shape_a.edges)
    e_removed = _edge_deltas(shape_a, shape_b.edges)
    counts = DeltaCounts(
        nodes_added=len(added),
        nodes_removed=len(removed),
        edges_added=len(e_added),
        edges_removed=len(e_removed),
    )
    return GraphShapeDiff(
        status="shape_changed",
        graph_digest_equal=False,
        a=side_a,
        b=side_b,
        counts=counts,
        nodes_added=added[:limit],
        nodes_removed=removed[:limit],
        edges_added=e_added[:limit],
        edges_removed=e_removed[:limit],
        limit=limit,
        truncated=any(len(x) > limit for x in (added, removed, e_added, e_removed)),
    )


def malformed_source_messages(diff: GraphShapeDiff) -> list[str]:
    """One sentence per graph source file with skipped lines, A side first."""
    messages: list[str] = []
    for name, side in (("A", diff.a), ("B", diff.b)):
        for key, count in side.skipped_malformed_lines.items():
            if count:
                messages.append(
                    f"skipped {count} malformed line(s) in {SOURCE_FILE_KEYS.get(key, key)} "
                    f"of run {name}: not JSON, or not a JSON object"
                )
    return messages


def _skipped_lines(name: str, side: GraphSide) -> list[str]:
    return [
        f"  {name}: skipped {count} malformed line(s) in {SOURCE_FILE_KEYS.get(key, key)}; "
        "the shape covers only the records that parsed"
        for key, count in side.skipped_malformed_lines.items()
        if count
    ]


def _side_line(name: str, side: GraphSide) -> str:
    if not side.available:
        return f"  {name}: graph unavailable — {side.reason}"
    kinds = " ".join(f"{k}={v}" for k, v in side.nodes_by_kind.items()) or "empty"
    return (
        f"  {name}: {side.node_count} nodes, {side.edge_count} edges ({kinds}); "
        f"graph_digest {side.graph_digest}"
    )


def format_graph_shape_text(diff: GraphShapeDiff) -> str:
    """Plain-text block (no Rich markup) appended to the text diff."""
    headline = {
        "same_shape": "same shape",
        "shape_changed": "shape changed",
        "unavailable": "graph unavailable",
    }[diff.status]
    lines = [f"Graph shape (ADR-0124, experimental): {headline}"]
    lines.append(_side_line("A", diff.a))
    lines.append(_side_line("B", diff.b))
    lines.extend(_skipped_lines("A", diff.a) + _skipped_lines("B", diff.b))
    if diff.status == "same_shape":
        basis = "identical graph_digest" if diff.graph_digest_equal else "equal shape_digest"
        lines.append(f"  {basis}: {diff.a.shape_digest}")
    if diff.status != "shape_changed":
        return "\n".join(lines)

    sections: list[tuple[str, str, list[str], int]] = [
        (
            "Nodes added",
            "+",
            [f"{d.kind} {d.path} (id {d.node_id})" for d in diff.nodes_added],
            diff.counts.nodes_added,
        ),
        (
            "Nodes removed",
            "-",
            [f"{d.kind} {d.path} (id {d.node_id})" for d in diff.nodes_removed],
            diff.counts.nodes_removed,
        ),
        (
            "Edges added",
            "+",
            [f"{d.type} {d.from_path} -> {d.to_path}" for d in diff.edges_added],
            diff.counts.edges_added,
        ),
        (
            "Edges removed",
            "-",
            [f"{d.type} {d.from_path} -> {d.to_path}" for d in diff.edges_removed],
            diff.counts.edges_removed,
        ),
    ]
    for title, sign, items, total in sections:
        if total == 0:
            continue
        lines.append(f"  {title} ({total}):")
        lines.extend(f"    {sign} {item}" for item in items)
        if total > len(items):
            lines.append(f"    … {total - len(items)} more not shown (limit {diff.limit})")
    return "\n".join(lines)


def format_graph_shape_annotations(diff: GraphShapeDiff) -> list[str]:
    """GitHub workflow-command lines for ``--output-format github-annotation``."""
    title = "title=NovaFabric Graph Shape"
    # A skipped line does not change the level of the verdict line; it warns
    # that the verdict covers only the records that parsed (ADR-0303 Am. 2).
    warnings = [
        f"::warning {title}::{m[:1].upper() + m[1:]}" for m in malformed_source_messages(diff)
    ]
    return _verdict_annotations(diff, title) + warnings


def _verdict_annotations(diff: GraphShapeDiff, title: str) -> list[str]:
    if diff.status == "same_shape":
        return [f"::notice {title}::Agent graph shape unchanged ({diff.a.shape_digest})"]
    if diff.status == "unavailable":
        reasons = "; ".join(
            f"{name}: {side.reason}"
            for name, side in (("A", diff.a), ("B", diff.b))
            if not side.available
        )
        return [f"::warning {title}::Agent graph unavailable ({reasons})"]
    c = diff.counts
    return [
        f"::error {title}::Agent graph shape changed: nodes +{c.nodes_added}/-{c.nodes_removed}, "
        f"edges +{c.edges_added}/-{c.edges_removed}"
    ]
