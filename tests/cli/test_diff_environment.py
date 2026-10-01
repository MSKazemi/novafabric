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

"""``nova diff --environment`` / ``--group-by environment`` (ADR-0126 P2 remainder).

Both are read-only views over the typed ``deployment_environment`` a capsule
recorded at capture; nothing is inferred and no capsule is modified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(base: Path, run_id: str, environment: Any = None) -> Path:
    capsule = base / run_id
    capsule.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-10-01T10:00:00Z",
        "status": "success",
    }
    if environment is not None:
        manifest["deployment_environment"] = environment
        manifest["environment_source"] = "cli"
    (capsule / "capsule.yaml").write_text(yaml.dump(manifest))
    (capsule / "env.lock").write_text(yaml.dump({"python": {"version": "3.12.0"}}))
    for name in ("model-calls.jsonl", "tool-calls.jsonl"):
        (capsule / name).write_text("")
    return capsule


def _diff(*args: str) -> Any:
    return runner.invoke(app, ["diff", *args])


def _flat(output: str) -> str:
    """Collapse Rich's boxed, wrapped error panel into one searchable line."""
    return " ".join(output.replace("│", " ").split())


# --- --group-by environment ------------------------------------------------


def test_cross_environment_grouping_text(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "prod-1", "production")
    b = _make_capsule(tmp_path, "stg-1", "staging")
    result = _diff("--group-by", "environment", str(a), str(b))
    assert result.exit_code == 0, result.output
    assert "Environment groups (ADR-0126" in result.output
    assert "Cross-environment diff: production → staging" in result.output


def test_within_environment_grouping_text(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "prod-1", "production")
    b = _make_capsule(tmp_path, "prod-2", "production")
    result = _diff("--group-by", "environment", str(a), str(b))
    assert result.exit_code == 0, result.output
    assert "Within-environment diff (both capsules in group production)" in result.output


def test_grouping_json_shape(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "prod-1", "production")
    b = _make_capsule(tmp_path, "legacy", None)
    result = _diff("--group-by", "environment", "--output-format", "json", str(a), str(b))
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["environment_groups"] == {str(a): "production", str(b): "(no environment)"}
    assert doc["cross_environment"] is True
    assert "diff" in doc
    assert "variant_groups" not in doc


def test_invalid_recorded_value_groups_as_no_environment(tmp_path: Path) -> None:
    """A rule-violating recorded value is never surfaced as a group (record-only)."""
    a = _make_capsule(tmp_path, "bad", "prod env!")
    b = _make_capsule(tmp_path, "none", None)
    result = _diff("--group-by", "environment", "--output-format", "json", str(a), str(b))
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert set(doc["environment_groups"].values()) == {"(no environment)"}
    assert doc["cross_environment"] is False


def test_variant_grouping_unchanged(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "a", "production")
    b = _make_capsule(tmp_path, "b", "staging")
    result = _diff("--group-by", "variant", "--output-format", "json", str(a), str(b))
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "variant_groups" in doc and "environment_groups" not in doc


# --- --environment filter ----------------------------------------------------


def test_environment_filter_admits_matching_pair(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "prod-1", "production")
    b = _make_capsule(tmp_path, "prod-2", "production")
    result = _diff("--environment", "production", str(a), str(b))
    assert result.exit_code == 0, result.output
    assert "Environment filter: both capsules recorded production" in result.output


def test_environment_filter_json_carries_filter(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "prod-1", "production")
    b = _make_capsule(tmp_path, "prod-2", "production")
    result = _diff("--environment", "production", "--output-format", "json", str(a), str(b))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["environment_filter"] == "production"


@pytest.mark.parametrize("other", ["staging", None])
def test_environment_filter_excludes_mismatch_exit_2(tmp_path: Path, other: str | None) -> None:
    a = _make_capsule(tmp_path, "prod-1", "production")
    b = _make_capsule(tmp_path, "other", other)
    result = _diff("--environment", "production", str(a), str(b))
    assert result.exit_code == 2
    expected = repr(other) if other is not None else "no deployment_environment"
    assert expected in result.output
    assert "both capsules must have recorded it" in result.output


def test_environment_filter_is_case_sensitive(tmp_path: Path) -> None:
    """Values are recorded verbatim; the filter compares verbatim too."""
    a = _make_capsule(tmp_path, "p1", "Production")
    b = _make_capsule(tmp_path, "p2", "Production")
    assert _diff("--environment", "production", str(a), str(b)).exit_code == 2


def test_environment_filter_with_grouping(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "p1", "production")
    b = _make_capsule(tmp_path, "p2", "production")
    result = _diff(
        "--environment", "production", "--group-by", "environment",
        "--output-format", "json", str(a), str(b),
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["environment_filter"] == "production"
    assert doc["cross_environment"] is False


def test_invalid_filter_value_rejected(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "p1", "production")
    b = _make_capsule(tmp_path, "p2", "production")
    result = _diff("--environment", "prod env!", str(a), str(b))
    assert result.exit_code != 0
    assert "invalid environment" in _flat(result.output)


def test_environment_rejected_for_asset_refs() -> None:
    result = _diff("--environment", "production", "a@v1", "b@v1")
    assert result.exit_code != 0
    assert "capsule diffs only" in _flat(result.output)


@pytest.mark.parametrize("mode", ["--media", "--significance"])
def test_environment_rejected_with_other_modes(tmp_path: Path, mode: str) -> None:
    a = _make_capsule(tmp_path, "p1", "production")
    b = _make_capsule(tmp_path, "p2", "production")
    result = _diff(mode, "--environment", "production", str(a), str(b))
    assert result.exit_code != 0
    assert "--environment cannot be combined" in _flat(result.output)


def test_default_output_unchanged_without_flags(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "p1", "production")
    b = _make_capsule(tmp_path, "p2", "staging")
    plain = _diff("--output-format", "json", str(a), str(b))
    assert plain.exit_code == 0, plain.output
    doc = json.loads(plain.output)
    assert "environment_filter" not in doc and "environment_groups" not in doc


def test_read_only(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "p1", "production")
    b = _make_capsule(tmp_path, "p2", "staging")
    before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    _diff("--group-by", "environment", str(a), str(b))
    _diff("--environment", "production", str(a), str(b))
    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert before == after


def test_help_lists_environment_flag() -> None:
    from novafabric.cli.diff import GROUP_BY_DIMENSIONS

    assert GROUP_BY_DIMENSIONS == ("variant", "environment")
    result = runner.invoke(app, ["diff", "--help"], env={"NO_COLOR": "1", "COLUMNS": "200"})
    assert result.exit_code == 0
    assert "--environment" in result.output
