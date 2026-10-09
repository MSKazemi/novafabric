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

"""A capsule a framework adapter wrote is found by its bare run id.

Reproduced on PyPI ``novafabric==0.104.0`` (2026-10-09): an adapter with no
``data_dir`` writes ``./.novafabric/runs/<run-id>/`` (or ``$NOVAFABRIC_HOME/runs/``),
while ``nova replay`` / ``nova diff`` / ``nova validate`` looked a bare id up only in
the configured capsule store (``$NOVAFABRIC_HOME/capsules`` by default) and failed
with "Captured runs live there — check the id". Only the path form worked.

The adapter default location is a documented contract (``docs/README.md``
"Default storage paths"), so it is kept; the run-id resolver searches the adapter
defaults as a bounded fallback instead. These tests drive the real adapter core and
the real CLI end to end.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.adapters._capsule import begin_capture
from novafabric.cli.main import app

runner = CliRunner()

_SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"


def _adapter_capsule(data_dir: Path | None = None) -> Path:
    cap = begin_capture(framework="haystack", run_name="demo", data_dir=data_dir)
    cap.finish()
    assert (cap.cap_dir / "capsule.yaml").is_file()
    return cap.cap_dir


def _invoke(command: str, run_id: str) -> list[str]:
    return {
        "replay": ["replay", "--mode", "forensic", run_id],
        "validate": ["validate", run_id],
        "diff": ["diff", run_id, run_id],
    }[command]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory as cwd, with the configured store somewhere else."""
    work = tmp_path / "project"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "store"))
    return work


@pytest.mark.parametrize("command", ["replay", "validate", "diff"])
def test_a_project_local_adapter_capsule_is_found_by_run_id(
    project: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.delenv("NOVAFABRIC_HOME", raising=False)
    cap_dir = _adapter_capsule()
    assert cap_dir.parent == project / ".novafabric" / "runs", "adapter default moved"

    result = runner.invoke(app, _invoke(command, cap_dir.name))

    assert result.exit_code == 0, result.output
    assert "No capsule found" not in result.output
    # It says where it found the capsule, on stderr — never silently.
    assert "found in" in result.stderr
    assert ".novafabric" in result.stderr and "runs" in result.stderr


@pytest.mark.parametrize("command", ["replay", "validate", "diff"])
def test_an_adapter_capsule_under_novafabric_home_runs_is_found_by_run_id(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """``$NOVAFABRIC_HOME/runs`` (adapter) is not ``$NOVAFABRIC_HOME/capsules`` (CLI)."""
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    home = tmp_path / "nova-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    cap_dir = _adapter_capsule()
    assert cap_dir.parent == home / "runs"

    result = runner.invoke(app, _invoke(command, cap_dir.name))

    assert result.exit_code == 0, result.output
    assert str(home / "runs") in result.stderr.replace("\n", "")


@pytest.mark.parametrize("command", ["replay", "validate", "diff"])
def test_an_id_present_in_two_stores_is_an_error_naming_both(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.delenv("NOVAFABRIC_HOME", raising=False)
    cap_dir = _adapter_capsule()
    store = tmp_path / "store"
    shutil.copytree(cap_dir, store / cap_dir.name)

    result = runner.invoke(app, _invoke(command, cap_dir.name))

    assert result.exit_code != 0
    flat = re.sub(r"\s+", "", result.output)
    assert "ambiguous" in result.output
    assert re.sub(r"\s+", "", str(store / cap_dir.name)) in flat
    assert re.sub(r"\s+", "", str(cap_dir)) in flat


def test_the_fallback_notice_never_pollutes_json_stdout(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_HOME", raising=False)
    cap_dir = _adapter_capsule()

    result = runner.invoke(
        app, ["diff", cap_dir.name, cap_dir.name, "--output-format", "json"]
    )

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert "found in" in result.stderr


def test_an_explicit_data_dir_is_unchanged(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "nova-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    explicit = tmp_path / "explicit"

    cap_dir = _adapter_capsule(data_dir=explicit)

    assert cap_dir.parent == explicit
    assert not (home / "runs").exists()
    assert not (project / ".novafabric").exists()


def test_novafabric_home_still_decides_where_an_adapter_writes(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer precedence is unchanged: explicit > $NOVAFABRIC_HOME/runs > ./.novafabric/runs."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))
    assert _adapter_capsule().parent == tmp_path / "nova-home" / "runs"

    monkeypatch.delenv("NOVAFABRIC_HOME")
    assert _adapter_capsule().parent == project / ".novafabric" / "runs"


def test_no_adapter_restates_the_default_location() -> None:
    """Every adapter takes its default from ``_paths.adapter_default_runs_dir``.

    Nine adapters each carried their own copy of the expression; the resolver's
    fallback list is only correct while every writer shares one definition.
    """
    offenders = [
        p.name
        for p in sorted((_SRC / "adapters").glob("*.py"))
        if '"NOVAFABRIC_HOME"' in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"restate NOVAFABRIC_HOME instead of the helper: {offenders}"
