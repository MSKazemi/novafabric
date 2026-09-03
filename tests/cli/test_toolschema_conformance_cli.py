"""NF-168 — the ``nova toolschema conformance`` CLI (ADR-0148 D2).

Sealing records and exits `0`; `--verify` is a *check* and exits `1` on a mismatch, matching
`nova assure-baseline verify`. The end-to-end test seals a real capsule, tampers with the file,
and re-verifies — which is the only way to show the seal detects tampering rather than merely
carrying a digest.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _capsule(tmp_path: Path, *, calls: list[dict]) -> Path:
    d = tmp_path / "run-42"
    d.mkdir(parents=True)
    d.joinpath("capsule.json").write_text(
        json.dumps({"run_id": "run-42", "created_at": "2026-07-10T00:00:00Z"})
    )
    d.joinpath("tool-calls.jsonl").write_text(
        "\n".join(json.dumps(c) for c in calls) + "\n"
    )
    return d


def _call(name: str, verdict: dict | None) -> dict:
    record: dict = {"tool_name": name, "tool_version": "1.0.0", "arguments": {}}
    if verdict is not None:
        record["schema_validation"] = verdict
    return record


PASS = {"arguments_valid": True, "result_valid": None, "validator": "v1", "errors": []}
FAIL = {"arguments_valid": False, "result_valid": None, "validator": "v1", "errors": ["bad"]}


def test_sealing_reports_the_three_counts(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, calls=[_call("a", PASS), _call("b", FAIL), _call("c", None)])
    result = runner.invoke(app, ["toolschema", "conformance", "--capsule", str(capsule), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (payload["conforming"], payload["violating"], payload["unchecked"]) == (1, 1, 1)
    assert payload["sealed_digest"].startswith("sha256:")


def test_a_violation_does_not_make_it_exit_non_zero(tmp_path: Path) -> None:
    """Sealing records; it does not gate."""
    capsule = _capsule(tmp_path, calls=[_call("b", FAIL)])
    result = runner.invoke(app, ["toolschema", "conformance", "--capsule", str(capsule)])
    assert result.exit_code == 0, result.output


def test_the_predicate_fragment_omits_the_verdicts(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, calls=[_call("a", PASS)])
    result = runner.invoke(
        app, ["toolschema", "conformance", "--capsule", str(capsule), "--predicate", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)["novafabric.dev/tool-schema-conformance/v1"]
    assert "sealed_digest" in body
    assert "verdicts" not in body


def test_seal_then_verify_then_tamper_then_fail(tmp_path: Path) -> None:
    """The whole point, end to end: the seal must actually detect a changed record."""
    capsule = _capsule(tmp_path, calls=[_call("a", PASS), _call("b", FAIL)])
    sealed = runner.invoke(
        app, ["toolschema", "conformance", "--capsule", str(capsule), "--json"]
    )
    assert sealed.exit_code == 0, sealed.output
    digest = json.loads(sealed.stdout)["sealed_digest"]

    ok = runner.invoke(
        app, ["toolschema", "conformance", "--capsule", str(capsule), "--verify", digest]
    )
    assert ok.exit_code == 0, ok.output
    assert "verified" in ok.output.lower()

    # Rewrite the failing verdict as a pass — exactly what a seal exists to catch.
    capsule.joinpath("tool-calls.jsonl").write_text(
        "\n".join(json.dumps(c) for c in [_call("a", PASS), _call("b", PASS)]) + "\n"
    )
    tampered = runner.invoke(
        app, ["toolschema", "conformance", "--capsule", str(capsule), "--verify", digest]
    )
    assert tampered.exit_code == 1
    assert "MISMATCH" in tampered.output


def test_verify_json_reports_the_recomputed_digest(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, calls=[_call("a", PASS)])
    result = runner.invoke(app, [
        "toolschema", "conformance", "--capsule", str(capsule),
        "--verify", "sha256:" + "0" * 64, "--json",
    ])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["verified"] is False
    assert payload["recomputed"].startswith("sha256:")


def test_a_capsule_with_no_tool_calls_exits_two(tmp_path: Path) -> None:
    """A digest over nothing is a constant — refused rather than sealed."""
    d = tmp_path / "empty-run"
    d.mkdir()
    d.joinpath("capsule.json").write_text(json.dumps({"run_id": "empty-run"}))
    result = runner.invoke(app, ["toolschema", "conformance", "--capsule", str(d)])
    assert result.exit_code == 2
    assert "no tool calls to seal" in result.output


def test_a_malformed_tool_call_line_exits_two(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, calls=[_call("a", PASS)])
    capsule.joinpath("tool-calls.jsonl").write_text("{not json\n")
    result = runner.invoke(app, ["toolschema", "conformance", "--capsule", str(capsule)])
    assert result.exit_code == 2


def test_a_missing_capsule_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["toolschema", "conformance", "--capsule", str(tmp_path / "nope")]
    )
    assert result.exit_code == 2


def test_help_smoke() -> None:
    result = runner.invoke(app, ["toolschema", "conformance", "--help"])
    assert result.exit_code == 0
    assert "seal" in result.output.lower()
