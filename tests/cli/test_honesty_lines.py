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

"""Guard: record-only commands must carry their honesty line in EVERY output.

Several specs make this normative — NF-221-230 ("every `verify`/`prove` CLI
output **MUST** carry the evaluation-integrity honesty line") and NF-231-240
("every exported artifact **MUST** carry the compliance-honesty line").

The 2026-07-20 audit found the requirement unimplemented and, more to the
point, **unguarded**: `nova forensics timeline` printed no line at all,
`nova eval cost` had one only in its module docstring, and no test anywhere
asserted the property. A normative MUST that nothing checks is a comment.

The `--json` case is the one that matters most and was missing in both. A
banner the terminal shows but the payload omits is absent exactly where the
artifact travels furthest from the person who ran it — piped into a report,
attached to a filing, read by a tool. So the line lives on the record *model*,
not in a `console.print`, and these tests check both surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.eval.integrity.cost import HONESTY_LINE as EVAL_COST_LINE
from novafabric.forensics.timeline import HONESTY_LINE as FORENSICS_LINE

runner = CliRunner()


def _flat(text: str) -> str:
    """Collapse whitespace before matching.

    Rich wraps to terminal width, so the banner arrives split across lines.
    Asserting on the raw output would make this test fail on a narrow
    terminal while the code is correct — the trap that cost real time
    elsewhere in this repo.
    """
    return " ".join(text.split())


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def eval_cost_doc(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "cost.json",
        {"wall_seconds": 12.5, "token_in": 100, "token_out": 50, "usd_cost": 0.42},
    )


@pytest.fixture
def incident_doc(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "incident.json",
        {
            "incident_id": "inc-1",
            "events": [
                {
                    "ts": "2026-07-20T10:00:00Z",
                    "source_capsule": "cap-a",
                    "seq": 1,
                    "kind": "run",
                }
            ],
        },
    )


# ── nova eval cost (NF-221-230) ───────────────────────────────────────────


def test_eval_cost_text_output_carries_the_line(eval_cost_doc: Path) -> None:
    res = runner.invoke(app, ["eval", "cost", str(eval_cost_doc)])
    assert res.exit_code == 0, res.output
    assert _flat(EVAL_COST_LINE) in _flat(res.output)


def test_eval_cost_json_output_carries_the_line(eval_cost_doc: Path) -> None:
    """The case the audit found missing: --json had no honesty line at all."""
    res = runner.invoke(app, ["eval", "cost", str(eval_cost_doc), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["honesty_line"] == EVAL_COST_LINE


# ── nova forensics timeline (NF-231-240) ──────────────────────────────────


def test_forensics_text_output_carries_the_line(incident_doc: Path) -> None:
    res = runner.invoke(app, ["forensics", "timeline", str(incident_doc)])
    assert res.exit_code == 0, res.output
    assert _flat(FORENSICS_LINE) in _flat(res.output)


def test_forensics_json_output_carries_the_line(incident_doc: Path) -> None:
    res = runner.invoke(app, ["forensics", "timeline", str(incident_doc), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["honesty_line"] == FORENSICS_LINE


# ── What the lines must actually say ──────────────────────────────────────


@pytest.mark.parametrize("line", [EVAL_COST_LINE, FORENSICS_LINE])
def test_line_disclaims_rather_than_advertises(line: str) -> None:
    """An honesty line has to say what NovaFabric does NOT do.

    Without this, "NovaFabric provides forensic timelines" would satisfy a
    naive presence check while asserting the opposite of the invariant.
    """
    assert " not " in f" {line.lower()} "


@pytest.mark.parametrize("line", [EVAL_COST_LINE, FORENSICS_LINE])
def test_line_is_not_empty_or_placeholder(line: str) -> None:
    assert len(line) > 40
    assert "TODO" not in line.upper()
