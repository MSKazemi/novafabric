"""CLI tests for `nova kg attack-path` / `blast-radius` (ADR-0111 UC2/UC3).

Requires the SPKG extra (kuzu); skipped otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("kuzu")

from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.main import app  # noqa: E402

runner = CliRunner()

_EDGES = [
    {"edge_type": "uses", "source": {"kind": "run", "ref": "attacker"},
     "target": {"kind": "tool", "ref": "shell"},
     "capsule_run_id": "attacker", "created_at": "2026-07-02T14:00:00.000Z"},
    {"edge_type": "reads", "source": {"kind": "tool", "ref": "shell"},
     "target": {"kind": "dataset", "ref": "aws_credentials"},
     "capsule_run_id": "attacker", "created_at": "2026-07-02T14:00:00.000Z"},
]


def _capsule(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _EDGES) + "\n", encoding="utf-8"
    )
    return path


def test_attack_path_found(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "cap")
    r = runner.invoke(
        app,
        ["kg", "attack-path", str(cap), "--from", "run:attacker",
         "--to", "dataset:aws_credentials"],
    )
    assert r.exit_code == 0, r.output
    assert "Attack path found" in r.output
    assert "2 hop" in r.output


def test_attack_path_none_in_reverse(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "cap")
    r = runner.invoke(
        app,
        ["kg", "attack-path", str(cap), "--from", "dataset:aws_credentials",
         "--to", "run:attacker"],
    )
    assert r.exit_code == 0, r.output
    assert "No attack path" in r.output


def test_blast_radius_downstream(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "cap")
    r = runner.invoke(
        app, ["kg", "blast-radius", str(cap), "--entity", "run:attacker"]
    )
    assert r.exit_code == 0, r.output
    assert "shell" in r.output
    assert "aws_credentials" in r.output


def test_blast_radius_upstream(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "cap")
    r = runner.invoke(
        app,
        ["kg", "blast-radius", str(cap), "--entity", "dataset:aws_credentials", "--upstream"],
    )
    assert r.exit_code == 0, r.output
    assert "attacker" in r.output
    assert "shell" in r.output


def test_bad_entity_spec_exits_2(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "cap")
    r = runner.invoke(
        app, ["kg", "attack-path", str(cap), "--from", "no-colon", "--to", "run:x"]
    )
    assert r.exit_code == 2, r.output
