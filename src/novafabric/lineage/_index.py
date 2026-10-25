"""Hot in-memory lineage impact index (ADR-0083, gap-013).

An **optional, derived, rebuildable** in-memory adjacency index layered over the
durable :class:`~novafabric.lineage._store.LineageStore`. It serves interactive
``blast_radius`` / impact queries from RAM (adjacency-list BFS) instead of
re-running a recursive CTE on every request.

Design constraints (see ADR-0083):

* The durable SQLite store remains the single source of truth — this index is a
  cache that can be dropped and rebuilt at any time.
* :meth:`HotLineageIndex.query_blast_radius` returns the **same node set** as
  ``LineageStore.blast_radius`` (equivalence invariant I-2).
* The index is **bounded** (``max_nodes``) with LRU eviction. Evicting a node drops
  the edges it carried, so a node that lost an upstream edge can no longer answer
  completely: it is marked incomplete, and a query whose walk reaches an incomplete
  (or absent) node returns an empty result, the cache-miss signal for the caller to
  fall back to the durable store. :meth:`HotLineageIndex.is_complete_for` says which
  case an empty result is. The cache never returns a truncated answer as if whole.

Stdlib only (``collections.OrderedDict``) — Tier-A per ADR-0024.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novafabric.lineage._store import LineageStore

# Sentinel matching LineageStore's default recursion depth.
_DEFAULT_DEPTH = 5


@dataclass
class _NodeRecord:
    """Cached node payload used to materialize query result rows."""

    node_id: str
    kind: str
    ref: str
    payload: str  # JSON string, mirroring the durable row shape


@dataclass
class HotLineageIndex:
    """Bounded, rebuildable in-memory adjacency index over lineage edges.

    Adjacency is stored target -> [(source, edge_type)] so that ``blast_radius``
    (walk backward from a node to its upstream sources) is a forward BFS over this
    reverse adjacency, matching the store's recursive CTE.
    """

    max_nodes: int = 100_000
    # target_node_id -> list of (source_node_id, edge_type)
    _reverse: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # LRU ordering of node ids; most-recently-touched at the end.
    _lru: "OrderedDict[str, None]" = field(default_factory=OrderedDict)
    # node_id -> record; and ref/(kind,ref) lookup maps.
    _nodes: dict[str, _NodeRecord] = field(default_factory=dict)
    _by_ref: dict[str, str] = field(default_factory=dict)
    _by_kind_ref: dict[tuple[str, str], str] = field(default_factory=dict)
    _edge_count: int = 0
    # source_node_id -> targets it has edges to (makes eviction O(degree), not O(E)).
    _out: dict[str, set[str]] = field(default_factory=dict)
    # nodes that lost at least one upstream edge to an eviction (cleared on rebuild).
    _incomplete: set[str] = field(default_factory=set)

    # --- introspection ---------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return self._edge_count

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    # --- mutation --------------------------------------------------------

    def _touch(self, node_id: str) -> None:
        """Mark *node_id* most-recently-used, evicting the LRU node if over bound."""
        if node_id in self._lru:
            self._lru.move_to_end(node_id)
        else:
            self._lru[node_id] = None
        while len(self._lru) > self.max_nodes:
            evicted, _ = self._lru.popitem(last=False)
            self._evict(evicted)

    def _evict(self, node_id: str) -> None:
        rec = self._nodes.pop(node_id, None)
        if rec is not None:
            self._by_ref.pop(rec.ref, None)
            self._by_kind_ref.pop((rec.kind, rec.ref), None)
        # The evicted node's own upstream edges go with it.
        removed = self._reverse.pop(node_id, [])
        self._edge_count -= len(removed)
        for _src, _etype in removed:
            targets = self._out.get(_src)
            if targets is not None:
                targets.discard(node_id)
        self._incomplete.discard(node_id)
        # Edges FROM the evicted node leave their targets with a missing source:
        # those targets keep their other edges but can no longer answer completely.
        for tgt in self._out.pop(node_id, set()):
            sources = self._reverse.get(tgt)
            if sources is None:
                continue
            kept = [(s, t) for (s, t) in sources if s != node_id]
            if len(kept) != len(sources):
                self._edge_count -= len(sources) - len(kept)
                self._reverse[tgt] = kept
                if tgt in self._nodes:
                    self._incomplete.add(tgt)

    def _register_node(
        self, node_id: str, kind: str, ref: str, payload: str
    ) -> None:
        if node_id not in self._nodes:
            self._nodes[node_id] = _NodeRecord(node_id, kind, ref, payload)
            self._by_ref[ref] = node_id
            self._by_kind_ref[(kind, ref)] = node_id
        self._touch(node_id)

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        *,
        source: _NodeRecord | None = None,
        target: _NodeRecord | None = None,
    ) -> None:
        """Insert one edge (and its endpoint nodes) into the index.

        ``source``/``target`` records carry kind/ref/payload when known; when
        omitted (e.g. raw test usage) the node id doubles as its own ref.
        """
        if source is not None:
            self._register_node(
                source_id, source.kind, source.ref, source.payload)
        else:
            self._register_node(source_id, "", source_id, "{}")
        if target is not None:
            self._register_node(
                target_id, target.kind, target.ref, target.payload)
        else:
            self._register_node(target_id, "", target_id, "{}")
        self._reverse.setdefault(target_id, []).append((source_id, edge_type))
        self._out.setdefault(source_id, set()).add(target_id)
        self._edge_count += 1

    # --- rebuild ---------------------------------------------------------

    def rebuild_from_store(self, store: "LineageStore") -> None:
        """Drop all cached state and reload nodes + edges from the durable store.

        Reads directly from the store's connection (read-only); the durable schema
        is untouched. After this call the index answers blast-radius queries
        equivalently to the store, bounded by ``max_nodes``.
        """
        self._reverse.clear()
        self._lru.clear()
        self._nodes.clear()
        self._by_ref.clear()
        self._by_kind_ref.clear()
        self._out.clear()
        self._incomplete.clear()
        self._edge_count = 0

        conn = store._conn  # read-only access to the durable connection
        node_rows = conn.execute(
            "SELECT node_id, kind, ref, payload FROM lineage_nodes"
        ).fetchall()
        records: dict[str, _NodeRecord] = {}
        for row in node_rows:
            d = dict(row)
            records[d["node_id"]] = _NodeRecord(
                d["node_id"], d["kind"], d["ref"], d["payload"])

        edge_rows = conn.execute(
            "SELECT source_id, target_id, edge_type FROM lineage_edges"
        ).fetchall()
        for row in edge_rows:
            d = dict(row)
            self.add_edge(
                d["source_id"],
                d["target_id"],
                d["edge_type"],
                source=records.get(d["source_id"]),
                target=records.get(d["target_id"]),
            )

    # --- query -----------------------------------------------------------

    def _resolve_start(self, ref: str, kind: str | None) -> str | None:
        if kind is not None:
            return self._by_kind_ref.get((kind, ref))
        return self._by_ref.get(ref)

    def is_complete_for(self, node_id: str, depth: int = _DEFAULT_DEPTH) -> bool:
        """Whether the index holds every upstream edge a ``depth`` walk from *node_id* needs.

        False when *node_id* is absent, or the walk reaches a node that lost an
        upstream edge to an eviction; an empty query result then means "ask the
        durable store", not "no upstream".
        """
        return self._walk(node_id, depth, None) is not None

    def _walk(self, start: str, depth: int, edge_type: str | None) -> set[str] | None:
        """Backward BFS from *start*; ``None`` if it touches an absent/incomplete node."""
        if start not in self._nodes:
            return None
        reached: set[str] = set()
        frontier = [start]
        current_depth = 0
        while frontier and current_depth < depth:
            current_depth += 1
            next_frontier: list[str] = []
            for node in frontier:
                if node in self._incomplete:
                    return None
                for source_id, etype in self._reverse.get(node, []):
                    if edge_type is not None and etype != edge_type:
                        continue
                    if source_id not in reached:
                        reached.add(source_id)
                        next_frontier.append(source_id)
            frontier = next_frontier
        return reached

    def query_blast_radius(
        self,
        ref: str,
        kind: str | None = None,
        depth: int = _DEFAULT_DEPTH,
        edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return upstream nodes that could have influenced *ref*.

        Equivalent to ``LineageStore.blast_radius`` (ADR-0083 invariant I-2): a
        backward BFS over the reverse adjacency, root level = direct sources
        (depth 1), recursing while ``depth < N``. Optional ``edge_type`` filters
        both the root and recursive steps.
        """
        start = self._resolve_start(ref, kind)
        if start is None:
            return []
        return self.query_blast_radius_by_node_id(start, depth, edge_type)

    def query_blast_radius_by_node_id(
        self,
        start: str,
        depth: int = _DEFAULT_DEPTH,
        edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Blast radius keyed directly by node id (cache-miss-safe)."""
        if start not in self._nodes:
            return []  # cache miss -> caller falls back to the durable store
        self._touch(start)

        # BFS mirroring the CTE: the root is depth 1 (direct sources of `start`) and
        # it recurses while depth < N, so the deepest level reached is `depth`. A
        # walk that touches a node an eviction left incomplete is a miss, not a
        # truncated answer (review replay-lineage-registry-16).
        reached = self._walk(start, depth, edge_type)
        if reached is None:
            return []

        rows: list[dict[str, Any]] = []
        # Sorted: a set's iteration order varies with the hash seed, and the touch order
        # decides which node the next eviction takes, so the cache must not depend on it.
        for node_id in sorted(reached):
            rec = self._nodes.get(node_id)
            if rec is None:
                continue
            self._touch(node_id)
            rows.append({
                "node_id": rec.node_id,
                "kind": rec.kind,
                "ref": rec.ref,
                "payload": rec.payload,
            })
        return rows


def build_hot_index(store: "LineageStore", max_nodes: int = 100_000) -> HotLineageIndex:
    """Construct and populate a :class:`HotLineageIndex` from *store*."""
    index = HotLineageIndex(max_nodes=max_nodes)
    index.rebuild_from_store(store)
    return index
