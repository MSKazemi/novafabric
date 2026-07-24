"""Node-level root-cause ranking over the lineage graph (ADR-0213).

Given a failed run, walk its upstream provenance and rank suspect nodes with
bounded, additive signals: hard error evidence, recency decay, edge
confidence, and failure correlation across sibling failed runs. Builds on
ADR-0084 (which localises the step *inside* one run); never fabricates a
culprit — no error signal anywhere means ``responsible is None``.

Read-only. Scores are relative ranking weights, not calibrated probabilities.
Deterministic: no wall clock (recency is relative to the newest candidate
timestamp), fixed tie-breaks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Deliberate reuse of ADR-0084's error heuristics so the two attribution
# surfaces classify failures identically.
from novafabric.diagnose.attribution import (
    AgentErrorTaxonomy,
    CapsuleNotFoundError,
    _classify,
    _error_text,
    _has_error,
    attribute_failure,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novafabric.lineage._store import LineageStore

_RANKING_NOTE = (
    "Scores are relative ranking weights, not calibrated probabilities "
    "(ADR-0213)."
)
_NO_SIGNAL_NOTE = (
    "No upstream node carries an error signal; refusing to fabricate a "
    "culprit (ADR-0213 I-1)."
)

_ERROR_WEIGHT = 1.0
_RECENCY_WEIGHT = 0.5
_CORRELATION_WEIGHT = 0.25
_CORRELATION_CAP = 4
_INFERRED_CONFIDENCE_MULTIPLIER = 0.8

_TIMESTAMP_KEYS = ("finished_at", "started_at", "created_at")


class UnknownLineageRunError(KeyError):
    """Raised when the requested run id has no node in the lineage graph."""


@dataclass
class SuspectNode:
    node_id: str
    kind: str
    ref: str
    score: float
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "ref": self.ref,
            "score": round(self.score, 4),
            "signals": self.signals,
        }


@dataclass
class RootCauseReport:
    run_ref: str
    suspects: list[SuspectNode]
    responsible: SuspectNode | None
    taxonomy: AgentErrorTaxonomy
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_ref": self.run_ref,
            "taxonomy": self.taxonomy.value,
            "responsible": self.responsible.as_dict() if self.responsible else None,
            "suspects": [s.as_dict() for s in self.suspects],
            "notes": self.notes,
        }


def _parse_ts(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _node_timestamp(payload: dict[str, Any], edge_times: list[str]) -> datetime | None:
    for key in _TIMESTAMP_KEYS:
        ts = _parse_ts(payload.get(key))
        if ts is not None:
            return ts
    edge_ts = [t for t in (_parse_ts(e) for e in edge_times) if t is not None]
    return max(edge_ts) if edge_ts else None


def _failed_run_refs(store: LineageStore) -> list[str]:
    """Refs of every run node whose payload carries an error signal."""
    return [
        n["ref"]
        for n in store.all_nodes()
        if n["kind"] == "run" and _has_error(n["payload"])
    ]


def rank_root_causes(
    store: LineageStore,
    run_id: str,
    *,
    depth: int = 5,
    capsule_dir: Path | None = None,
    half_life_hours: float = 24.0,
) -> RootCauseReport:
    """Rank upstream suspect nodes for a failed run (ADR-0213).

    Raises :class:`UnknownLineageRunError` when *run_id* has no lineage node.
    """
    start = store._node_id_for_ref(run_id, "run")
    if start is None:
        raise UnknownLineageRunError(run_id)

    candidates = store.provenance(run_id, kind="run", depth=depth)
    notes = [_RANKING_NOTE]
    if not candidates:
        return RootCauseReport(
            run_ref=run_id,
            suspects=[],
            responsible=None,
            taxonomy=AgentErrorTaxonomy.UNKNOWN,
            notes=[*notes, "Run has no upstream provenance at this depth."],
        )

    # Parse candidate payloads (rooted traversals return payload as JSON text).
    parsed: dict[str, dict[str, Any]] = {}
    for row in candidates:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        parsed[row["node_id"]] = {**row, "payload": payload}

    # Subgraph edges: weakest-confidence and latest-timestamp per candidate.
    node_ids = [*parsed.keys(), start]
    conf_by_node: dict[str, str] = {}
    edge_times_by_node: dict[str, list[str]] = {nid: [] for nid in parsed}
    for edge in store.edges_for_nodes(node_ids):
        confidence = str(edge.get("confidence") or "")
        created_at = str(edge.get("created_at") or "")
        for endpoint in (edge.get("source", {}), edge.get("target", {})):
            nid = store._resolve_node_id(endpoint, [])
            if nid not in parsed:
                continue
            if confidence == "inferred":
                conf_by_node[nid] = "inferred"
            if created_at:
                edge_times_by_node[nid].append(created_at)

    # Failure correlation: how many *other* failed runs share this ancestor?
    correlation: dict[str, int] = dict.fromkeys(parsed, 0)
    for ref in _failed_run_refs(store):
        if ref == run_id:
            continue
        for row in store.provenance(ref, kind="run", depth=depth):
            nid = row["node_id"]
            if nid in correlation:
                correlation[nid] += 1

    # Newest candidate timestamp anchors recency (no wall clock — determinism).
    timestamps = {
        nid: _node_timestamp(c["payload"], edge_times_by_node.get(nid, []))
        for nid, c in parsed.items()
    }
    known = [t for t in timestamps.values() if t is not None]
    newest = max(known) if known else None

    suspects: list[SuspectNode] = []
    any_error = False
    for nid, cand in parsed.items():
        payload = cand["payload"]
        signals: list[str] = []
        score = 0.0

        has_error = _has_error(payload)
        if has_error:
            any_error = True
            score += _ERROR_WEIGHT
            signals.append(
                f"error signal: {_error_text(payload) or payload.get('status', 'failed')}"
            )

        ts = timestamps[nid]
        if newest is not None and ts is not None:
            age_hours = (newest - ts).total_seconds() / 3600.0
            decay = 0.5 ** (age_hours / half_life_hours) if half_life_hours > 0 else 1.0
            score += _RECENCY_WEIGHT * decay
            signals.append(f"recency: {decay:.2f} (as of newest candidate activity)")

        corr = min(correlation[nid], _CORRELATION_CAP)
        if corr:
            score += _CORRELATION_WEIGHT * corr
            signals.append(
                f"appears in provenance of {corr} other failed run(s)"
            )

        if conf_by_node.get(nid) == "inferred":
            score *= _INFERRED_CONFIDENCE_MULTIPLIER
            signals.append(
                f"weakest incident edge is inferred (x{_INFERRED_CONFIDENCE_MULTIPLIER})"
            )

        suspects.append(
            SuspectNode(
                node_id=nid,
                kind=str(cand["kind"]),
                ref=str(cand["ref"]),
                score=score,
                signals=signals,
            )
        )

    # Deterministic order: score desc, earliest activity first, node_id.
    def _sort_key(s: SuspectNode) -> tuple[float, str, str]:
        ts = timestamps.get(s.node_id)
        return (-s.score, ts.isoformat() if ts else "~", s.node_id)

    suspects.sort(key=_sort_key)

    if not any_error:
        return RootCauseReport(
            run_ref=run_id,
            suspects=suspects,
            responsible=None,
            taxonomy=AgentErrorTaxonomy.UNKNOWN,
            notes=[*notes, _NO_SIGNAL_NOTE],
        )

    responsible = next(s for s in suspects if "error signal" in " ".join(s.signals))
    taxonomy = _taxonomy_for(responsible, parsed[responsible.node_id]["payload"],
                             capsule_dir, store, notes)
    return RootCauseReport(
        run_ref=run_id,
        suspects=suspects,
        responsible=responsible,
        taxonomy=taxonomy,
        notes=notes,
    )


def _taxonomy_for(
    suspect: SuspectNode,
    payload: dict[str, Any],
    capsule_dir: Path | None,
    store: LineageStore,
    notes: list[str],
) -> AgentErrorTaxonomy:
    """Step-grade taxonomy via ADR-0084 when the capsule resolves, else cues."""
    if suspect.kind == "run" and capsule_dir is not None:
        candidate_dir = capsule_dir / suspect.ref
        try:
            attribution = attribute_failure(candidate_dir, lineage_store=store)
        except CapsuleNotFoundError:
            notes.append(
                f"No capsule found for {suspect.ref} under {capsule_dir}; "
                "taxonomy derived from lineage payload cues only."
            )
        else:
            notes.append(f"Step-level attribution via ADR-0084 for {suspect.ref}.")
            return attribution.taxonomy
    return _classify(_error_text(payload), suspect.ref)
