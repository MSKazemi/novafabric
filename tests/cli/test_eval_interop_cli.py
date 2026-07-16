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

"""CLI tests for ``nova eval import-inspect`` / ``export-inspect`` (NF-024)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.eval.inspect_interop import IMPORT_RECORD_PATH
from novafabric.eval.scores import SCORES_FILENAME, read_scores

runner = CliRunner()

FIXTURES = Path(__file__).parent.parent / "fixtures"
VALID_LOG = FIXTURES / "inspect_log_valid.json"
INVALID_LOG = FIXTURES / "inspect_log_invalid.json"


def test_import_inspect_help() -> None:
    result = runner.invoke(app, ["eval", "import-inspect", "--help"])
    assert result.exit_code == 0, result.output
    assert "Inspect" in result.output


def test_export_inspect_help() -> None:
    result = runner.invoke(app, ["eval", "export-inspect", "--help"])
    assert result.exit_code == 0, result.output
    assert "Inspect" in result.output


def test_import_inspect_writes_scores_record_and_facet(tmp_path: Path) -> None:
    cap = tmp_path / "capsule"
    result = runner.invoke(
        app, ["eval", "import-inspect", str(VALID_LOG), "--capsule", str(cap)]
    )
    assert result.exit_code == 0, result.output
    assert "Imported" in result.output
    assert "unmapped" in result.output
    scores = read_scores(cap / SCORES_FILENAME)
    assert len(scores) == 7  # 4 sample scores + 3 aggregate metrics
    # honesty ledger preserved under extensions/org.inspect/
    record = json.loads((cap / IMPORT_RECORD_PATH).read_text())
    assert record["provenance"]["source"] == "inspect-ai"
    assert record["unmapped"]
    assert record["omitted"]
    # NF-028 dataset facet recorded (no hashes in Inspect logs → unknown)
    facets = list((cap / "extensions" / "dev.novafabric.dataset-provenance").glob("*.json"))
    assert len(facets) == 1
    assert json.loads(facets[0].read_text())["name"] == "hello-dataset"


def test_import_inspect_invalid_log_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "import-inspect", str(INVALID_LOG), "--capsule", str(tmp_path / "cap")],
    )
    assert result.exit_code == 2
    assert not (tmp_path / "cap").exists()


def test_import_inspect_missing_log_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "import-inspect", str(tmp_path / "nope.json"), "--capsule", str(tmp_path / "cap")],
    )
    assert result.exit_code == 2


def test_export_inspect_to_stdout(tmp_path: Path) -> None:
    cap = tmp_path / "capsule"
    runner.invoke(app, ["eval", "import-inspect", str(VALID_LOG), "--capsule", str(cap)])
    result = runner.invoke(app, ["eval", "export-inspect", str(cap)])
    assert result.exit_code == 0, result.output
    assert '"example/hello"' in result.output


def test_export_inspect_to_file_roundtrip(tmp_path: Path) -> None:
    cap = tmp_path / "capsule"
    out = tmp_path / "out" / "log.json"
    runner.invoke(app, ["eval", "import-inspect", str(VALID_LOG), "--capsule", str(cap)])
    result = runner.invoke(
        app, ["eval", "export-inspect", str(cap), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    log = json.loads(out.read_text())
    assert log["version"] == 2
    assert log["eval"]["task"] == "example/hello"
    assert len(log["samples"]) == 2
    assert log["samples"][0]["scores"]["match"]["value"] == "C"


def test_export_inspect_capsule_without_scores(tmp_path: Path) -> None:
    cap = tmp_path / "empty"
    cap.mkdir()
    result = runner.invoke(app, ["eval", "export-inspect", str(cap)])
    assert result.exit_code == 0, result.output
    assert '"total_samples": 0' in result.output


def test_export_inspect_not_a_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", "export-inspect", str(tmp_path / "nope")])
    assert result.exit_code == 2
