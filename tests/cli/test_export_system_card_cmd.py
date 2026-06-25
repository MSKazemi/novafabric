"""CLI smoke tests for `nova export-system-card` (ADR-0085)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.signing import (
    LocalSigner,
    generate_keypair,
    verify_with_pem,
)

runner = CliRunner()


def _make_capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap1"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": "r-cli", "model": "m", "provider": "p"})
    )
    return cap


def test_export_system_card_default_output(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    cap = _make_capsule(tmp_path)

    result = runner.invoke(app, ["export-system-card", str(cap)])
    assert result.exit_code == 0, result.output

    out = cap / "system-card.intoto.json"
    assert out.exists()
    envelope = json.loads(out.read_text())
    assert envelope["payloadType"] == "application/vnd.in-toto+json"


def test_export_system_card_with_key_verifies(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    priv, _pub = generate_keypair(tmp_path / "keys")
    out = tmp_path / "card.intoto.json"

    result = runner.invoke(
        app, ["export-system-card", str(cap), "-o", str(out), "--key", str(priv)]
    )
    assert result.exit_code == 0, result.output

    envelope = json.loads(out.read_text())
    signer = LocalSigner(priv)
    statement = dsse_verify(
        envelope, lambda pae, sig: verify_with_pem(signer.public_pem, pae, sig)
    )
    assert statement["predicate"]["run_id"] == "r-cli"


def test_export_system_card_rejects_non_capsule(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["export-system-card", str(empty)])
    assert result.exit_code == 1
