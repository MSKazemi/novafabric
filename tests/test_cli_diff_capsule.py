from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(tmp_path: Path, name: str, run_id: str = "RUNTEST") -> Path:
    cap = tmp_path / name
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()
    manifest = {"schema_version": "0.1.0", "run_id": run_id, "status": "success"}
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "env.lock").write_text(yaml.dump({
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "outputs" / "stdout.txt").write_text("output\n")
    return cap


def test_identical_capsules_no_changes(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    cap_b = _make_capsule(tmp_path, "b", "RUNB")
    result = runner.invoke(app, ["diff", str(cap_a), str(cap_b)])
    assert result.exit_code == 0
    assert "No differences found" in result.output


def test_diff_shows_changed_output(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    cap_b = _make_capsule(tmp_path, "b", "RUNB")
    (cap_b / "outputs" / "stdout.txt").write_text("different\n")
    result = runner.invoke(app, ["diff", str(cap_a), str(cap_b)])
    assert result.exit_code == 0
    assert "stdout.txt" in result.output or "output" in result.output.lower()


def test_assert_no_regressions_exits_1_on_change(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    cap_b = _make_capsule(tmp_path, "b", "RUNB")
    (cap_b / "outputs" / "stdout.txt").write_text("different\n")
    result = runner.invoke(app, ["diff", str(cap_a), str(cap_b), "--assert-no-regressions"])
    assert result.exit_code == 1


def test_assert_no_regressions_exits_0_when_same(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    cap_b = _make_capsule(tmp_path, "b", "RUNB")
    result = runner.invoke(app, ["diff", str(cap_a), str(cap_b), "--assert-no-regressions"])
    assert result.exit_code == 0


def test_json_output_format(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    cap_b = _make_capsule(tmp_path, "b", "RUNB")
    result = runner.invoke(app, ["diff", str(cap_a), str(cap_b), "--output-format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "run_a_id" in data
    assert "summary" in data


def test_github_annotation_format(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    cap_b = _make_capsule(tmp_path, "b", "RUNB")
    (cap_b / "outputs" / "stdout.txt").write_text("changed\n")
    result = runner.invoke(
        app, ["diff", str(cap_a), str(cap_b), "--output-format", "github-annotation"]
    )
    assert result.exit_code == 0
    assert "::" in result.output


def test_invalid_capsule_path_exits_1(tmp_path: Path) -> None:
    cap_a = _make_capsule(tmp_path, "a", "RUNA")
    result = runner.invoke(app, ["diff", str(cap_a), str(tmp_path / "no-such-dir")])
    assert result.exit_code == 1


def test_asset_diff_routing_preserved(tmp_path: Path) -> None:
    # Both args contain @: routes to asset diff, fails with registry error (not capsule error)
    result = runner.invoke(app, ["diff", "model@1.0.0", "model@2.0.0"])
    # Should fail with asset-not-found, not a capsule error
    assert result.exit_code != 0
    # The output should mention something registry-related, not "capsule"
    assert "capsule" not in result.output.lower()
