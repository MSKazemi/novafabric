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

"""The bring-your-own-catalog tutorial runs as written and stays within its claims.

The tutorial's single YAML block must load as a profile and give the result the
tutorial prints; the page must carry the non-affiliation disclaimer verbatim and
must not describe a mapping as making anything "compliant".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.audit.loader import load_profile

_DOC = Path(__file__).resolve().parents[2] / "docs" / "tutorials" / "map-evidence-to-your-control-framework.md"

DISCLAIMER = (
    "NovaFabric is not affiliated with, endorsed by, or certified by the publishers of these "
    "frameworks. A mapping shows which recorded evidence exists; it does not certify that a "
    "control is met."
)


def _text(path: Path = _DOC) -> str:
    return path.read_text(encoding="utf-8")


def _yaml_block(text: str) -> str:
    blocks = re.findall(r"```yaml\n(.*?)```", text, flags=re.DOTALL)
    assert len(blocks) == 1, f"expected exactly one yaml block, found {len(blocks)}"
    return blocks[0]


def test_the_catalog_in_the_tutorial_loads(tmp_path: Path) -> None:
    path = tmp_path / "example-ai-1.yaml"
    path.write_text(_yaml_block(_text()), encoding="utf-8")
    profile = load_profile(str(path))
    assert [c.id for c in profile.controls] == ["EX-LOG-1", "EX-TOOL-1", "EX-SEAL-1"]


def test_the_tutorial_run_gives_the_printed_result(tmp_path: Path) -> None:
    path = tmp_path / "example-ai-1.yaml"
    path.write_text(_yaml_block(_text()), encoding="utf-8")
    cap = tmp_path / "data" / "capsules" / "demo-run"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: demo-run\n", encoding="utf-8")
    (cap / "model-calls.jsonl").write_text('{"model": "m"}\n', encoding="utf-8")
    (cap / "tool-calls.jsonl").write_text('{"tool": "search"}\n', encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["audit", "map", "--profile", str(path), "--data-dir", str(tmp_path / "data"),
         "--capsule", "demo-run", "--json"],
    )
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1  # the tutorial says: a missing required control exits 1
    doc = json.loads(result.output)
    status = {c["control_id"]: c["status"] for c in doc["coverages"]}
    assert status == {"EX-LOG-1": "covered", "EX-TOOL-1": "covered", "EX-SEAL-1": "missing"}
    assert round(doc["overall_score"] * 100, 1) == 66.7


def test_the_disclaimer_is_present_verbatim() -> None:
    assert DISCLAIMER in " ".join(_text().split())


def test_the_tutorial_never_calls_a_mapping_compliant() -> None:
    assert "compliant" not in _text().lower()


def test_the_tutorial_is_listed_in_the_index() -> None:
    index = _DOC.parent / "README.md"
    assert "map-evidence-to-your-control-framework.md" in index.read_text(encoding="utf-8")


def test_the_tutorial_does_not_promise_unmapped_controls_show_as_gaps() -> None:
    # A control with no evidence requirements cannot be loaded (it would score as
    # covered), so the page must tell readers to leave such controls out.
    text = " ".join(_text().split())
    assert "unmapped control is an honest gap" not in text
    assert "out of the catalog" in text
