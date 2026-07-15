"""Failure attribution over the lineage / causal graph (ADR-0084).

Pure, read-only analysis that runs *over* the existing captured trace and the
existing lineage store. Given a failed Run Capsule it decomposes the run into
ordered steps (src-411 module decomposition), scores them with a small set of
bounded signals in a coarse pass, then in a fine pass picks the single most
likely responsible step and labels it with the :class:`AgentErrorTaxonomy`.

Research anchors:

* src-413 — *From Flat Logs to Causal Graphs* (hierarchical, coarse->fine
  attribution over a causal graph). NovaFabric *captures* the graph, so we read
  parent/child + ``delegated_to``/``spawned`` edges from the lineage store to
  bias a downstream step above its coordinator ancestor.
* src-411 — *Where LLM Agents Fail / AgentDebug* (the AgentErrorTaxonomy and the
  "earliest root-cause step" localisation).

This module writes nothing. Scores are relative ranking weights, **not**
calibrated probabilities. When no error signal is present the result is
``UNKNOWN`` with no fabricated culprit (honest under-determination).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novafabric.lineage._store import LineageStore


class AgentErrorTaxonomy(str, Enum):
    """src-411 operational failure categories, plus an honest ``UNKNOWN``."""

    MEMORY = "MEMORY"
    REFLECTION = "REFLECTION"
    PLANNING = "PLANNING"
    ACTION = "ACTION"
    SYSTEM = "SYSTEM"
    UNKNOWN = "UNKNOWN"


# Keyword cues used to classify a step's failure into a taxonomy category.
# Order matters only as a deterministic tie-break: the first category whose cues
# match wins. ACTION/tool errors are very common, so SYSTEM is checked first to
# catch infrastructure faults that would otherwise look like a generic action.
_TAXONOMY_CUES: list[tuple[AgentErrorTaxonomy, tuple[str, ...]]] = [
    (
        AgentErrorTaxonomy.SYSTEM,
        (
            "nonzeroexit",
            "exit code",
            "timeout",
            "timed out",
            "oom",
            "out of memory",
            "segfault",
            "killed",
            "infrastructure",
            "runtime error",
            "connectionerror",
            "connection refused",
        ),
    ),
    (
        AgentErrorTaxonomy.PLANNING,
        (
            "plan",
            "subgoal",
            "decompos",
            "sequenc",
            "goal selection",
            "wrong order",
        ),
    ),
    (
        AgentErrorTaxonomy.MEMORY,
        (
            "memory",
            "context",
            "retriev",
            "stale",
            "forgot",
            "missing context",
            "state",
        ),
    ),
    (
        AgentErrorTaxonomy.REFLECTION,
        (
            "reflect",
            "self-critique",
            "self critique",
            "self-evaluat",
            "could not recover",
            "did not detect",
            "verify",
        ),
    ),
    (
        AgentErrorTaxonomy.ACTION,
        (
            "tool",
            "action",
            "api call",
            "http",
            "request failed",
            "malformed",
            "invalid argument",
            "rejected",
        ),
    ),
]


@dataclass
class Step:
    """One decomposed unit of a rollout (a trace span / tool call / model call)."""

    step_id: str
    ordinal: int
    name: str
    kind: str  # "span" | "tool" | "model"
    started_at: str | None
    run_id: str | None
    has_error: bool
    error_text: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepAttribution:
    step_id: str
    name: str
    kind: str
    score: float
    taxonomy: AgentErrorTaxonomy
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "kind": self.kind,
            "score": round(self.score, 4),
            "taxonomy": self.taxonomy.value,
            "rationale": self.rationale,
        }


@dataclass
class RunAttribution:
    run_id: str
    status: str
    responsible: StepAttribution | None
    taxonomy: AgentErrorTaxonomy
    ranked: list[StepAttribution]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "taxonomy": self.taxonomy.value,
            "responsible": self.responsible.as_dict() if self.responsible else None,
            "ranked": [s.as_dict() for s in self.ranked],
            "note": self.note,
        }


_RANKING_NOTE = (
    "Scores are relative ranking weights, not calibrated probabilities (ADR-0084)."
)


class CapsuleNotFoundError(FileNotFoundError):
    """Raised when the capsule directory or its manifest cannot be read."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _error_text(rec: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("error", "exception", "message", "type", "name", "tool_name"):
        val = rec.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            for sub in ("type", "message"):
                sv = val.get(sub)
                if isinstance(sv, str):
                    parts.append(sv)
    attrs = rec.get("attributes")
    if isinstance(attrs, dict):
        ec = attrs.get("exit_code")
        if isinstance(ec, int) and ec != 0:
            parts.append(f"exit code {ec}")
    return " ".join(parts)


def _has_error(rec: dict[str, Any]) -> bool:
    status = rec.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failure", "failed"}:
        return True
    if rec.get("error") or rec.get("exception"):
        return True
    if rec.get("traceback_ref"):
        return True
    attrs = rec.get("attributes")
    if isinstance(attrs, dict):
        ec = attrs.get("exit_code")
        if isinstance(ec, int) and ec != 0:
            return True
    return False


def _classify(error_text: str, name: str) -> AgentErrorTaxonomy:
    haystack = f"{error_text} {name}".lower()
    if not haystack.strip():
        return AgentErrorTaxonomy.UNKNOWN
    for category, cues in _TAXONOMY_CUES:
        if any(cue in haystack for cue in cues):
            return category
    return AgentErrorTaxonomy.UNKNOWN


