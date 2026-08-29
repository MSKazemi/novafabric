from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ReplayResult:
    replay_id: str
    replay_of_run_id: str
    mode: str
    status: str  # "success" | "failure" | "aborted" | "dry_run"
    start_time: str
    end_time: str
    duration_ms: int
    policy_flags_used: list[str]
    env_warnings: list[dict[str, str]]
    model_calls_mocked: int = 0
    # ADR-0261. This counts tool responses actually SERVED FROM CACHE, which is
    # currently always zero: `_run_mocked_subprocess` writes only model calls
    # into the replay queue and the hook loader installs only
    # `MockModelDispatcher`. `MockToolDispatcher` exists but has no `install()`
    # and is never instantiated, so no tool response is ever substituted. Until
    # that lands, reporting `len(tool_calls)` here asserted work the engine had
    # not done. The capsule's tool-call count is preserved in
    # `tool_calls_available` below.
    tool_calls_mocked: int = 0
    # ADR-0261, additive and optional: tool calls the capsule carried into this
    # replay and that a tool dispatcher COULD serve. Not a claim that any were.
    tool_calls_available: int | None = None
    exit_code: int | None = None
    error: dict[str, Any] | None = None
    # semantic-mode fields
    similarity_score: float | None = None
    matched_run_id: str | None = None
    # intervention-mode fields (ADR-0086)
    intervention: dict[str, Any] | None = None
    # exact-mode fields
    exact_eligible: bool | None = None
    exact_hash_count: int | None = None
    exact_reasons: list[str] | None = None
    # tool-call schema drift findings (ADR-0128; additive, optional)
    schema_drift: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": "0.1.0",
            "replay_id": self.replay_id,
            "replay_of_run_id": self.replay_of_run_id,
            "mode": self.mode,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "policy_flags_used": self.policy_flags_used,
            "env_warnings": self.env_warnings,
            "model_calls_mocked": self.model_calls_mocked,
            "tool_calls_mocked": self.tool_calls_mocked,
        }
        if self.tool_calls_available is not None:
            d["tool_calls_available"] = self.tool_calls_available
        if self.exit_code is not None:
            d["exit_code"] = self.exit_code
        if self.error is not None:
            d["error"] = self.error
        if self.similarity_score is not None:
            d["similarity_score"] = self.similarity_score
        if self.matched_run_id is not None:
            d["matched_run_id"] = self.matched_run_id
        if self.intervention is not None:
            d["intervention"] = self.intervention
        if self.exact_eligible is not None:
            d["exact_eligible"] = self.exact_eligible
        if self.exact_hash_count is not None:
            d["exact_hash_count"] = self.exact_hash_count
        if self.exact_reasons is not None:
            d["exact_reasons"] = self.exact_reasons
        if self.schema_drift is not None:
            d["schema_drift"] = self.schema_drift
        return d


def write_replay_result(result: ReplayResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "replay_result.yaml"
    path.write_text(yaml.dump(result.as_dict(), allow_unicode=True))
    return path
