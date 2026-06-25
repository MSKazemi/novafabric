from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

runner = CliRunner()

_FRESH_OPENAI = "sk-abcDEFghijKLMNopqRSTuvwxyz012345678901234567890123456"


def _make_capsule(tmp_path: Path) -> Path:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    return result.capsule_dir


def _inject_secret(capsule_dir: Path, secret: str = _FRESH_OPENAI) -> None:
    target = capsule_dir / "model-calls.jsonl"
    target.write_text(json.dumps({"api_key": secret}) + "\n")


def test_re_redact_clean_capsule_no_changes(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    proof_before = json.loads((capsule_dir / "redaction-proof.json").read_text())
    result = runner.invoke(app, ["redact", str(capsule_dir)])
    assert result.exit_code == 0
    proof_after = json.loads((capsule_dir / "redaction-proof.json").read_text())
    assert proof_after["findings_count"]["total"] == proof_before["findings_count"]["total"]


def test_re_redact_finds_injected_secret(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_secret(capsule_dir)
    proof_before = json.loads((capsule_dir / "redaction-proof.json").read_text())
    result = runner.invoke(app, ["redact", str(capsule_dir)])
    assert result.exit_code == 0
    proof_after = json.loads((capsule_dir / "redaction-proof.json").read_text())
    assert proof_after["findings_count"]["total"] >= 1
    assert proof_after["chain_hash"] != proof_before["chain_hash"]
    assert _FRESH_OPENAI not in (capsule_dir / "model-calls.jsonl").read_text()


def test_review_without_tty_exits_2(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    result = runner.invoke(app, ["redact", str(capsule_dir), "--review"])
    assert result.exit_code == 2
    assert "tty" in result.output.lower() or "interactive" in result.output.lower()


def test_strategy_override_hash_applied(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_secret(capsule_dir)
    result = runner.invoke(
        app,
        ["redact", str(capsule_dir), "--strategy-override", "openai-api-key:hash"],
    )
    assert result.exit_code == 0
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    openai_findings = [f for f in proof["findings"] if f["rule_id"] == "openai-api-key"]
    assert openai_findings, "expected at least one openai-api-key finding"
    for finding in openai_findings:
        assert finding["redaction_strategy"] == "hash"
        assert finding["replacement"].startswith("[REDACTED:openai-api-key:sha256:")


def test_mark_unsafe_skip_added(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_secret(capsule_dir)
    runner.invoke(app, ["redact", str(capsule_dir)])
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    finding_id = proof["findings"][0]["finding_id"]

    result = runner.invoke(
        app,
        [
            "redact", str(capsule_dir),
            "--mark-unsafe-skip", finding_id,
            "--rationale", "test fixture, false positive",
        ],
    )
    assert result.exit_code == 0
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    assert "unsafe_skips" in proof
    assert any(s["finding_id"] == finding_id for s in proof["unsafe_skips"])
    skip = next(s for s in proof["unsafe_skips"] if s["finding_id"] == finding_id)
    assert skip["rationale"] == "test fixture, false positive"


def test_clear_unsafe_skips(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_secret(capsule_dir)
    runner.invoke(app, ["redact", str(capsule_dir)])
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    finding_id = proof["findings"][0]["finding_id"]
    runner.invoke(
        app,
        [
            "redact", str(capsule_dir),
            "--mark-unsafe-skip", finding_id,
            "--rationale", "test",
        ],
    )

    result = runner.invoke(app, ["redact", str(capsule_dir), "--clear-unsafe-skips"])
    assert result.exit_code == 0
    proof = json.loads((capsule_dir / "redaction-proof.json").read_text())
    assert proof.get("unsafe_skips", []) == []
