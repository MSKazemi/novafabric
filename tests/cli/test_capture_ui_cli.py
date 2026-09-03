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

"""``nova capture-ui`` — the NF-166/167 CLI surface (ADR-0148 D3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.ui import UiRecorder, attach_facet
from novafabric.cli.capture_ui import HONESTY_LINE

runner = CliRunner()

A_SECRET = "nvfk_abcd1234_" + "x" * 40


def run(*args: str) -> Any:
    from novafabric.cli.main import app

    return runner.invoke(app, ["capture-ui", *args])


def a_capsule(tmp_path: Path, *, store_bytes: bool = False) -> Path:
    """A capsule carrying a navigate → click → type run plus two observations."""
    capsule = tmp_path / "run_1"
    (capsule / "outputs").mkdir(parents=True)
    rec = UiRecorder(capture_raw=store_bytes)
    rec.record_action("navigate", url="https://example.test/cart")
    rec.record_action("click", target_ref="css:button#checkout", coords=(812, 344))
    rec.record_action("type", target_ref="css:input#coupon", text="SAVE20")
    rec.record_action("type", target_ref="css:input#password", text=A_SECRET)

    shot = b"\x89PNG\r\n\x1a\nshot"
    if store_bytes:
        (capsule / "outputs" / "shot.png").write_bytes(shot)
    rec.record_observation("screenshot", shot, blob_ref="outputs/shot.png")
    rec.record_observation("dom_snapshot", b"<html><body>cart</body></html>")

    manifest: dict[str, Any] = {"run_id": "run_1"}
    attach_facet(manifest, rec.actions_facet())
    attach_facet(manifest, rec.observations_facet())
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return capsule


@pytest.mark.parametrize(
    "args", [("--help",), ("show", "--help"), ("verify", "--help")]
)
def test_help_is_reachable(args: tuple[str, ...]) -> None:
    assert run(*args).exit_code == 0


def test_show_lists_actions_and_observations(tmp_path: Path) -> None:
    result = run("show", "--capsule", str(a_capsule(tmp_path)))
    assert result.exit_code == 0, result.output
    assert "ui_actions v0.1.0" in result.output
    assert "navigate" in result.output
    assert "ui_observations v0.1.0" in result.output
    assert "dom_snapshot" in result.output


def test_show_never_prints_the_typed_secret(tmp_path: Path) -> None:
    """The CLI is a disclosure surface too — a facet that redacts and a view that
    prints would leak just as completely."""
    result = run("show", "--capsule", str(a_capsule(tmp_path)))
    assert result.exit_code == 0
    assert A_SECRET not in result.output
    assert "nvfk_" not in result.output
    assert "redacted" in result.output


def test_show_states_the_residual_risk_when_a_digest_is_present(tmp_path: Path) -> None:
    """A reader who sees a digest must also see what it does not protect against."""
    result = run("show", "--capsule", str(a_capsule(tmp_path)))
    assert "not encryption" in result.output


def test_show_json_is_machine_readable_and_carries_the_honesty_line(
    tmp_path: Path,
) -> None:
    result = run("show", "--capsule", str(a_capsule(tmp_path)), "--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["honesty"] == HONESTY_LINE
    assert len(body["ui_actions"]["actions"]) == 4
    assert body["ui_observations"]["observations"][1]["kind"] == "dom_snapshot"
    assert A_SECRET not in result.output


def test_show_on_a_capsule_without_the_facets_exits_zero(tmp_path: Path) -> None:
    capsule = tmp_path / "bare"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: bare\n")
    result = run("show", "--capsule", str(capsule))
    assert result.exit_code == 0
    assert "No computer-use evidence" in result.output


def test_show_json_stays_json_when_there_are_no_facets(tmp_path: Path) -> None:
    capsule = tmp_path / "bare"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: bare\n")
    body = json.loads(run("show", "--capsule", str(capsule), "--json").output)
    assert body["ui_actions"] is None
    assert body["ui_observations"] is None


def test_verify_reports_reference_only_observations_as_fine(tmp_path: Path) -> None:
    """Reference-metadata-only is the documented default, not a fault."""
    result = run("verify", "--capsule", str(a_capsule(tmp_path)))
    assert result.exit_code == 0, result.output
    assert "0 unresolved" in result.output
    assert "not a fault" in result.output


def test_verify_detects_a_tampered_stored_observation(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, store_bytes=True)
    (capsule / "outputs" / "shot.png").write_bytes(b"tampered")
    result = run("verify", "--capsule", str(capsule))
    assert result.exit_code == 0, "reporting a tamper is the job succeeding"
    assert "unresolved" in result.output
    assert "1 unresolved" in result.output


def test_verify_strict_is_the_opt_in_gate(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, store_bytes=True)
    assert run("verify", "--capsule", str(capsule), "--strict").exit_code == 0
    (capsule / "outputs" / "shot.png").write_bytes(b"tampered")
    assert run("verify", "--capsule", str(capsule), "--strict").exit_code == 1


def test_verify_json_strict_still_exits_one(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, store_bytes=True)
    (capsule / "outputs" / "shot.png").write_bytes(b"tampered")
    result = run("verify", "--capsule", str(capsule), "--strict", "--json")
    assert result.exit_code == 1
    assert len(json.loads(result.output)["unresolved"]) == 1


def test_verify_without_the_facet_exits_zero(tmp_path: Path) -> None:
    capsule = tmp_path / "bare"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: bare\n")
    result = run("verify", "--capsule", str(capsule))
    assert result.exit_code == 0
    assert "No ui_observations facet" in result.output


@pytest.mark.parametrize("cmd", ["show", "verify"])
def test_a_missing_capsule_is_a_usage_error(tmp_path: Path, cmd: str) -> None:
    result = run(cmd, "--capsule", str(tmp_path / "nope"))
    assert result.exit_code == 2
    assert "Capsule directory not found" in result.output


def test_a_capsule_without_a_manifest_is_a_usage_error(tmp_path: Path) -> None:
    capsule = tmp_path / "empty"
    capsule.mkdir()
    result = run("show", "--capsule", str(capsule))
    assert result.exit_code == 2
    assert "capsule.yaml not found" in result.output


def test_an_unparseable_manifest_is_a_usage_error(tmp_path: Path) -> None:
    capsule = tmp_path / "broken"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: [unclosed\n")
    result = run("show", "--capsule", str(capsule))
    assert result.exit_code == 2
    assert "Could not read capsule.yaml" in result.output
