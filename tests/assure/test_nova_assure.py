"""Tests for nova assure — OWASP Top 10 for LLM (2025) evidence report (E-10)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner  # noqa: E402

from novafabric.assure.checker import AssuranceChecker
from novafabric.assure.checks import (
    ALL_CHECKS,
    LLM01Check,
    LLM02Check,
    LLM03Check,
    LLM04Check,
    LLM05Check,
    LLM06Check,
    LLM07Check,
    LLM08Check,
    LLM09Check,
    LLM10Check,
)
from novafabric.assure.models import (
    AssuranceReport,
    AssuranceResult,
    CheckStatus,
)
from novafabric.cli.main import app

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_capsule_dir(tmp_path: Path, *, manifest: dict | None = None) -> Path:
    """Create a minimal valid capsule directory with all 7 artifact files."""
    cap = tmp_path / "cap"
    cap.mkdir()

    m = manifest or {
        "schema_version": "1.0.0",
        "run_id": "01TEST",
        "status": "success",
        "model_call_count": 2,
        "tool_call_count": 1,
        "duration_ms": 5000,
        "capture_mode": "direct",
        "novafabric_version": "0.26.0",
    }
    (cap / "capsule.yaml").write_text(yaml.dump(m))
    (cap / "model-calls.jsonl").write_text(
        json.dumps({"type": "model_call", "tokens": {"input": 100, "output": 50}}) + "\n"
    )
    (cap / "tool-calls.jsonl").write_text(
        json.dumps({"type": "tool_call", "tool": "search"}) + "\n"
    )
    (cap / "redaction-proof.json").write_text(
        json.dumps(
            {
                "secrets_found": 0,
                "redacted": [],
                "scan_timestamp": "2026-01-01T00:00:00Z",
            }
        )
    )
    (cap / "trace.jsonl").write_text(
        json.dumps({"span_id": "s1", "status": "ok", "name": "root"}) + "\n"
    )
    (cap / "replay.yaml").write_text(yaml.dump({"mode": "forensic", "allow_mutating": False}))
    (cap / "env.lock").write_text(yaml.dump({"python": "3.12.0", "packages": []}))
    return cap


# ---------------------------------------------------------------------------
# TestAssuranceModels
# ---------------------------------------------------------------------------

class TestAssuranceModels:
    def test_check_status_values(self) -> None:
        assert CheckStatus.PASS == "PASS"
        assert CheckStatus.FAIL == "FAIL"
        assert CheckStatus.WARN == "WARN"
        assert CheckStatus.SKIP == "SKIP"

    def test_assurance_result_creation(self) -> None:
        r = AssuranceResult(
            check_id="LLM01",
            category="LLM01-PromptInjection",
            status=CheckStatus.PASS,
            message="Redaction proof present with 0 secrets found",
            evidence={"secrets_found": 0},
        )
        assert r.check_id == "LLM01"
        assert r.status == CheckStatus.PASS

    def test_assurance_report_summary(self) -> None:
        results = [
            AssuranceResult(
                check_id="LLM01",
                category="LLM01-PromptInjection",
                status=CheckStatus.PASS,
                message="ok",
                evidence={},
            ),
            AssuranceResult(
                check_id="LLM02",
                category="LLM02-SensitiveInfoDisclosure",
                status=CheckStatus.FAIL,
                message="failed",
                evidence={},
            ),
        ]
        report = AssuranceReport(run_id="01TEST", capsule_path="/tmp/x", results=results)
        assert report.pass_count == 1
        assert report.fail_count == 1
        assert report.overall_status == CheckStatus.FAIL

    def test_report_all_pass_is_pass(self) -> None:
        results = [
            AssuranceResult(
                check_id=f"LLM0{i}",
                category=f"LLM0{i}-Category",
                status=CheckStatus.PASS,
                message="ok",
                evidence={},
            )
            for i in range(1, 5)
        ]
        report = AssuranceReport(run_id="TEST", capsule_path="/tmp/y", results=results)
        assert report.overall_status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# TestOWASPChecks
# ---------------------------------------------------------------------------

class TestOWASPChecks:
    def test_llm01_pass_when_redaction_proof_exists(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM01Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm01_fail_when_redaction_proof_missing(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        (cap / "redaction-proof.json").unlink()
        result = LLM01Check().run(cap)
        assert result.status.value == "FAIL"

    def test_llm02_fail_when_secrets_found(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        (cap / "redaction-proof.json").write_text(
            json.dumps({"secrets_found": 3, "redacted": ["token1", "token2", "token3"]})
        )
        result = LLM02Check().run(cap)
        assert result.status.value == "FAIL"

    def test_llm02_pass_when_zero_secrets(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM02Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm03_pass_when_env_lock_exists(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM03Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm03_fail_when_env_lock_missing(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        (cap / "env.lock").unlink()
        result = LLM03Check().run(cap)
        assert result.status.value == "FAIL"

    def test_llm04_pass_reasonable_token_counts(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM04Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm05_pass_when_tool_calls_present(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM05Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm06_warn_when_excessive_tool_ratio(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(
            tmp_path,
            manifest={
                "schema_version": "1.0.0",
                "run_id": "01TEST",
                "status": "success",
                "model_call_count": 1,
                "tool_call_count": 30,
                "duration_ms": 5000,
                "capture_mode": "direct",
                "novafabric_version": "0.26.0",
            },
        )
        result = LLM06Check().run(cap)
        assert result.status.value in ("WARN", "FAIL")

    def test_llm07_pass_no_system_prompt_leaked(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM07Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm08_pass_when_replay_policy_exists(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM08Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm09_pass_when_trace_spans_present(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM09Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm10_pass_reasonable_duration(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = LLM10Check().run(cap)
        assert result.status.value == "PASS"

    def test_llm10_fail_excessive_duration(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(
            tmp_path,
            manifest={
                "schema_version": "1.0.0",
                "run_id": "01TEST",
                "status": "success",
                "model_call_count": 1,
                "tool_call_count": 0,
                "duration_ms": 4_000_000,  # over 1-hour gate
                "capture_mode": "direct",
                "novafabric_version": "0.26.0",
            },
        )
        result = LLM10Check().run(cap)
        assert result.status.value in ("WARN", "FAIL")

    def test_all_checks_list_has_ten(self) -> None:
        assert len(ALL_CHECKS) == 10


# ---------------------------------------------------------------------------
# TestAssuranceChecker
# ---------------------------------------------------------------------------

class TestAssuranceChecker:
    def test_check_all_returns_ten_results(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        checker = AssuranceChecker()
        report = checker.check_all(cap)
        assert len(report.results) == 10

    def test_check_all_clean_capsule_all_pass(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        checker = AssuranceChecker()
        report = checker.check_all(cap)
        assert report.overall_status.value == "PASS"

    def test_check_all_missing_proof_fail(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        (cap / "redaction-proof.json").unlink()
        checker = AssuranceChecker()
        report = checker.check_all(cap)
        assert report.fail_count >= 1
        assert report.overall_status.value == "FAIL"


# ---------------------------------------------------------------------------
# TestAssureCLI
# ---------------------------------------------------------------------------

class TestAssureCLI:
    runner: CliRunner = CliRunner()

    def test_assure_clean_capsule_exits_zero(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = self.runner.invoke(app, ["assure", str(cap)])
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_assure_bad_capsule_exits_one(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        (cap / "redaction-proof.json").unlink()
        (cap / "env.lock").unlink()
        result = self.runner.invoke(app, ["assure", str(cap)])
        assert result.exit_code == 1

    def test_assure_json_output(self, tmp_path: Path) -> None:
        cap = _make_capsule_dir(tmp_path)
        result = self.runner.invoke(app, ["assure", str(cap), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "run_id" in data
        assert "results" in data
        assert len(data["results"]) == 10
        assert "overall_status" in data

    def test_assure_nonexistent_path_exits_two(self, tmp_path: Path) -> None:
        result = self.runner.invoke(app, ["assure", str(tmp_path / "nonexistent")])
        assert result.exit_code == 2
