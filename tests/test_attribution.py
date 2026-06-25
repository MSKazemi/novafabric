"""Tests for failure attribution over the lineage / causal graph (ADR-0084).

Covers:
- AgentErrorTaxonomy enum values are stable (src-411 five modules + UNKNOWN).
- A multi-step failed run where the responsible step is known is ranked top.
- The taxonomy label for that step matches the injected failure category.
- Earliest-root-cause bias (src-411): an earlier hard error outranks a later
  downstream symptom.
- A clean (no-error) run yields UNKNOWN and no fabricated culprit.
- A run with no trace files degrades safely (no crash, UNKNOWN).
- Lineage causal-depth bias (src-413): a downstream/delegated step outranks its
  coordinator ancestor when both error.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.diagnose import (
    AgentErrorTaxonomy,
    attribute_failure,
)


def _write_capsule(
    root: Path,
    run_id: str,
    *,
    status: str = "failure",
    trace: list[dict] | None = None,
    tool_calls: list[dict] | None = None,
    model_calls: list[dict] | None = None,
    error: dict | None = None,
) -> Path:
    cap = root / run_id
    cap.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": status,
        "trace_ref": "trace.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "model_calls_ref": "model-calls.jsonl",
    }
    if error is not None:
        manifest["error"] = error
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "trace.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (trace or []))
    )
    (cap / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (tool_calls or []))
    )
    (cap / "model-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (model_calls or []))
    )
    return cap


class TestTaxonomy:
    def test_enum_has_five_src411_modules_plus_unknown(self) -> None:
        values = {m.value for m in AgentErrorTaxonomy}
        assert values == {
            "MEMORY",
            "REFLECTION",
            "PLANNING",
            "ACTION",
            "SYSTEM",
            "UNKNOWN",
        }


class TestAttribution:
    def test_responsible_step_ranked_top_and_labelled(self, tmp_path: Path) -> None:
        # Three-step run: planner ok, retriever ok, the *action* step (a tool
        # call) fails — that is the known responsible step.
        cap = _write_capsule(
            tmp_path,
            "run-action-fail",
            trace=[
                {
                    "span_id": "s1",
                    "name": "planner.decompose",
                    "started_at": "2026-06-11T10:00:00.000Z",
                    "status": "ok",
                },
                {
                    "span_id": "s2",
                    "name": "retriever.fetch",
                    "started_at": "2026-06-11T10:00:01.000Z",
                    "status": "ok",
                },
                {
                    "span_id": "s3",
                    "name": "tool.call",
                    "started_at": "2026-06-11T10:00:02.000Z",
                    "status": "error",
                },
            ],
            tool_calls=[
                {
                    "span_id": "s3",
                    "tool_name": "http_post",
                    "status": "error",
                    "error": "ConnectionError: refused",
                    "started_at": "2026-06-11T10:00:02.000Z",
                }
            ],
            error={"type": "ToolError", "message": "http_post failed"},
        )

        result = attribute_failure(cap)

        assert result.responsible is not None
        assert result.responsible.step_id == "s3"
        assert result.responsible.taxonomy == AgentErrorTaxonomy.ACTION
        # ranked: the responsible step must be rank 0
        assert result.ranked[0].step_id == "s3"
        assert result.taxonomy == AgentErrorTaxonomy.ACTION

    def test_earliest_root_cause_outranks_downstream_symptom(
        self, tmp_path: Path
    ) -> None:
        # Two erroring steps: the earlier planning error is the root cause; the
        # later reflection error is a downstream symptom and must rank lower.
        cap = _write_capsule(
            tmp_path,
            "run-earliest",
            trace=[
                {
                    "span_id": "p1",
                    "name": "planner.plan",
                    "started_at": "2026-06-11T10:00:00.000Z",
                    "status": "error",
                    "error": "planning produced an invalid subgoal sequence",
                },
                {
                    "span_id": "r1",
                    "name": "reflect.evaluate",
                    "started_at": "2026-06-11T10:00:05.000Z",
                    "status": "error",
                    "error": "self-critique could not recover",
                },
            ],
        )

        result = attribute_failure(cap)

        assert result.responsible is not None
        assert result.responsible.step_id == "p1"
        assert result.responsible.taxonomy == AgentErrorTaxonomy.PLANNING
        # downstream symptom present but ranked below the root cause
        ids = [s.step_id for s in result.ranked]
        assert ids.index("p1") < ids.index("r1")

    def test_clean_run_yields_unknown_no_culprit(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-clean",
            status="success",
            trace=[
                {
                    "span_id": "s1",
                    "name": "ok.step",
                    "started_at": "2026-06-11T10:00:00.000Z",
                    "status": "ok",
                }
            ],
        )

        result = attribute_failure(cap)

        assert result.responsible is None
        assert result.taxonomy == AgentErrorTaxonomy.UNKNOWN

    def test_missing_trace_files_degrade_safely(self, tmp_path: Path) -> None:
        cap = tmp_path / "run-bare"
        cap.mkdir()
        (cap / "capsule.yaml").write_text(
            yaml.dump({"run_id": "run-bare", "status": "failure"})
        )

        result = attribute_failure(cap)

        # No fabricated culprit, no crash.
        assert result.taxonomy == AgentErrorTaxonomy.UNKNOWN

    def test_system_failure_from_nonzero_exit(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-syserr",
            error={"type": "NonZeroExit", "message": "Command exited with code 1"},
            trace=[
                {
                    "span_id": "root",
                    "name": "novafabric.capture",
                    "started_at": "2026-06-11T10:00:00.000Z",
                    "status": "error",
                    "attributes": {"exit_code": 1},
                }
            ],
        )

        result = attribute_failure(cap)

        assert result.responsible is not None
        assert result.responsible.taxonomy == AgentErrorTaxonomy.SYSTEM


class TestLineageCausalDepth:
    def test_downstream_delegated_step_outranks_coordinator(
        self, tmp_path: Path
    ) -> None:
        """src-413: with a delegated_to edge, the downstream worker step that
        errors is a more proximate cause than the coordinator that also surfaces
        an error."""
        from novafabric.lineage._store import LineageStore
        from novafabric.lineage._types import LineageEdge

        db = tmp_path / "lineage.db"
        store = LineageStore(db_path=db)
        # coordinator run delegates to a worker run
        store.insert_edge(
            LineageEdge(
                edge_type="delegated_to",
                source={"kind": "run", "run_id": "coordinator"},
                target={"kind": "run", "run_id": "worker"},
                confidence="high",
                capsule_run_id="coordinator",
            )
        )

        cap = _write_capsule(
            tmp_path,
            "coordinator",
            trace=[
                {
                    "span_id": "coordinator",
                    "name": "coordinator.run",
                    "started_at": "2026-06-11T10:00:00.000Z",
                    "status": "error",
                    "error": "child task failed",
                },
                {
                    "span_id": "worker",
                    "name": "worker.run",
                    "started_at": "2026-06-11T10:00:01.000Z",
                    "status": "error",
                    "error": "tool call rejected",
                    "run_id": "worker",
                },
            ],
        )

        result = attribute_failure(cap, lineage_store=store)

        ids = [s.step_id for s in result.ranked]
        # worker (downstream) must rank above coordinator (ancestor)
        assert ids.index("worker") < ids.index("coordinator")
