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
"""No-LLM causal-graph back-trace attribution (ADR-0101 §NF-019/§NF-022).

Reconstructs the span parent/child **causal graph** from a capsule's recorded spans
(``parent_span_id`` → ``span_id``) and back-traces the *root* failure nodes: a failing
node whose entire ancestor chain is error-free is a causal root; a failing node that is
downstream of another failure is not (its failure is explained by the earlier one).
Deterministic and fast — no LLM inference at debug time (AgentTrace, EVIDENCE src-413).

This **complements** the ordinal, run-level ``attribute_failure`` (ADR-0084) with the
graph-structural view NF-019 calls for; it does not replace it. It reuses that module's
step loading, error detection, and taxonomy classifier.

**NF-022 verification gate.** This layer runs *no* intervention replay, so every candidate
it produces is honestly marked ``verification = "unverified"`` — a ranked candidate, never
a proven root cause. Confirming a candidate (``CONFIRMED``/``REFUTED``) is the job of the
replay-backed :func:`~novafabric.diagnose.verify.verify_hypothesis` (NF-017/018), which is
gated on the ADR-0086 intervention engine and deferred here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from novafabric.diagnose.attribution import (
    AgentErrorTaxonomy,
    CapsuleNotFoundError,
    Step,
    _classify,
    _load_steps,
)

#: Marker for a hypothesis with no replay confirmation (NF-022).
UNVERIFIED = "unverified"

_MAX_ANCESTOR_WALK = 10_000  # bounded walk — guards a malformed cyclic parent chain


@dataclass
class CausalRootCandidate:
    """A failing step attributed as a causal root by graph back-trace."""

    step_id: str
    name: str
    kind: str
    depth: int
    taxonomy: AgentErrorTaxonomy
    rank_score: float
    verification: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "kind": self.kind,
            "depth": self.depth,
            "taxonomy": self.taxonomy.value,
            "rank_score": round(self.rank_score, 4),
            "verification": self.verification,
            "rationale": self.rationale,
        }


@dataclass
class CausalAttribution:
    """Result of a causal-graph back-trace over a failed run."""

    run_id: str
    status: str
    root_candidates: list[CausalRootCandidate] = field(default_factory=list)
    n_error_nodes: int = 0
    note: str = (
        "Causal-graph back-trace over recorded spans (NF-019); candidates are "
        "unverified — no intervention replay was run (NF-022)."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "n_error_nodes": self.n_error_nodes,
            "root_candidates": [c.as_dict() for c in self.root_candidates],
            "note": self.note,
        }


def _parent_id(step: Step) -> str | None:
    raw = step.raw
    for key in ("parent_span_id", "parent_id", "parentSpanId"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _depth(step_id: str, parents: dict[str, str | None], present: set[str]) -> int:
    """Graph depth of a node (root = 0), with a bounded walk against cycles."""
    depth = 0
    cur = parents.get(step_id)
    seen: set[str] = {step_id}
    while cur is not None and cur in present and cur not in seen and depth < _MAX_ANCESTOR_WALK:
        seen.add(cur)
        depth += 1
        cur = parents.get(cur)
    return depth


def _has_failing_ancestor(
    step_id: str,
    parents: dict[str, str | None],
    erroring: set[str],
    present: set[str],
) -> bool:
    """True if any ancestor of *step_id* is itself an error node (bounded walk)."""
    cur = parents.get(step_id)
    seen: set[str] = {step_id}
    hops = 0
    while cur is not None and cur not in seen and hops < _MAX_ANCESTOR_WALK:
        if cur in erroring:
            return True
        if cur not in present:
            break
        seen.add(cur)
        cur = parents.get(cur)
        hops += 1
    return False


def causal_root_candidates(capsule_dir: Path | str) -> CausalAttribution:
    """Back-trace the causal graph of a failed run to its root failure nodes.

    Pure and read-only. Reconstructs the span parent/child graph, marks the failing
    nodes whose ancestors are all error-free as causal roots, and ranks them (shallower
    graph depth first, then earliest rollout ordinal). Every candidate is ``unverified``
    (NF-022). Raises :class:`CapsuleNotFoundError` if there is no ``capsule.yaml``.
    """
    capsule_dir = Path(capsule_dir)
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise CapsuleNotFoundError(f"No capsule.yaml under {capsule_dir}")
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    run_id = str(manifest.get("run_id", capsule_dir.name))
    status = str(manifest.get("status", "unknown"))

    steps = _load_steps(capsule_dir, manifest)
    by_id: dict[str, Step] = {s.step_id: s for s in steps}
    present = set(by_id)
    parents: dict[str, str | None] = {s.step_id: _parent_id(s) for s in steps}
    erroring = {s.step_id for s in steps if s.has_error}

    candidates: list[CausalRootCandidate] = []
    for step in steps:
        if not step.has_error:
            continue
        if _has_failing_ancestor(step.step_id, parents, erroring, present):
            continue  # downstream of an earlier failure — not a causal root
        depth = _depth(step.step_id, parents, present)
        taxonomy = _classify(step.error_text, step.name)
        # Rank: shallower (more upstream) first, then earliest rollout ordinal.
        rank_score = 1.0 / (1.0 + depth) + 1.0 / (2.0 + step.ordinal)
        rationale = (
            f"causal-graph root: failing {step.kind} step '{step.name}' at depth "
            f"{depth} with no failing ancestor; ranked candidate (unverified — no "
            f"replay run), not a proven root cause"
        )
        candidates.append(
            CausalRootCandidate(
                step_id=step.step_id,
                name=step.name,
                kind=step.kind,
                depth=depth,
                taxonomy=taxonomy,
                rank_score=rank_score,
                verification=UNVERIFIED,
                rationale=rationale,
            )
        )

    # Deterministic order: highest rank first, ties broken by step_id.
    candidates.sort(key=lambda c: (-c.rank_score, c.step_id))
    return CausalAttribution(
        run_id=run_id,
        status=status,
        root_candidates=candidates,
        n_error_nodes=len(erroring),
    )
