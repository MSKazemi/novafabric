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

"""CLI tests for ``nova diff --group-by variant`` (ADR-0116 P3, read-only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(base: Path, run_id: str, variant: dict[str, Any] | None) -> Path:
    capsule = base / run_id
    capsule.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-07-15T10:00:00Z",
        "status": "success",
    }
    if variant is not None:
        manifest["variant"] = variant
    (capsule / "capsule.yaml").write_text(yaml.dump(manifest))
    (capsule / "env.lock").write_text(yaml.dump({"python": {"version": "3.12.0"}}))
    for name in ("model-calls.jsonl", "tool-calls.jsonl"):
        (capsule / name).write_text("")
    return capsule


def _arm(variant_id: str) -> dict[str, Any]:
    return {
        "experiment_id": "exp-1",
        "variant_id": variant_id,
        "assignment_source": "statsig",
    }


def test_cross_arm_grouping_text(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "arm-a", _arm("control"))
    b = _make_capsule(tmp_path, "arm-b", _arm("treatment"))
    result = runner.invoke(app, ["diff", "--group-by", "variant", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "Variant groups" in result.output
    assert "exp-1/control" in result.output
    assert "exp-1/treatment" in result.output
    assert "Cross-arm diff" in result.output


def test_within_arm_grouping_text(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "run-1", _arm("control"))
    b = _make_capsule(tmp_path, "run-2", _arm("control"))
    result = runner.invoke(app, ["diff", "--group-by", "variant", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "Within-arm diff" in result.output


def test_capsule_without_block_groups_as_no_variant(tmp_path: Path) -> None:
    """Absence changes nothing: an unattributed capsule still groups (as '(no variant)')."""
    a = _make_capsule(tmp_path, "run-1", _arm("control"))
    b = _make_capsule(tmp_path, "run-2", None)
    result = runner.invoke(app, ["diff", "--group-by", "variant", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "(no variant)" in result.output


def test_malformed_block_groups_as_no_variant(tmp_path: Path) -> None:
    """A malformed recorded block is never repaired or guessed — '(no variant)'."""
    a = _make_capsule(tmp_path, "run-1", {"experiment_id": 42, "variant_id": ["x"]})
    b = _make_capsule(tmp_path, "run-2", _arm("control"))
    result = runner.invoke(app, ["diff", "--group-by", "variant", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "(no variant)" in result.output


def test_unreadable_manifest_groups_as_no_variant(tmp_path: Path) -> None:
    from novafabric.cli.diff import _NO_VARIANT_GROUP, _variant_group

    capsule = tmp_path / "broken"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("{unclosed: [")
    assert _variant_group(capsule) == _NO_VARIANT_GROUP


def test_json_output_carries_groups_and_diff(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "arm-a", _arm("control"))
    b = _make_capsule(tmp_path, "arm-b", _arm("treatment"))
    result = runner.invoke(
        app, ["diff", "--group-by", "variant", "--output-format", "json", str(a), str(b)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["variant_groups"] == {str(a): "exp-1/control", str(b): "exp-1/treatment"}
    assert payload["cross_arm"] is True
    assert payload["diff"]["run_a_id"] == "arm-a"


def test_grouping_never_mutates_the_capsules(tmp_path: Path) -> None:
    """Read-only invariant: --group-by variant reads recorded facts, writes nothing."""
    a = _make_capsule(tmp_path, "arm-a", _arm("control"))
    b = _make_capsule(tmp_path, "arm-b", _arm("treatment"))
    before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    runner.invoke(app, ["diff", "--group-by", "variant", str(a), str(b)])
    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert before == after


def test_unsupported_dimension_rejected(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "run-1", None)
    b = _make_capsule(tmp_path, "run-2", None)
    result = runner.invoke(app, ["diff", "--group-by", "model", str(a), str(b)])
    assert result.exit_code != 0
    assert "only 'variant'" in result.output


def test_group_by_rejected_for_asset_refs() -> None:
    result = runner.invoke(app, ["diff", "--group-by", "variant", "a@v1", "b@v1"])
    assert result.exit_code != 0
    assert "capsule diffs only" in result.output


def test_group_by_rejected_for_github_annotation(tmp_path: Path) -> None:
    a = _make_capsule(tmp_path, "run-1", None)
    b = _make_capsule(tmp_path, "run-2", None)
    result = runner.invoke(
        app,
        ["diff", "--group-by", "variant", "--output-format", "github-annotation",
         str(a), str(b)],
    )
    assert result.exit_code != 0
    assert "github-annotation" in result.output


def test_diff_without_group_by_unchanged(tmp_path: Path) -> None:
    """No --group-by ⇒ exactly today's output (the flag is opt-in, additive)."""
    a = _make_capsule(tmp_path, "run-1", _arm("control"))
    b = _make_capsule(tmp_path, "run-2", _arm("treatment"))
    result = runner.invoke(app, ["diff", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "Variant groups" not in result.output
