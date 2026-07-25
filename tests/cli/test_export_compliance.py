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

from typer.testing import CliRunner

from novafabric.cli.export_compliance import export_compliance_app

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


def test_iso42001_from_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps([{"control_id": "ISO42-8.5-RESPONSIBLE-AI", "evidence_kind": "eval_gate"}]),
        encoding="utf-8",
    )
    out = tmp_path / "iso.json"
    result = runner.invoke(
        export_compliance_app,
        [
            "iso42001",
            "--catalog", str(catalog),
            "--evidence", "eval_gate",
            "--capsule-id", "cap-1",  # supplies the re-performable ref evidenced controls require
            "--out", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["controls"][0]["status"] == "evidenced"


def test_iso42001_without_capsule_id_degrades_to_not_evidenced(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps([{"control_id": "ISO42-8.5-RESPONSIBLE-AI", "evidence_kind": "eval_gate"}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        export_compliance_app,
        ["iso42001", "--catalog", str(catalog), "--evidence", "eval_gate"],
    )
    assert result.exit_code == 0, result.output
    # No re-performable ref → the exporter honestly refuses to claim "evidenced".
    assert '"not_evidenced"' in result.output


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
