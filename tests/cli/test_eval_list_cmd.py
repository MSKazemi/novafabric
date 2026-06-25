from __future__ import annotations

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_eval_list_shows_builtin_suites() -> None:
    """All six built-in suites appear in the table."""
    result = runner.invoke(app, ["eval", "list"])
    assert result.exit_code == 0
    expected_ids = [
        "novafabric-smoke-v1",
        "gaia-v1",
        "mmlu-v1",
        "truthful-qa-v1",
        "swe-bench-verified-v1",
        "agentbench-v1",
    ]
    for suite_id in expected_ids:
        assert suite_id in result.output, f"Missing suite: {suite_id}"


def test_eval_list_shows_version() -> None:
    """Version column is populated for each suite."""
    result = runner.invoke(app, ["eval", "list"])
    assert result.exit_code == 0
    # All built-in suites use 0.1.0
    assert "0.1.0" in result.output


def test_eval_list_shows_entry_point_module() -> None:
    """Entry point module paths appear in the table."""
    result = runner.invoke(app, ["eval", "list"])
    assert result.exit_code == 0
    assert "novafabric.evals.suites" in result.output


def test_eval_list_oci_column_present() -> None:
    """OCI Digest column exists (host-env suites show 'host-env')."""
    result = runner.invoke(app, ["eval", "list"])
    assert result.exit_code == 0
    assert "OCI Digest" in result.output
    assert "host-env" in result.output


def test_eval_list_table_headers() -> None:
    """All four column headers are present."""
    result = runner.invoke(app, ["eval", "list"])
    assert result.exit_code == 0
    for header in ("Suite ID", "Version", "OCI Digest", "Entry Point"):
        assert header in result.output
