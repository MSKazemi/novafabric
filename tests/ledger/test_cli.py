"""ADR-0094 (A) — ``nova ledger anchor|verify|status`` CLI tests.

The CLI is exposed as ``ledger_app`` (not registered in main.py here — the
parent wires it). ``verify`` exits with the ADR-0094 taxonomy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.ledger import ledger_app
from novafabric.evidence.signing import generate_keypair

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"model": "m", "n": 0}) + "\n"
        + json.dumps({"model": "m", "n": 1}) + "\n"
    )
    (capsule / "tool-calls.jsonl").write_text(json.dumps({"tool": "db"}) + "\n")
    return capsule


def test_anchor_then_verify_passes(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    priv, pub = generate_keypair(tmp_path / "keys")
    res = runner.invoke(
        ledger_app,
        ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1", "--epoch", "0"],
    )
    assert res.exit_code == 0, res.output
    assert (capsule / ".ledger" / "checkpoint.json").exists()

    res2 = runner.invoke(
        ledger_app, ["verify", str(capsule), "--pubkey", str(pub)]
    )
    assert res2.exit_code == 0, res2.output


def test_verify_no_checkpoint_exits_10(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    res = runner.invoke(ledger_app, ["verify", str(capsule)])
    assert res.exit_code == 10


def test_verify_detects_content_edit_exit_3(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    priv, pub = generate_keypair(tmp_path / "keys")
    runner.invoke(
        ledger_app,
        ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1", "--epoch", "0"],
    )
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    lines[0] = json.dumps({"model": "m", "n": 999})
    (capsule / "model-calls.jsonl").write_text("\n".join(lines) + "\n")
    res = runner.invoke(ledger_app, ["verify", str(capsule), "--pubkey", str(pub)])
    assert res.exit_code == 3


def test_status_shows_chain_heads(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    priv, _ = generate_keypair(tmp_path / "keys")
    runner.invoke(
        ledger_app,
        ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1", "--epoch", "0"],
    )
    res = runner.invoke(ledger_app, ["status", str(capsule)])
    assert res.exit_code == 0, res.output
    assert "model-calls" in res.output
    assert "tool-calls" in res.output


def test_status_json_output(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    priv, _ = generate_keypair(tmp_path / "keys")
    runner.invoke(
        ledger_app,
        ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1", "--epoch", "0"],
    )
    res = runner.invoke(ledger_app, ["status", str(capsule), "--format", "json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    streams = {s["stream"] for s in payload["streams"]}
    assert {"model-calls", "tool-calls"} <= streams


def test_anchor_missing_capsule_exits_1(tmp_path: Path) -> None:
    priv, _ = generate_keypair(tmp_path / "keys")
    res = runner.invoke(
        ledger_app,
        ["anchor", str(tmp_path / "nope"), "--key", str(priv), "--node-id", "n1"],
    )
    assert res.exit_code == 1


def test_status_no_chains_exits_1(tmp_path: Path) -> None:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    res = runner.invoke(ledger_app, ["status", str(capsule)])
    assert res.exit_code == 1


def test_anchor_no_jsonl_streams_exits_1(tmp_path: Path) -> None:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    priv, _ = generate_keypair(tmp_path / "keys")
    res = runner.invoke(
        ledger_app, ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1"]
    )
    assert res.exit_code == 1
    assert "No .jsonl" in res.output


def test_anchor_bad_key_exits_1(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    bad_key = tmp_path / "bad.pem"
    bad_key.write_text("not a key")
    res = runner.invoke(
        ledger_app, ["anchor", str(capsule), "--key", str(bad_key), "--node-id", "n1"]
    )
    assert res.exit_code == 1
    assert "Failed to load signing key" in res.output


def test_verify_missing_pubkey_file_exits_1(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    priv, _ = generate_keypair(tmp_path / "keys")
    runner.invoke(
        ledger_app,
        ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1", "--epoch", "0"],
    )
    res = runner.invoke(
        ledger_app, ["verify", str(capsule), "--pubkey", str(tmp_path / "nope.pem")]
    )
    assert res.exit_code == 1
    assert "Failed to read public key" in res.output


def test_status_skips_streams_without_chain(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    priv, _ = generate_keypair(tmp_path / "keys")
    runner.invoke(
        ledger_app,
        ["anchor", str(capsule), "--key", str(priv), "--node-id", "n1", "--epoch", "0"],
    )
    # add a new jsonl stream with no sidecar chain — status must skip it
    (capsule / "late.jsonl").write_text(json.dumps({"x": 1}) + "\n")
    res = runner.invoke(ledger_app, ["status", str(capsule), "--format", "json"])
    assert res.exit_code == 0, res.output
    streams = {s["stream"] for s in json.loads(res.output)["streams"]}
    assert "late" not in streams
    assert "model-calls" in streams


@pytest.mark.parametrize("verb", ["anchor", "verify", "status"])
def test_help_smoke(verb: str) -> None:
    res = runner.invoke(ledger_app, [verb, "--help"])
    assert res.exit_code == 0
    assert verb in res.output
