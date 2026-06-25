# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Fixtures for compliance export tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def minimal_capsule_dir(tmp_path: Path) -> Path:
    """Create a minimal valid capsule directory for export tests."""
    capsule_dir = tmp_path / "capsules" / "01HTEST000000000000000001"
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "model-calls.jsonl").touch()
    (capsule_dir / "tool-calls.jsonl").touch()
    (capsule_dir / "trace.jsonl").touch()
    (capsule_dir / "assets.jsonl").touch()
    manifest = {
        "schema_version": "0.1.0",
        "run_id": "01HTEST000000000000000001",
        "created_at": "2024-01-01T00:00:00.000000Z",
        "finished_at": "2024-01-01T00:00:10.000000Z",
        "duration_ms": 10000,
        "status": "success",
        "command": ["python", "agent.py"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.14.4",
        "working_directory": "~/projects/myagent",
        "host": {
            "os": "linux",
            "arch": "x86_64",
            "python": "3.12.0",
            "cpu_count": 8,
            "memory_bytes": 16000000000,
            "gpu": [],
            "hostname_redacted": True,
        },
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
        "model_call_count": 5,
        "tool_call_count": 3,
        "mutating_tool_count": 0,
        "exit_code": 0,
    }
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
    return capsule_dir
