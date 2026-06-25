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
"""``nova incident`` CLI smoke tests (ADR-0088)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.incident import incident_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.delenv("NOVAFABRIC_INCIDENTS_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)


def _open_incident() -> str:
    result = runner.invoke(
        incident_app,
        [
            "open",
            "--title",
            "Tool misuse",
            "--classification",
            "unauthorized_tool_use",
            "--severity",
            "high",
            "--occurred-at",
            "2026-06-10T08:00:00+00:00",
            "--aware-at",
            "2026-06-11T09:30:00+00:00",
            "--run-id",
            "run-abc",
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output.split()[2]


class TestIncidentCli:
    def test_open_list_status_round_trip_offline(self) -> None:
        incident_id = _open_incident()
        listed = runner.invoke(incident_app, ["list"])
        assert listed.exit_code == 0
        assert incident_id in listed.output
        status = runner.invoke(incident_app, ["status", incident_id])
        assert status.exit_code == 0
        assert "art73_2_standard" in status.output
        assert "not legal advice" in status.output

    def test_status_unknown_id_exits_nonzero(self) -> None:
        result = runner.invoke(incident_app, ["status", "inc-missing"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_export_aim_writes_json_and_transitions_to_reported(
        self, tmp_path: Path
    ) -> None:
        incident_id = _open_incident()
        out = tmp_path / "aim.json"
        result = runner.invoke(
            incident_app,
            ["export", incident_id, "--format", "aim", "--output", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert "open -> reported" in result.output
        payload = json.loads(out.read_text())
        assert payload["report_type"] == "oecd-aim"
        status = runner.invoke(incident_app, ["status", incident_id])
        assert "reported" in status.output

    def test_export_nis2_writes_valid_report(self, tmp_path: Path) -> None:
        incident_id = _open_incident()
        out = tmp_path / "nis2.json"
        result = runner.invoke(
            incident_app,
            ["export", incident_id, "--format", "nis2", "--output", str(out)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(out.read_text())
        assert payload["incident_id"] == incident_id
        assert payload["preliminary_classification"] == "unauthorized_tool_use"

    def test_open_rejects_aware_before_occurred(self) -> None:
        result = runner.invoke(
            incident_app,
            [
                "open",
                "--title",
                "x",
                "--classification",
                "unauthorized_tool_use",
                "--severity",
                "low",
                "--occurred-at",
                "2026-06-11T09:30:00+00:00",
                "--aware-at",
                "2026-06-10T08:00:00+00:00",
            ],
        )
        assert result.exit_code == 1
        assert "aware_at" in result.output
