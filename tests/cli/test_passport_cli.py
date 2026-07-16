"""CLI tests for ``nova passport`` (ADR-0149 / NF-179)."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _write(tmp_path: Path, name: str, obj: object) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return p


def test_help_smoke() -> None:
    assert runner.invoke(app, ["passport", "--help"]).exit_code == 0
    assert runner.invoke(app, ["passport", "issue", "--help"]).exit_code == 0
    assert runner.invoke(app, ["passport", "verify", "--help"]).exit_code == 0


def test_issue_green(tmp_path: Path) -> None:
    refs = _write(tmp_path, "refs.json", {
        "agent_ref": "agent@v1",
        "present": {
            n: f"sha256:{n}"
            for n in ("identity", "lineage", "aibom", "card", "package", "delegation")
        },
    })
    result = runner.invoke(app, ["passport", "issue", str(refs)])
    assert result.exit_code == 0
    assert "(green)" in result.stdout


def test_issue_missing_agent_ref_exits_2(tmp_path: Path) -> None:
    refs = _write(tmp_path, "bad.json", {"present": {"identity": "sha256:x"}})
    result = runner.invoke(app, ["passport", "issue", str(refs)])
    assert result.exit_code == 2


def test_issue_then_verify_roundtrip(tmp_path: Path) -> None:
    refs = _write(tmp_path, "refs.json", {
        "agent_ref": "agent@v1",
        "present": {"identity": "sha256:aa", "aibom": "sha256:cc"},
        "opaque": ["lineage"],
    })
    issued = runner.invoke(app, ["passport", "issue", str(refs), "--json"])
    assert issued.exit_code == 0
    passport = _write(tmp_path, "passport.json", json.loads(issued.stdout))
    verified = runner.invoke(app, ["passport", "verify", str(passport)])
    assert verified.exit_code == 0
    assert "verified" in verified.stdout


def test_verify_detects_tampered_status_exits_3(tmp_path: Path) -> None:
    refs = _write(tmp_path, "refs.json", {
        "agent_ref": "agent@v1", "present": {"identity": "sha256:aa"},
    })
    issued = runner.invoke(app, ["passport", "issue", str(refs), "--json"])
    doc = json.loads(issued.stdout)
    doc["status"] = "green"  # tamper: real status is amber (missing components)
    passport = _write(tmp_path, "tampered.json", doc)
    result = runner.invoke(app, ["passport", "verify", str(passport)])
    assert result.exit_code == 3
