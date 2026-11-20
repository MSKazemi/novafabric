# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""CLI smoke tests for the ``nova export-compliance`` cohort.

Thin wrappers over the shipped compliance exporters (ADR-0107). Each subcommand reads
its input (a capsule directory or a JSON file) and writes a report JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from novafabric.cli.export_compliance import export_compliance_app
from novafabric.compliance.export.provenance import build_capsule_ref

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: r1\nstatus: completed\n", encoding="utf-8")
    return cap


def test_genai_profile_from_capsule(tmp_path: Path) -> None:
    out = tmp_path / "profile.json"
    result = runner.invoke(
        export_compliance_app,
        ["genai-profile", str(_capsule(tmp_path)), "--evidence", "tool_permissions", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["base_run_id"] == "r1"
    assert len(doc["mappings"]) == 10


def _catalog(tmp_path: Path, *ids: str) -> Path:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps([{"control_id": i, "evidence_kind": "eval_gate"} for i in ids]),
        encoding="utf-8",
    )
    return catalog


def _iso(tmp_path: Path, *args: str) -> tuple[Any, Path]:
    out = tmp_path / "iso.json"
    result = runner.invoke(export_compliance_app, ["iso42001", *args, "--out", str(out)])
    return result, out


def test_iso42001_binds_evidenced_controls_to_the_real_capsule_digest(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "ISO42-8.4-AI-SYSTEM-IMPACT")),
        "--evidence", "eval_gate", "--capsule", str(cap),
    )
    assert result.exit_code == 0, result.output
    ctrl = json.loads(out.read_text())["controls"][0]
    assert ctrl["status"] == "evidenced"
    expected = build_capsule_ref(cap, "unused")
    assert expected is not None
    assert ctrl["evidence_ref"]["content_digest"] == expected.content_digest
    assert ctrl["evidence_ref"]["content_digest"] != "sha256:" + "0" * 64
    assert ctrl["evidence_ref"]["capsule_id"] == "r1"


def test_iso42001_unsealed_capsule_carries_no_seal_path(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(_capsule(tmp_path)),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["controls"][0]["evidence_ref"]["seal_envelope_path"] is None


def test_iso42001_capsule_id_alone_fails_closed(tmp_path: Path) -> None:
    # The pre-fix call shape: it attached an all-zero digest to a capsule nobody read.
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule-id", "run-123",
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "--capsule" in result.output
    assert not out.exists()


def test_iso42001_capsule_id_must_match_the_manifest(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")), "--evidence", "eval_gate",
        "--capsule", str(_capsule(tmp_path)), "--capsule-id", "someone-else",
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_directory_without_manifest_exit_2(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(empty),
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_symlinked_manifest_refused(tmp_path: Path) -> None:
    real = _capsule(tmp_path)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "capsule.yaml").symlink_to(real / "capsule.yaml")
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(linked),
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_duplicate_control_id_exit_2(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1", "C-1")),
        "--evidence", "eval_gate", "--capsule", str(_capsule(tmp_path)),
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_entry_without_control_id_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"evidence_kind": "eval_gate"}]), encoding="utf-8")
    result, out = _iso(tmp_path, "--catalog", str(catalog))
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_standard_label(tmp_path: Path) -> None:
    cat = _catalog(tmp_path, "C-1")
    result, out = _iso(tmp_path, "--catalog", str(cat), "--standard", "AIUC-1 (user catalog)")
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["standard"] == "AIUC-1 (user catalog)"
    result, out = _iso(tmp_path, "--catalog", str(cat))
    assert json.loads(out.read_text())["standard"] == "ISO/IEC 42001:2023"


def test_iso42001_without_capsule_degrades_to_not_evidenced(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "ISO42-8.4-AI-SYSTEM-IMPACT")),
        "--evidence", "eval_gate",
    )
    assert result.exit_code == 0, result.output
    # No capsule → the exporter honestly refuses to claim "evidenced".
    ctrl = json.loads(out.read_text())["controls"][0]
    assert ctrl["status"] == "not_evidenced"
    assert ctrl["evidence_ref"] is None


def test_gpai53_from_fields(tmp_path: Path) -> None:
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"general_description": "A GPAI model."}), encoding="utf-8")
    out = tmp_path / "form.json"
    result = runner.invoke(
        export_compliance_app,
        ["gpai53", "--model", "gpai-x", "--fields", str(fields), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["model_name"] == "gpai-x"
    assert len(doc["revisions"]) == 1
    assert doc["revisions"][0]["content_digest"].startswith("sha256:")


def test_pmm_from_findings(tmp_path: Path) -> None:
    findings = tmp_path / "findings.json"
    findings.write_text(
        json.dumps(
            [
                {
                    "metric": "hallucination_rate",
                    "trend": "degrading",
                    "severity": "high",
                    "description": "rising",
                    "incident_classification": "widespread_infringement",
                }
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "pmm.json"
    result = runner.invoke(
        export_compliance_app,
        [
            "pmm",
            "--system", "triage-agent",
            "--findings", str(findings),
            "--period-start", "2026-06-01",
            "--period-end", "2026-07-01",
            "--occurred-at", "2026-07-01",
            "--out", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert len(doc["referred_incidents"]) == 1


def test_pmm_serious_finding_without_classification_errors(tmp_path: Path) -> None:
    findings = tmp_path / "findings.json"
    findings.write_text(
        json.dumps([{"metric": "m", "trend": "degrading", "severity": "critical", "description": "d"}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        export_compliance_app,
        ["pmm", "--system", "s", "--findings", str(findings), "--occurred-at", "2026-07-01"],
    )
    assert result.exit_code != 0


def test_stdout_when_no_out(tmp_path: Path) -> None:
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"general_description": "A GPAI model."}), encoding="utf-8")
    result = runner.invoke(
        export_compliance_app,
        ["gpai53", "--model", "gpai-x", "--fields", str(fields)],
    )
    assert result.exit_code == 0, result.output
    assert "revisions" in result.output
