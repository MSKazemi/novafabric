# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Memory lineage edges — ADR-0143 P1 (NF-111, NF-114).

Turns ``MemoryOperationEvent`` records into typed lineage edges so that a
poisoned memory item can be back-traced to the run that wrote it:

    run  --wrote_memory-->  memory item      (on write/update)
    memory item  --read_memory-->  run       (on read)

**No content capture.** A memory item is identified by its key only; the
value never enters the graph, even when the event carries an opt-in ``value``
(ADR-0021 §4). Callers who need the value have the capsule.

Deletes emit no edge. A delete removes an item; it neither produces nor
consumes a value, so there is no provenance fact to record — and emitting a
``wrote_memory`` for one would make a deletion indistinguishable from a write
in exactly the back-trace this module exists to serve.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from novafabric.capture.events import MemoryOperationEvent
from novafabric.lineage._types import LineageEdge, node_id_for

# ── Node identity ─────────────────────────────────────────────────────────

MEMORY_NODE_KIND = "memory"

#: Operations that create or replace a value, and therefore emit a
#: ``wrote_memory`` edge. ``delete`` is deliberately absent (see module docs).
_WRITE_OPERATIONS = frozenset({"write", "update"})


def node_ref_for_memory(memory_key: str, *, namespace: str | None = None) -> str:
    """Return the stable node ref for a memory item.

    ``namespace`` scopes the key so that two agents using the same key (say,
    ``"user_prefs"``) do not collapse into one node and cross-contaminate each
    other's back-trace. Unnamespaced keys stay bare so that existing graphs
    keep their node ids.
    """
    return memory_key if namespace is None else f"{namespace}:{memory_key}"


def memory_node(memory_key: str, *, namespace: str | None = None) -> dict[str, str]:
    """Return the graph node descriptor for a memory item."""
    ref = node_ref_for_memory(memory_key, namespace=namespace)
    return {
        "kind": MEMORY_NODE_KIND,
        "memory_key": memory_key,
        "node_id": node_id_for(MEMORY_NODE_KIND, ref),
    }


def _run_node(run_id: str) -> dict[str, str]:
    return {"kind": "run", "run_id": run_id}


# ── Edge construction ─────────────────────────────────────────────────────


def edges_for_event(
    event: MemoryOperationEvent,
    *,
    namespace: str | None = None,
) -> list[LineageEdge]:
    """Build the lineage edges implied by a single memory operation.

    Returns an empty list for operations that carry no provenance fact
    (``delete``), so callers can flat-map without special-casing.
    """
    item = memory_node(event.memory_key, namespace=namespace)
    facets: dict[str, object] = {"operation": event.operation}
    if event.relevance_score is not None:
        facets["relevance_score"] = event.relevance_score
    if event.freshness_seconds is not None:
        facets["freshness_seconds"] = event.freshness_seconds
    if event.agent_id is not None:
        facets["agent_id"] = event.agent_id

    if event.operation in _WRITE_OPERATIONS:
        return [
            LineageEdge(
                edge_type="wrote_memory",
                source=_run_node(event.run_id),
                target=item,
                confidence="observed",
                capsule_run_id=event.capsule_id,
                created_at=event.timestamp_utc,
                facets=facets,
            )
        ]

    if event.operation == "read":
        # `origin_run_id` is what the reader believed it was reading. Recording
        # it as a facet rather than as the edge source keeps the graph honest:
        # the read edge asserts only that THIS run read the item. Whether the
        # claimed origin actually wrote it is answered by walking the
        # wrote_memory edges, not by trusting the reader's own claim.
        if event.origin_run_id is not None:
            facets["claimed_origin_run_id"] = event.origin_run_id
        if event.origin_timestamp_utc is not None:
            facets["claimed_origin_timestamp_utc"] = event.origin_timestamp_utc
        return [
            LineageEdge(
                edge_type="read_memory",
                source=item,
                target=_run_node(event.run_id),
                confidence="observed",
                capsule_run_id=event.capsule_id,
                created_at=event.timestamp_utc,
                facets=facets,
            )
        ]

    return []


def edges_for_events(
    events: Iterable[MemoryOperationEvent],
    *,
    namespace: str | None = None,
) -> list[LineageEdge]:
    """Build lineage edges for a sequence of memory operations."""
    out: list[LineageEdge] = []
    for event in events:
        out.extend(edges_for_event(event, namespace=namespace))
    return out


# ── Back-trace ────────────────────────────────────────────────────────────


def writers_of(
    edges: Sequence[LineageEdge],
    memory_key: str,
    *,
    namespace: str | None = None,
) -> list[str]:
    """Return run ids that wrote ``memory_key``, oldest first.

    This is the poisoned-read back-trace (NF-114): given a memory item that
    produced a bad answer, which runs put a value there. Order is by the
    event timestamp carried on the edge, so the most recent writer — the one
    a reader most likely saw — is last.
    """
    node_id = node_id_for(
        MEMORY_NODE_KIND, node_ref_for_memory(memory_key, namespace=namespace)
    )
    matches = [
        e
        for e in edges
        if e.edge_type == "wrote_memory" and e.target.get("node_id") == node_id
    ]
    matches.sort(key=lambda e: e.created_at)
    return [e.source["run_id"] for e in matches]


def readers_of(
    edges: Sequence[LineageEdge],
    memory_key: str,
    *,
    namespace: str | None = None,
) -> list[str]:
    """Return run ids that read ``memory_key``, oldest first.

    The blast radius of a poisoned item: every run that consumed it.
    """
    node_id = node_id_for(
        MEMORY_NODE_KIND, node_ref_for_memory(memory_key, namespace=namespace)
    )
    matches = [
        e
        for e in edges
        if e.edge_type == "read_memory" and e.source.get("node_id") == node_id
    ]
    matches.sort(key=lambda e: e.created_at)
    return [e.target["run_id"] for e in matches]
