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

"""``nova audit map --capsule ID`` audits that one capsule, gaps included.

The report used to be computed over every capsule and then filtered to controls the
named capsule contributed evidence to. A control with *no* evidence lists no capsule,
so it was filtered out: it vanished from the table and the documented exit 1 never
fired, while the summary counts still described every capsule.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_PROFILE = """\
id: two-controls
name: Two controls
framework: In-house
version: "1"
controls:
  - id: C-LOG
    title: Calls are recorded
    description: Model calls are recorded.
    evidence_requirements:
      - evidence_type: model_calls
        description: model-calls.jsonl present
  - id: C-LINEAGE
    title: Lineage is recorded
    description: Lineage is recorded.
    evidence_requirements:
      - evidence_type: lineage
        description: lineage.jsonl present
"""


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    for name, files in {
        "only-calls": {"model-calls.jsonl": '{"m": 1}\n'},
        "calls-and-lineage": {"model-calls.jsonl": '{"m": 1}\n', "lineage.jsonl": '{"e": 1}\n'},
    }.items():
        cap = data / "capsules" / name
        cap.mkdir(parents=True)
        (cap / "capsule.yaml").write_text(f"run_id: {name}\n", encoding="utf-8")
        for fname, body in files.items():
            (cap / fname).write_text(body, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE, encoding="utf-8")
    return data, profile


def _run(data: Path, profile: Path, capsule: str) -> tuple[int, dict[str, object] | None, str]:
    result = runner.invoke(
        app,
        ["audit", "map", "--profile", str(profile), "--data-dir", str(data),
         "--capsule", capsule, "--json"],
        env={"COLUMNS": "300"},
    )
    assert isinstance(result.exception, SystemExit) or result.exception is None, result.exception
    try:
        doc = json.loads(result.output)
    except json.JSONDecodeError:
        doc = None
    return result.exit_code, doc, result.output


def test_a_control_the_capsule_lacks_is_reported_missing_and_fails_the_gate(tmp_path: Path) -> None:
    data, profile = _setup(tmp_path)
    code, doc, out = _run(data, profile, "only-calls")
    assert doc is not None, out
    status = {c["control_id"]: c["status"] for c in doc["coverages"]}  # type: ignore[index, union-attr]
    assert status == {"C-LOG": "covered", "C-LINEAGE": "missing"}
    assert code == 1


def test_the_summary_describes_only_the_named_capsule(tmp_path: Path) -> None:
    data, profile = _setup(tmp_path)
    _, doc, out = _run(data, profile, "only-calls")
    assert doc is not None, out
    assert doc["capsule_count"] == 1
    assert doc["missing_controls"] == 1


def test_a_capsule_with_every_control_covered_passes(tmp_path: Path) -> None:
    data, profile = _setup(tmp_path)
    code, doc, out = _run(data, profile, "calls-and-lineage")
    assert doc is not None, out
    assert {c["status"] for c in doc["coverages"]} == {"covered"}  # type: ignore[union-attr]
    assert code == 0


def test_an_unknown_capsule_id_is_an_error_not_an_empty_pass(tmp_path: Path) -> None:
    data, profile = _setup(tmp_path)
    code, _, out = _run(data, profile, "no-such-capsule")
    assert code != 0
    assert "no-such-capsule" in out


def test_a_capsule_id_cannot_escape_the_capsules_directory(tmp_path: Path) -> None:
    data, profile = _setup(tmp_path)
    code, _, _ = _run(data, profile, "../capsules/only-calls")
    assert code != 0
