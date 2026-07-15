"""Regression test for examples/spkg-anomaly-scan/ — the planted attack edge ranks first.

Pure-stdlib (the detector needs no optional extra), so this always runs.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()
_EXAMPLE = Path(__file__).parent.parent / "examples" / "spkg-anomaly-scan"


def _load_make_fixture():
    spec = importlib.util.spec_from_file_location(
        "spkg_make_fixture", _EXAMPLE / "make_fixture.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_detects_planted_shell_edge(tmp_path, monkeypatch):
    mod = _load_make_fixture()
    # Redirect the fixture's output into tmp so we never write into the repo tree.
    capsule = tmp_path / "capsule"
    monkeypatch.setattr(mod, "CAPSULE", capsule)
    mod.main()

    lineage = json.loads((capsule / "lineage.jsonl").read_text().splitlines()[-1])
    assert lineage["target"]["ref"] == "tool:/bin/shell"  # planted edge is last

    result = runner.invoke(app, ["kg", "detect", str(capsule), "-k", "3", "--json"])
    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)
    # The top-ranked finding is the shell exec, mapped to the Unix-shell technique.
    assert findings[0]["explanation"]["attack_technique_id"] == "T1059.004"
    assert findings[0]["score"] == 1.0
