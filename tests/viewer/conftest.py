"""Fixtures for the shareable capsule viewer (ADR-0140) tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

#: A secret-looking value stored ONLY inside tool-call arguments — the viewer
#: projection must never surface it (ADR-0140 D3).
SECRET_ARGUMENT_VALUE = "hunter2-SECRET-argument-value"

#: A redaction marker stored in a projected field — must render verbatim.
REDACTION_MARKER = "[REDACTED:email]"

RUN_ID = "01HTEST000000000000000001"


def _manifest(run_id: str) -> dict:
    return {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-01-01T00:00:00.000000Z",
        "finished_at": "2026-01-01T00:00:10.000000Z",
        "duration_ms": 10000,
        "status": "success",
        "command": ["python", "agent.py"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.58.0",
        "working_directory": "~/projects/myagent",
        "host": {"os": "linux", "arch": "x86_64", "python": "3.12.0"},
        "environment_ref": "env.lock",
        "replay_policy_ref": "replay.yaml",
        "redaction_proof_ref": "redaction-proof.json",
        "trace_ref": "trace.jsonl",
        "trace_root_span_id": "span-abc123",
        "model_calls_ref": "model-calls.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "assets_ref": "assets.jsonl",
        "inputs": [],
        "outputs": [],
        "model_call_count": 1,
        "tool_call_count": 2,
        "mutating_tool_count": 1,
        "exit_code": 0,
        "metadata": {"agent_id": "kubernetes_sentinel"},
        "capsule_hash": "sha256:" + "ab" * 32,
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def golden_capsule_dir(tmp_path: Path) -> Path:
    """A golden capsule with model calls, tool calls, scores, and lineage.

    Includes an already-redacted tool argument (never projected) and a
    redaction marker in a projected field (must stay verbatim).
    """
    capsule_dir = tmp_path / "runs" / RUN_ID
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(_manifest(RUN_ID)))
    _write_jsonl(
        capsule_dir / "model-calls.jsonl",
        [
            {
                "model_call_id": "01HXAY7M6FN9TQGE0V0M7PAY1Q",
                "gen_ai.request.model": "claude-x",
                "gen_ai.response.model": "claude-x-20260101",
                "gen_ai.usage.input_tokens": 812,
                "gen_ai.usage.output_tokens": 344,
                "duration_ms": 1420,
                "status": "success",
                "gen_ai.request.messages": [
                    {"role": "user", "content": f"contact {REDACTION_MARKER} please"}
                ],
            },
            {
                "model_call_id": "01HXAY7M6FN9TQGE0V0M7PAY2R",
                "gen_ai.request.model": "claude-x",
                "status": "error",
            },
        ],
    )
    _write_jsonl(
        capsule_dir / "tool-calls.jsonl",
        [
            {
                "tool_call_id": "01HXAY7M7QM4YZ2K7N9DPBYK2W",
                "tool_name": "web_search",
                "mutation_class": "read-only",
                "status": "success",
                "duration_ms": 178,
                "arguments": {"q": "weather"},
            },
            {
                "tool_call_id": "01HXAY7MASW4YZ2K7N9DPBYK2X",
                "tool_name": f"send_email {REDACTION_MARKER}",
                "mutation_class": "external-side-effect",
                "status": "denied",
                "latency_ms": 100,
                "arguments": {"to": REDACTION_MARKER, "body": SECRET_ARGUMENT_VALUE},
                "result": {"detail": SECRET_ARGUMENT_VALUE},
            },
        ],
    )
    _write_jsonl(
        capsule_dir / "scores.jsonl",
        [
            {"name": "gaia", "value": 0.71},
            {"name": "task_pass", "value": True},
        ],
    )
    _write_jsonl(
        capsule_dir / "lineage.jsonl",
        [
            {
                "edge_id": "01HXAY7MEDGE0000000000001",
                "edge_type": "consumed",
                "source": "dataset:sha256:1234",
                "target": f"capsule:{RUN_ID}",
            },
            {
                "edge_id": "01HXAY7MEDGE0000000000002",
                "edge_type": "produced",
                "source": f"capsule:{RUN_ID}",
                "target": "model:claude-x",
            },
        ],
    )
    return capsule_dir


@pytest.fixture
def empty_capsule_dir(tmp_path: Path) -> Path:
    """A capsule with a manifest but no event/score/lineage files at all."""
    capsule_dir = tmp_path / "runs" / "01HTEST000000000000000002"
    capsule_dir.mkdir(parents=True)
    manifest = _manifest("01HTEST000000000000000002")
    del manifest["metadata"]
    del manifest["capsule_hash"]
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
    return capsule_dir
