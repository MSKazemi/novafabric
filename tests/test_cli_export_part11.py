"""ADR-0160 — the ``nova export-part11`` CLI.

Read-only. Renders the 21 CFR Part 11 electronic-records evidence artifact and — per ADR-0160 —
**prints the binding medical-honesty banner in every output** (rich and JSON). Exit 0 on render;
2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "capsule_root": "r" * 64,
    "elements": {
        "signer_identity": "capsule://root/signer#d1",
        "trusted_timestamp": "capsule://root/tsr#d2",
    },
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "p11.json"
    p.write_text(json.dumps(doc))
    return p


def test_output_carries_the_banner(tmp_path):
    result = runner.invoke(app, ["export-part11", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "not a medical device" in result.output.lower()  # binding banner present
    assert "signer_identity" in result.output


def test_json_output_carries_banner_and_no_conformity(tmp_path):
    result = runner.invoke(app, ["export-part11", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "not a medical device" in payload["banner"].lower()
    assert "conformity" not in payload
    ts = next(f for f in payload["fields"] if f["element"] == "trusted_timestamp")
    assert ts["status"] == "complete"


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-part11", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-part11", str(p)])
    assert result.exit_code == 2
