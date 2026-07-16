"""End-to-end: masking pipeline wired into capture (ADR-0135 P4).

The captured command line lands in trace.jsonl (span attributes), which is
a scan target — so a masker over the command string proves the whole loop:
capture → built-in scan → custom mask → attributed proof → sealed capsule
files that never contain the raw value.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.capture.secrets import recompute_chain_hash
from novafabric.cli.main import app
from novafabric.masking import MaskerSpec, MaskingPipeline
from novafabric.masking._registry import LoadedMasker
from novafabric.masking.examples import EmailMasker

from .conftest import CrashingMasker

EMAIL = "alice.smith@example.com"


def _email_pipeline() -> MaskingPipeline:
    return MaskingPipeline(
        [LoadedMasker(masker=EmailMasker(), spec=MaskerSpec(id="novafabric-email"))]
    )


def _capture(tmp_path: Path, pipeline: MaskingPipeline | None):
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs", masking_pipeline=pipeline)
    return orch.run(command=[sys.executable, "-c", f"x = {EMAIL!r}"])


def test_capture_masks_email_and_extends_proof(tmp_path: Path) -> None:
    result = _capture(tmp_path, _email_pipeline())
    assert result.exit_code == 0

    trace = (result.capsule_dir / "trace.jsonl").read_text()
    assert EMAIL not in trace
    assert "[MASKED:email]" in trace

    proof = json.loads((result.capsule_dir / "redaction-proof.json").read_text())
    assert proof["masker_errors"] == []
    assert any(f["masker_id"] == "novafabric-email" for f in proof["masker_findings"])
    # the chain hash covers the new arrays and verifies
    assert recompute_chain_hash(dict(proof))["chain_hash"] == proof["chain_hash"]


def test_capture_without_pipeline_writes_plain_adr0009_proof(tmp_path: Path) -> None:
    result = _capture(tmp_path, None)
    proof = json.loads((result.capsule_dir / "redaction-proof.json").read_text())
    assert "masker_findings" not in proof
    assert "masker_errors" not in proof


def test_capture_survives_crashing_masker(tmp_path: Path) -> None:
    """Fail-safe for the workload: the run completes, the crash is recorded."""
    pipeline = MaskingPipeline(
        [LoadedMasker(masker=CrashingMasker(), spec=MaskerSpec(id="crashing-masker"))]
    )
    result = _capture(tmp_path, pipeline)
    assert result.exit_code == 0  # workload never blocked
    proof = json.loads((result.capsule_dir / "redaction-proof.json").read_text())
    assert proof["masker_findings"] == []
    assert proof["masker_errors"]
    assert all(e["reason"] == "raised" for e in proof["masker_errors"])
    assert EMAIL not in (result.capsule_dir / "trace.jsonl").read_text()


def test_nova_validate_accepts_extended_proof(tmp_path: Path) -> None:
    result = _capture(tmp_path, _email_pipeline())
    cli = CliRunner().invoke(app, ["validate", str(result.capsule_dir)])
    assert cli.exit_code == 0, cli.output


def test_cli_masking_config_flag(tmp_path: Path) -> None:
    cfg = tmp_path / "masking.yaml"
    cfg.write_text(
        "masking:\n"
        "  enabled: true\n"
        "  maskers:\n"
        "    - id: novafabric.masking.examples:EmailMasker\n"
    )
    out_dir = tmp_path / "runs"
    cli = CliRunner().invoke(
        app,
        [
            "capture",
            "--masking-config", str(cfg),
            "--output-dir", str(out_dir),
            sys.executable, "-c", f"x = {EMAIL!r}",
        ],
    )
    assert cli.exit_code == 0, cli.output
    (capsule_dir,) = [p for p in out_dir.iterdir() if p.is_dir()]
    assert EMAIL not in (capsule_dir / "trace.jsonl").read_text()
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    assert proof["masker_findings"]


def test_cli_disabled_config_is_inert(tmp_path: Path) -> None:
    cfg = tmp_path / "masking.yaml"
    cfg.write_text(
        "masking:\n  enabled: false\n  maskers:\n    - id: novafabric-email\n"
    )
    out_dir = tmp_path / "runs"
    cli = CliRunner().invoke(
        app,
        [
            "capture",
            "--masking-config", str(cfg),
            "--output-dir", str(out_dir),
            sys.executable, "-c", f"x = {EMAIL!r}",
        ],
    )
    assert cli.exit_code == 0, cli.output
    (capsule_dir,) = [p for p in out_dir.iterdir() if p.is_dir()]
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    assert "masker_findings" not in proof  # byte-for-byte ADR-0009 behavior


@pytest.mark.parametrize(
    "args",
    [
        ["--masker", "no-such-masker-anywhere"],
        ["--masking-config", "/nonexistent/masking.yaml"],
    ],
)
def test_cli_bad_masking_setup_fails_closed_before_workload(
    tmp_path: Path, args: list[str]
) -> None:
    marker = tmp_path / "ran.txt"
    cli = CliRunner().invoke(
        app,
        [
            "capture",
            *args,
            "--output-dir", str(tmp_path / "runs"),
            sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')",
        ],
    )
    assert cli.exit_code == 2
    assert not marker.exists(), "workload must not run when registration fails"
