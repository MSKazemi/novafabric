"""CLI tests for `nova kg detect` — unsupervised SPKG anomaly scan (ADR-0111 SP-2 baseline).

Needs no optional extra (the detector is pure-stdlib), so these run in the base env.
"""
from __future__ import annotations

import json
from pathlib import Path

from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _capsule(path: Path, edges: list[dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )
    return path


def _benign(n: int) -> list[dict]:
    return [
        {"edge_type": "uses", "source": {"kind": "run", "ref": f"run-{i}"},
         "target": {"kind": "dataset", "ref": "dataset:train"},
         "created_at": "2026-07-02T14:00:00.000Z"}
        for i in range(n)
    ]


def _malicious() -> dict:
    return {"edge_type": "executes", "source": {"kind": "run", "ref": "run-x"},
            "target": {"kind": "tool", "ref": "tool:/bin/shell"},
            "created_at": "2026-07-02T14:00:00.000Z"}


def test_detect_table_ranks_outlier_first(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "target", _benign(20) + [_malicious()])
    result = runner.invoke(app, ["kg", "detect", str(cap), "-k", "3"])
    assert result.exit_code == 0, result.output
    assert "SPKG anomaly scan" in result.output
    # the shell edge maps to the Unix-shell technique
    assert "T1059.004" in result.output


def test_detect_json_emits_findings(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "target", _benign(10) + [_malicious()])
    result = runner.invoke(app, ["kg", "detect", str(cap), "--json", "-k", "2"])
    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)
    assert len(findings) == 2
    assert all("attack_technique_id" in f["explanation"] for f in findings)


def test_detect_json_output_file(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "target", _benign(5) + [_malicious()])
    out = tmp_path / "findings.json"
    result = runner.invoke(app, ["kg", "detect", str(cap), "--json", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "findings written" in result.output
    assert json.loads(out.read_text())  # non-empty valid JSON


def test_detect_with_baseline_corpus(tmp_path: Path) -> None:
    base = _capsule(tmp_path / "normal", _benign(30))
    cap = _capsule(tmp_path / "target", _benign(3) + [_malicious()])
    result = runner.invoke(
        app, ["kg", "detect", str(cap), "--baseline", str(base), "-k", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "baseline edges" in result.output
    assert "T1059.004" in result.output


def test_detect_empty_capsule(tmp_path: Path) -> None:
    cap = tmp_path / "empty"
    cap.mkdir()
    result = runner.invoke(app, ["kg", "detect", str(cap)])
    assert result.exit_code == 0
    assert "No lineage edges" in result.output


def test_detect_help() -> None:
    result = runner.invoke(app, ["kg", "detect", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--baseline")
