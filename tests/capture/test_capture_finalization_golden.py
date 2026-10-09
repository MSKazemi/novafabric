"""`nova capture` finalization is pinned so a refactor of it cannot drift silently.

The tail of ``CaptureOrchestrator.run`` -- manifest redaction, lineage, late scans,
the ADR-0009 residual pass, ADR-0251 ``evidence_digests``, the manifest gate and the
opt-in seal -- moved into ``capture/finalize.py`` so that framework-adapter and
``@agent`` capsules go through the same steps (2026-10-09). These sets were recorded
from the orchestrator *before* that move; the move must leave every one unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from novafabric.capture.orchestrator import CaptureOrchestrator

#: Top-level keys of a clean, unsealed `nova capture` manifest.
EXPECTED_MANIFEST_KEYS = {
    "assets_ref",
    "capture_mode",
    "command",
    "created_at",
    "duration_ms",
    "environment_ref",
    "evidence_digests",
    "exit_code",
    "finished_at",
    "host",
    "inputs",
    "model_call_count",
    "model_calls_ref",
    "mutating_tool_count",
    "novafabric_version",
    "outputs",
    "redaction_proof_ref",
    "replay_policy_ref",
    "run_id",
    "schema_version",
    "status",
    "tool_call_count",
    "tool_calls_ref",
    "trace_ref",
    "trace_root_span_id",
    "working_directory",
}

#: Every file `evidence_digests` binds for that capture.
EXPECTED_BOUND_FILES = {
    "assets.jsonl",
    "env.lock",
    "lineage.jsonl",
    "model-calls.jsonl",
    "outputs/stdout.txt",
    "redaction-proof.json",
    "replay.yaml",
    "tool-calls.jsonl",
    "trace.jsonl",
}

#: Proof targets after the residual pass (every bound file but the proof itself).
EXPECTED_PROOF_TARGETS = EXPECTED_BOUND_FILES - {"redaction-proof.json"} | {"capsule.yaml"}


def test_clean_capture_manifest_digests_and_proof_are_unchanged(tmp_path: Path) -> None:
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "print('ok')"]
    )
    capsule = result.capsule_dir
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    proof = json.loads((capsule / "redaction-proof.json").read_text())

    assert set(manifest) == EXPECTED_MANIFEST_KEYS
    assert set(manifest["evidence_digests"]) == EXPECTED_BOUND_FILES
    assert {t["ref"] for t in proof["targets"]} == EXPECTED_PROOF_TARGETS
    assert proof["residual_check"]["files_rescanned"] == len(EXPECTED_BOUND_FILES) - 1
    assert proof["residual_check"]["residual_findings"] == 0
    # No signing profile: sealing stays opt-in (ADR-0301).
    assert not (capsule / ".seal").exists()
    assert {p.relative_to(capsule).as_posix() for p in capsule.rglob("*") if p.is_file()} == (
        EXPECTED_BOUND_FILES | {"capsule.yaml"}
    )
