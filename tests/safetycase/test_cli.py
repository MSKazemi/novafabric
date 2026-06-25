"""ADR-0095 slice C1 — CLI smoke for `safety_case_app` (build / verify / export).

The app is NOT registered in cli/main.py (deferred to the parent); it is tested in
isolation. Each verb exits 0 on a valid fixture and non-zero with a named error
otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.safety_case import safety_case_app

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    att = cap / "attestations"
    att.mkdir()
    (att / "reperformance.json").write_text(
        json.dumps({"run_id": cap.name, "match": "exact", "replay_mode": "exact"})
    )
    (cap / "completeness.json").write_text(json.dumps({"run_id": cap.name}))
    return cap


class TestBuild:
    def test_build_writes_case(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        result = runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        case = json.loads(out.read_text())
        assert case["template_id"] == "clymer-generic-v0"
        assert case["case_hash"].startswith("sha256:")

    def test_build_stdout(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        result = runner.invoke(
            safety_case_app, ["build", str(cap), "--template", "clymer-generic-v0"]
        )
        assert result.exit_code == 0, result.output
        assert "schema_version" in result.output

    def test_build_unknown_template_nonzero(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        result = runner.invoke(
            safety_case_app, ["build", str(cap), "--template", "nope-v9"]
        )
        assert result.exit_code != 0
        assert "template" in result.output.lower()

    def test_build_invalid_capsule_nonzero(self) -> None:
        result = runner.invoke(
            safety_case_app, ["build", "no-such-dir", "--template", "clymer-generic-v0"]
        )
        assert result.exit_code != 0


class TestVerify:
    def test_verify_clean_case_zero(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        result = runner.invoke(
            safety_case_app, ["verify", str(out), "--capsule", str(cap)]
        )
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_verify_tampered_artifact_nonzero(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        (cap / "attestations" / "reperformance.json").write_text("{}")
        result = runner.invoke(
            safety_case_app, ["verify", str(out), "--capsule", str(cap)]
        )
        assert result.exit_code != 0
        assert "FAILED" in result.output

    def test_verify_missing_case_file_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(
            safety_case_app, ["verify", str(tmp_path / "nope.json"), "--capsule", str(tmp_path)]
        )
        assert result.exit_code != 0


class TestExport:
    def test_export_markdown(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        result = runner.invoke(
            safety_case_app, ["export", str(out), "--format", "markdown"]
        )
        assert result.exit_code == 0, result.output
        assert "# Safety Case" in result.output
        assert "backing_state" in result.output or "Backing" in result.output

    def test_export_json(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        result = runner.invoke(safety_case_app, ["export", str(out), "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["template_id"] == "clymer-generic-v0"

    def test_export_annex_iv(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        result = runner.invoke(
            safety_case_app, ["export", str(out), "--format", "annex-iv"]
        )
        assert result.exit_code == 0, result.output
        assert "Annex IV" in result.output

    def test_export_nist_rmf(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "nist-ai-rmf-v0", "-o", str(out)],
        )
        result = runner.invoke(
            safety_case_app, ["export", str(out), "--format", "nist-rmf"]
        )
        assert result.exit_code == 0, result.output
        assert "NIST AI Risk Management Framework" in result.output

    def test_export_malformed_json_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        result = runner.invoke(
            safety_case_app, ["export", str(bad), "--format", "json"]
        )
        assert result.exit_code != 0
        assert "Malformed" in result.output

    def test_export_invalid_case_nonzero(self, tmp_path: Path) -> None:
        invalid = tmp_path / "invalid.json"
        invalid.write_text(json.dumps({"not": "a safety case"}))
        result = runner.invoke(
            safety_case_app, ["export", str(invalid), "--format", "json"]
        )
        assert result.exit_code != 0
        assert "Invalid" in result.output

    def test_export_to_file(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        out = tmp_path / "safety-case.json"
        md = tmp_path / "case.md"
        runner.invoke(
            safety_case_app,
            ["build", str(cap), "--template", "clymer-generic-v0", "-o", str(out)],
        )
        result = runner.invoke(
            safety_case_app,
            ["export", str(out), "--format", "markdown", "-o", str(md)],
        )
        assert result.exit_code == 0, result.output
        assert md.exists()
        assert "# Safety Case" in md.read_text()
