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
"""M3: without ``--data-dir``, ``nova audit`` reads the capsules ``nova capture`` writes.

The default was ``~/novafabric-data``, a directory capture never writes, so a default
``nova audit map/report/coverage/bundle`` scanned zero capsules. Capture writes to
``_paths.default_capsule_dir()``: ``$NOVAFABRIC_CAPSULE_DIR``, else
``$NOVAFABRIC_HOME/capsules``. An explicit ``--data-dir`` keeps its meaning (a root
holding ``capsules/``).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _capsule(capsules: Path, run_id: str) -> None:
    d = capsules / run_id
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text(f"run_id: {run_id}\nstatus: success\n")
    (d / "env.lock").write_text("python: 3.12\n")
    (d / "model-calls.jsonl").write_text(json.dumps({"model": "gpt-4"}) + "\n")
    (d / "lineage.jsonl").write_text(json.dumps({"edge_type": "contains"}) + "\n")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    # A decoy under the old default must not be what is scanned.
    _capsule(tmp_path / "user" / "novafabric-data" / "capsules", "decoy")
    return h


def _scan(*args: str) -> dict[str, object]:
    result = runner.invoke(app, ["audit", "map", "--json", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    payload: dict[str, object] = json.loads(result.stdout)
    return payload


def _capsule_ids(report: dict[str, object]) -> set[str]:
    coverages = report["coverages"]
    assert isinstance(coverages, list)
    return {cid for c in coverages for cid in c["capsule_ids"]}


def test_default_reads_nova_home_capsules(home: Path) -> None:
    _capsule(home / "capsules", "01CAPTURED")
    report = _scan()
    assert report["capsule_count"] == 1
    assert _capsule_ids(report) == {"01CAPTURED"}


def test_default_honours_novafabric_capsule_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture writes to NOVAFABRIC_CAPSULE_DIR when set, even if it is not named capsules/."""
    elsewhere = tmp_path / "store" / "runs"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(elsewhere))
    _capsule(elsewhere, "01ELSEWHERE")
    _capsule(home / "capsules", "01NOT-USED")
    report = _scan()
    assert report["capsule_count"] == 1
    assert _capsule_ids(report) == {"01ELSEWHERE"}


def test_explicit_data_dir_keeps_its_meaning(home: Path, tmp_path: Path) -> None:
    root = tmp_path / "explicit"
    _capsule(root / "capsules", "01EXPLICIT")
    report = _scan("--data-dir", str(root))
    assert report["capsule_count"] == 1


def test_report_uses_the_same_default(home: Path) -> None:
    _capsule(home / "capsules", "01CAPTURED")
    _capsule(home / "capsules", "01SECOND")
    result = runner.invoke(app, ["audit", "report", "--format", "text"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Capsules scanned: 2" in result.stdout


@pytest.mark.parametrize("command", ["map", "report", "bundle", "coverage"])
def test_every_scanning_command_defaults_to_none(command: str) -> None:
    """All four commands share _resolve_dirs; none may carry a literal default path."""
    from novafabric.cli import audit as audit_cli

    func = {"map": audit_cli.audit_map, "report": audit_cli.audit_report,
            "bundle": audit_cli.audit_bundle, "coverage": audit_cli.audit_coverage}[command]
    import inspect

    assert inspect.signature(func).parameters["data_dir"].default is None


def test_bundle_default_collects_from_the_capture_directory(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "store" / "runs"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(elsewhere))
    _capsule(elsewhere, "01ELSEWHERE")
    out = tmp_path / "bundle.zip"
    result = runner.invoke(app, ["audit", "bundle", "--output", str(out)])
    assert result.exit_code == 0, result.output
    names = zipfile.ZipFile(out).namelist()
    assert "audit-report.json" in names
    assert any(n.startswith("capsules/01ELSEWHERE/") for n in names), names
