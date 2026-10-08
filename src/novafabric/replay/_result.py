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
    # In `mocked` mode (ADR-0300): model responses the dispatcher actually
    # served from the capsule, counted from its event log -- not the capsule's
    # record count. Other modes keep their pre-0300 meaning (see ADR-0261 for
    # the open forensic-mode note).
    model_calls_mocked: int = 0
    # ADR-0261/ADR-0300: tool responses actually SERVED FROM THE CAPSULE. Only
    # `mocked` mode installs a tool dispatcher, and only on the
    # `mcp.ClientSession.call_tool` surface; every other path reports 0.
    tool_calls_mocked: int = 0
    # ADR-0261/ADR-0300, additive and optional: recorded tool calls on a surface
    # a tool dispatcher can serve (MCP tools/call). Not a claim that any were.
    tool_calls_available: int | None = None
    # ADR-0300, additive and optional: every tool call the capsule recorded,
    # whatever its surface (the pre-0300 value of `tool_calls_available`).
    tool_calls_recorded: int | None = None
    # ADR-0300, additive and optional, `mocked` mode only.
    model_calls_available: int | None = None  # servable recorded responses
    model_calls_unmatched: int | None = None  # calls with no recorded answer
    tool_calls_live: int | None = None  # intercepted-surface calls run live
    tool_calls_unmatched: int | None = None  # intercepted-surface calls refused
    queues_fully_consumed: bool | None = None
    divergence_reason: str | None = None
    replay_contract: dict[str, Any] | None = None
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
        for name in (
            "tool_calls_available",
            "tool_calls_recorded",
            "model_calls_available",
            "model_calls_unmatched",
            "tool_calls_live",
            "tool_calls_unmatched",
            "queues_fully_consumed",
            "divergence_reason",
            "replay_contract",
        ):
            value = getattr(self, name)
            if value is not None:
                d[name] = value
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
