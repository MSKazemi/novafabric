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
"""ADR-0146 P2 / NF-142 — the ``nova cost rollup`` CLI (report-only).

Exit 0 whenever a report renders (findings included); 2 on missing, oversized
or malformed input. Never writes to the capsule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import cost_rollup as cli_mod
from novafabric.cli.main import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cost-rollup"
DELEG = str(FIXTURES / "delegation_valid.json")
ATT = str(FIXTURES / "attribution_valid.json")


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "console", Console(width=200))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _capsule(tmp_path: Path, name: str = "run-1") -> Path:
    capsule = tmp_path / name
    capsule.mkdir()
    doc = json.loads(Path(ATT).read_text())
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": name, **doc}))
    return capsule


def test_json_report_from_file() -> None:
    result = runner.invoke(app, ["cost", "rollup", DELEG, ATT, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["basis"] == "measured"
    assert payload["conservation"]["ok"] is True
    assert payload["conservation"]["root_subtree_cost"] == "4.12"
    assert payload["hops"][1]["agent_id"] == "planner"
    assert payload["hops"][1]["subtree_cost"] == "4.12"


def test_table_output() -> None:
    result = runner.invoke(app, ["cost", "rollup", DELEG, ATT])
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert "basis: measured" in out
    assert "planner self=1.21 USD subtree=4.12 USD grantees=[executor, retriever]" in out
    assert "ok=True" in out
    assert "Record-only" in out


def test_capsule_dir_is_read_and_not_modified(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    before = (capsule / "capsule.yaml").read_bytes()
    result = runner.invoke(app, ["cost", "rollup", DELEG, str(capsule), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["conservation"]["ok"] is True
    assert (capsule / "capsule.yaml").read_bytes() == before
    assert sorted(p.name for p in capsule.iterdir()) == ["capsule.yaml"]


def test_run_id_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _capsule(tmp_path, "01RUNID")
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    result = runner.invoke(app, ["cost", "rollup", DELEG, "01RUNID", "--json"])
    assert result.exit_code == 0, result.output


def test_yaml_attribution_file(tmp_path: Path) -> None:
    path = tmp_path / "att.yaml"
    path.write_text(yaml.safe_dump(json.loads(Path(ATT).read_text())))
    result = runner.invoke(app, ["cost", "rollup", DELEG, str(path), "--json"])
    assert result.exit_code == 0, result.output


def test_cycle_reports_finding_exit_zero() -> None:
    deleg = str(FIXTURES / "delegation_cycle_invalid.json")
    result = runner.invoke(app, ["cost", "rollup", deleg, ATT])
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert "basis: partial" in out
    assert "finding cycle" in out


def test_broken_chain_json() -> None:
    deleg = str(FIXTURES / "delegation_broken_chain_invalid.json")
    result = runner.invoke(app, ["cost", "rollup", deleg, ATT, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [f["code"] for f in payload["findings"]] == ["broken_linkage", "unchained_agent"]
    assert payload["conservation"]["unchained_cost"] == "2.91"


def test_missing_delegation_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["cost", "rollup", str(tmp_path / "nope.json"), ATT])
    assert result.exit_code == 2


def test_unknown_capsule_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    result = runner.invoke(app, ["cost", "rollup", DELEG, "no-such-run"])
    assert result.exit_code == 2


def test_malformed_json_exits_two(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    result = runner.invoke(app, ["cost", "rollup", str(bad), ATT])
    assert result.exit_code == 2


def test_invalid_attribution_exits_two() -> None:
    att = str(FIXTURES / "attribution_invalid.json")
    result = runner.invoke(app, ["cost", "rollup", DELEG, att])
    assert result.exit_code == 2
    assert "RollupInputError" in _squash(result.output)


def test_oversize_input_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "MAX_INPUT_BYTES", 10)
    result = runner.invoke(app, ["cost", "rollup", DELEG, ATT])
    assert result.exit_code == 2


def test_capsule_without_manifest_exits_two(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "capsule.yaml").unlink()
    (capsule / "capsule.yaml").mkdir()  # exists but unreadable as a file
    result = runner.invoke(app, ["cost", "rollup", DELEG, str(capsule)])
    assert result.exit_code == 2


def test_help() -> None:
    result = runner.invoke(app, ["cost", "rollup", "--help"])
    assert result.exit_code == 0
    assert "report-only" in _squash(result.output).lower()


def test_read_doc_missing_file_exits_two(tmp_path: Path) -> None:
    import typer

    with pytest.raises(typer.Exit) as info:
        cli_mod._read_doc(tmp_path / "gone.json")
    assert info.value.exit_code == 2


def test_unchained_rendered_in_table() -> None:
    deleg = str(FIXTURES / "delegation_broken_chain_invalid.json")
    result = runner.invoke(app, ["cost", "rollup", deleg, ATT])
    assert result.exit_code == 0, result.output
    assert "(unchained) executor self=1.48 USD" in _squash(result.output)