def _load_steps(capsule_dir: Path, manifest: dict[str, Any]) -> list[Step]:
    """Decompose the run into ordered steps from trace + tool + model streams."""
    steps: list[Step] = []
    ordinal = 0
    sources = [
        (manifest.get("trace_ref", "trace.jsonl"), "span"),
        (manifest.get("tool_calls_ref", "tool-calls.jsonl"), "tool"),
        (manifest.get("model_calls_ref", "model-calls.jsonl"), "model"),
    ]
    for ref, kind in sources:
        if not isinstance(ref, str):
            continue
        for rec in _read_jsonl(capsule_dir / ref):
            sid = rec.get("span_id") or rec.get("id") or rec.get("call_id")
            step_id = str(sid) if sid is not None else f"{kind}-{ordinal}"
            name = str(
                rec.get("name")
                or rec.get("tool_name")
                or rec.get("model")
                or kind
            )
            steps.append(
                Step(
                    step_id=step_id,
                    ordinal=ordinal,
                    name=name,
                    kind=kind,
                    started_at=rec.get("started_at"),
                    run_id=rec.get("run_id"),
                    has_error=_has_error(rec),
                    error_text=_error_text(rec),
                    raw=rec,
                )
            )
            ordinal += 1
    # Order by start time when available, falling back to discovery ordinal.
    steps.sort(key=lambda s: (s.started_at or "", s.ordinal))
    # Re-stamp ordinals to reflect the sorted rollout order (earliest-first).
    for i, step in enumerate(steps):
        step.ordinal = i
    return steps


def _downstream_run_ids(
    run_id: str, lineage_store: LineageStore | None
) -> set[str]:
    """Run ids reachable downstream of *run_id* via ``delegated_to``/``spawned``.

    Used for the src-413 causal-depth bias: a step that belongs to a downstream
    (delegated/spawned) run is a more proximate cause than its coordinator
    ancestor. Absent a lineage store or edges, returns an empty set and the bias
    term is zero (flat-trace attribution still works — self-contained).
    """
    if lineage_store is None:
        return set()
    downstream: set[str] = set()
    for edge_type in ("delegated_to", "spawned", "contains"):
        try:
            rows = lineage_store.provenance(run_id, kind="run", edge_type=edge_type)
        except Exception:  # pragma: no cover - defensive: never fail the analysis
            continue
        for row in rows:
            if row.get("kind") == "run" and row.get("ref"):
                downstream.add(str(row["ref"]))
    return downstream


def attribute_failure(
    capsule_dir: Path,
    lineage_store: LineageStore | None = None,
) -> RunAttribution:
    """Attribute a failed run to its most likely responsible step.

    Reads ``capsule.yaml`` plus the trace / tool-call / model-call streams under
    *capsule_dir*; optionally consults *lineage_store* for causal-depth bias.
    Pure and read-only. See ADR-0084 for the algorithm.
    """
    capsule_dir = Path(capsule_dir)
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise CapsuleNotFoundError(f"No capsule.yaml under {capsule_dir}")
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    run_id = str(manifest.get("run_id", capsule_dir.name))
    status = str(manifest.get("status", "unknown"))

    steps = _load_steps(capsule_dir, manifest)
    downstream = _downstream_run_ids(run_id, lineage_store)

    erroring = [s for s in steps if s.has_error]
    n_err = len(erroring)

    # --- Coarse pass: score every step with bounded, additive signals. -------
    candidates: list[StepAttribution] = []
    for step in steps:
        if not step.has_error:
            continue
        score = 1.0  # base weight for any hard error signal

        # Earliest-failure bias (src-411): earlier errors are likelier root
        # causes; decay linearly with position among erroring steps.
        err_rank = sorted(s.ordinal for s in erroring).index(step.ordinal)
        if n_err > 1:
            score += 0.5 * (1.0 - err_rank / (n_err - 1))

        # Causal-depth bias (src-413): a downstream/delegated step is a more
        # proximate cause than its coordinator ancestor.
        if step.run_id and step.run_id in downstream:
            score += 0.75

        taxonomy = _classify(step.error_text, step.name)
        rationale = (
            f"error signal on {step.kind} step '{step.name}'"
            f"; rollout position {step.ordinal}"
        )
        if step.run_id and step.run_id in downstream:
            rationale += "; downstream (delegated/spawned) of the coordinator"
        candidates.append(
            StepAttribution(
                step_id=step.step_id,
                name=step.name,
                kind=step.kind,
                score=score,
                taxonomy=taxonomy,
                rationale=rationale,
            )
        )

    # Fall back to the run-level error block when no per-step error was found
    # but the manifest reports a failure (e.g. a bare non-zero exit).
    run_error = manifest.get("error")
    if not candidates and isinstance(run_error, dict):
        err_text = _error_text(run_error)
        taxonomy = _classify(err_text, str(run_error.get("type", "")))
        candidates.append(
            StepAttribution(
                step_id="<run>",
                name=str(run_error.get("type", "run")),
                kind="run",
                score=1.0,
                taxonomy=taxonomy,
                rationale="run-level error block; no per-step error signal found",
            )
        )

    # --- Fine pass: rank, then choose the single responsible step. -----------
    # Rank by score desc, then earliest-first via the original step ordinal.
    ordinal_of = {s.step_id: s.ordinal for s in steps}
    candidates.sort(key=lambda c: (-c.score, ordinal_of.get(c.step_id, 1 << 30)))

    if not candidates:
        return RunAttribution(
            run_id=run_id,
            status=status,
            responsible=None,
            taxonomy=AgentErrorTaxonomy.UNKNOWN,
            ranked=[],
            note=(
                "No error signal found in capsule; cannot attribute a culprit. "
                + _RANKING_NOTE
            ),
        )

    responsible = candidates[0]
    return RunAttribution(
        run_id=run_id,
        status=status,
        responsible=responsible,
        taxonomy=responsible.taxonomy,
        ranked=candidates,
        note=_RANKING_NOTE,
    )
