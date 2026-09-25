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

"""``nova safety control show`` / ``nova safety tripwire list`` (ADR-0167 P2).

Exit-code contract: 0 whenever the capsule was read — *including* when a
tripwire fired (reporting is not gating, I-1) — and 2 on input errors. Every
output carries the in-mission-boundary line (spec §3.16).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.frontier_safety import (
    FACET_NAME,
    IN_MISSION_BOUNDARY,
    ControlDecision,
    TripwireTrigger,
    attach_facet,
    build_facet,
    digest_ref,
)

runner = CliRunner()

ACTION_A = digest_ref("action-a")
ACTION_B = digest_ref("action-b")


def _decision(action: str, decision: str = "resample") -> ControlDecision:
    return ControlDecision(
        protocol="resample",
        decision=decision,  # type: ignore[arg-type]
        governed_action_ref=action,
        monitor_ref=digest_ref("monitor"),
        verdict_ref=digest_ref(f"log-{action}-{decision}"),
    )


def _trigger(indicator: str, framework: str) -> TripwireTrigger:
    return TripwireTrigger(
        indicator_id=indicator,
        framework=framework,  # type: ignore[arg-type]
        commitment_ref=digest_ref("commitment"),
        triggered_by_ref=digest_ref("observation"),
    )


def _write_capsule(root: Path, manifest: Any, name: str = "run-01") -> Path:
    capsule_dir = root / name
    capsule_dir.mkdir(parents=True)
    text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest)
    (capsule_dir / "capsule.yaml").write_text(text, encoding="utf-8")
    return capsule_dir


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    facet = build_facet(
        control_decisions=[
            _decision(ACTION_A, "resample"),
            _decision(ACTION_B, "defer"),
            _decision(ACTION_A, "accept"),
        ],
        tripwire_triggers=[
            _trigger("fsf.ccl.cyber.autonomous-uplift", "deepmind_fsf"),
            _trigger("asl-4.autonomy", "anthropic_rsp"),
        ],
    )
    manifest = attach_facet({"run_id": "run-01", "status": "completed"}, facet)
    return _write_capsule(tmp_path, json.loads(json.dumps(manifest)))


@pytest.fixture
def empty(tmp_path: Path) -> Path:
    return _write_capsule(tmp_path, {"run_id": "run-02", "status": "completed"}, "run-02")


def _boundary_in(output: str) -> bool:
    return " ".join(IN_MISSION_BOUNDARY.split()) in " ".join(output.split())


# ── --help smoke ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["safety", "--help"],
        ["safety", "control", "show", "--help"],
        ["safety", "tripwire", "list", "--help"],
    ],
)
def test_help(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


def test_safety_group_is_distinct_from_safety_case() -> None:
    result = runner.invoke(app, ["--help"])
    assert "safety-case" in result.output
    assert "safety " in result.output or "│ safety" in result.output


# ── control show ──────────────────────────────────────────────────────────


def test_control_show_text(populated: Path) -> None:
    result = runner.invoke(app, ["safety", "control", "show", "--capsule", str(populated)])
    assert result.exit_code == 0, result.output
    assert _boundary_in(result.output)
    assert "3 control-protocol decision(s)" in result.output
    assert "defer" in result.output


def test_control_show_json_and_action_filter(populated: Path) -> None:
    result = runner.invoke(
        app,
        ["safety", "control", "show", "--capsule", str(populated), "--action", ACTION_A, "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["boundary"] == IN_MISSION_BOUNDARY
    decisions = data["control_decisions"]
    assert [d["decision"] for d in decisions] == ["resample", "accept"]
    assert all(d["governed_action_ref"] == ACTION_A for d in decisions)
    assert all(d["verdict_source"] == "control_protocol" for d in decisions)


def test_control_show_with_no_facet_is_not_an_error(empty: Path) -> None:
    result = runner.invoke(app, ["safety", "control", "show", "--capsule", str(empty)])
    assert result.exit_code == 0, result.output
    assert _boundary_in(result.output)
    assert "neither safe nor unsafe" in " ".join(result.output.split())
    as_json = runner.invoke(app, ["safety", "control", "show", "--capsule", str(empty), "--json"])
    assert json.loads(as_json.output)["control_decisions"] == []


def test_control_show_resolves_a_bare_run_id(
    populated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(populated.parent))
    result = runner.invoke(app, ["safety", "control", "show", "--capsule", "run-01", "--json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["control_decisions"]) == 3


# ── tripwire list ─────────────────────────────────────────────────────────


def test_fired_tripwires_exit_zero(populated: Path) -> None:
    """No-block: a fired indicator is reported, never turned into a gate."""
    result = runner.invoke(app, ["safety", "tripwire", "list", "--capsule", str(populated)])
    assert result.exit_code == 0, result.output
    assert _boundary_in(result.output)
    assert "2 tripwire trigger(s) fired" in result.output


def test_tripwire_list_json_and_framework_filter(populated: Path) -> None:
    result = runner.invoke(
        app,
        [
            "safety",
            "tripwire",
            "list",
            "--capsule",
            str(populated),
            "--framework",
            "deepmind_fsf",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["boundary"] == IN_MISSION_BOUNDARY
    assert [t["indicator_id"] for t in data["tripwire_triggers"]] == [
        "fsf.ccl.cyber.autonomous-uplift"
    ]
    assert data["tripwire_triggers"][0]["fired"] is True


def test_tripwire_list_with_no_facet(empty: Path) -> None:
    result = runner.invoke(app, ["safety", "tripwire", "list", "--capsule", str(empty)])
    assert result.exit_code == 0, result.output
    assert "No tripwire triggers recorded." in result.output


# ── Input errors → exit 2, boundary line still printed ────────────────────


@pytest.mark.parametrize("sub", [["control", "show"], ["tripwire", "list"]])
def test_missing_capsule_exits_2(tmp_path: Path, sub: list[str]) -> None:
    result = runner.invoke(app, ["safety", *sub, "--capsule", str(tmp_path / "nope" / "x")])
    assert result.exit_code == 2
    assert _boundary_in(result.output)


@pytest.mark.parametrize(
    "manifest",
    [
        "run_id: [unclosed\n",  # YAML error
        "- just\n- a list\n",  # not a mapping
        {"facets": {FACET_NAME: {"control_decisions": [{"protocol": "resample"}]}}},
        {
            "facets": {
                FACET_NAME: {
                    "tripwire_trigger": {
                        "indicator_id": "x",
                        "framework": "other",
                        "commitment_ref": digest_ref("c"),
                        "triggered_by_ref": digest_ref("t"),
                        "verdict": "unsafe",
                    }
                }
            }
        },
    ],
)
def test_unreadable_or_malformed_capsule_exits_2(tmp_path: Path, manifest: Any) -> None:
    capsule_dir = _write_capsule(tmp_path, manifest)
    result = runner.invoke(app, ["safety", "control", "show", "--capsule", str(capsule_dir)])
    assert result.exit_code == 2, result.output
    assert _boundary_in(result.output)
