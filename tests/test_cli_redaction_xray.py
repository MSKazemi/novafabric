"""ADR-0174 (data slice) — the ``nova redaction-xray`` CLI.

Read-only. Loads a JSON document describing a capsule's field-protection state and prints the
per-field state overlay + a coverage meter + per-state counts. The load-bearing invariant is
that **no field value is ever printed** — the command surfaces paths and states only.
"""
from __future__ import annotations

import json
from pathlib import Path

from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "xray.json"
    p.write_text(json.dumps(doc))
    return p


def test_fields_list_reports_coverage_and_counts(tmp_path):
    doc = {"fields": [
        {"path": "p1", "state": "redacted"},
        {"path": "p2", "state": "secret_scrubbed"},
        {"path": "p3", "state": "clear"},
    ]}
    result = runner.invoke(app, ["redaction-xray", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "redacted" in result.output.lower()
    assert "coverage" in result.output.lower()


def test_raw_findings_are_adapted(tmp_path):
    doc = {"findings": [
        {"target_ref": "env.yaml SECRET_KEY", "redaction_strategy": "drop",
         "match_hash": "cafe", "replacement": ""},
    ]}
    result = runner.invoke(app, ["redaction-xray", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "SECRET_KEY" in result.output  # the path is shown
    assert "secret_scrubbed" in result.output.lower()


def test_values_are_never_printed(tmp_path):
    # a caller hands us a record carrying a raw secret value; it must not appear in output
    doc = {"fields": [
        {"path": "api_key", "state": "redacted", "value": "sk-live-DEADBEEF"},
    ]}
    result = runner.invoke(app, ["redaction-xray", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "sk-live-DEADBEEF" not in result.output


def test_json_output_is_machine_readable(tmp_path):
    doc = {"fields": [{"path": "p1", "state": "redacted"}]}
    result = runner.invoke(
        app, ["redaction-xray", str(_write(tmp_path, doc)), "--json", "--capsule-id", "run-7"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capsule_id"] == "run-7"
    assert payload["coverage"] == 1.0
    assert payload["fields"][0] == {"path": "p1", "state": "redacted"}


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["redaction-xray", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_json_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["redaction-xray", str(p)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# --capsule: read a capsule's redaction-proof.json directly
#
# Before this, all three trust surfaces took a hand-assembled JSON document and
# `--capsule-id` was only a LABEL stamped into the output — it selected nothing,
# which reads as though it picks a capsule. This closes that for the X-Ray.
# ---------------------------------------------------------------------------

def _capsule_with_proof(tmp_path: Path, proof: dict) -> Path:
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "redaction-proof.json").write_text(json.dumps(proof))
    return cap


def test_capsule_flag_reads_the_proof(tmp_path: Path) -> None:
    cap = _capsule_with_proof(
        tmp_path,
        {
            "capsule_run_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
            "findings": [
                {"target_ref": "inputs.prompt", "redaction_strategy": "mask"},
                {"target_ref": "env.API_KEY", "action_taken": "scrub"},
            ],
        },
    )
    result = runner.invoke(app, ["redaction-xray", "--capsule", str(cap), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert {f["path"] for f in report["fields"]} == {"inputs.prompt", "env.API_KEY"}


def test_capsule_id_comes_from_the_proof_not_the_directory(tmp_path: Path) -> None:
    """The proof is authoritative; the directory name is only a fallback.

    Regression: the id was read AFTER the document was narrowed to its
    findings, so it always fell back to the directory name. Invisible
    whenever the two agree — which they usually do.
    """
    cap = tmp_path / "renamed-on-copy"
    cap.mkdir()
    (cap / "redaction-proof.json").write_text(
        json.dumps({"capsule_run_id": "01HXTRUEIDK4P9DPBYK2WX000", "findings": []})
    )
    result = runner.invoke(app, ["redaction-xray", "--capsule", str(cap), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["capsule_id"] == "01HXTRUEIDK4P9DPBYK2WX000"


def test_zero_findings_is_a_result_not_an_error(tmp_path: Path) -> None:
    """A clean capsule genuinely has nothing to redact — report it as such."""
    cap = _capsule_with_proof(tmp_path, {"findings": []})
    result = runner.invoke(app, ["redaction-xray", "--capsule", str(cap), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["fields"] == []


def test_capsule_without_a_proof_explains_why(tmp_path: Path) -> None:
    cap = tmp_path / "no-proof"
    cap.mkdir()
    result = runner.invoke(app, ["redaction-xray", "--capsule", str(cap)])
    assert result.exit_code == 2
    assert "masking pipeline" in result.output


def test_document_and_capsule_are_mutually_exclusive(tmp_path: Path) -> None:
    cap = _capsule_with_proof(tmp_path, {"findings": []})
    doc = tmp_path / "d.json"
    doc.write_text(json.dumps({"fields": []}))
    result = runner.invoke(
        app, ["redaction-xray", str(doc), "--capsule", str(cap)]
    )
    assert result.exit_code == 2
    assert "not both" in result.output


def test_neither_document_nor_capsule_is_a_clear_error() -> None:
    result = runner.invoke(app, ["redaction-xray"])
    assert result.exit_code == 2
    assert_flag_in_help(result, "--capsule")
