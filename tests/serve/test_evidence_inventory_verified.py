"""The evidence inventory's ``verified`` column runs the real bundle verifier (review serve-17).

``report_evidence_inventory`` recomputed ``manifest_hash`` its own way — a different
canonicalisation from the one bundles are built with — so every valid bundle showed
``verified: False``. It now asks :func:`verify_bundle_archive`, the recipe ``nova
verify <bundle>.zip`` and ``POST /api/evidence/{id}/verify`` use.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.evidence.signing import generate_keypair
from novafabric.serve.reports import report_evidence_inventory


@pytest.fixture()
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    cap = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "pass"]
    ).capsule_dir
    keys = tmp_path / "keys"
    keys.mkdir()
    key, _ = generate_keypair(keys)
    out = evidence / f"{cap.name}.zip"
    result = CliRunner().invoke(app, ["export-evidence", str(cap), "--key", str(key), "--output", str(out)])
    assert result.exit_code == 0, result.output
    return out


def test_a_valid_bundle_is_verified(bundle: Path) -> None:
    _, rows = report_evidence_inventory()
    (row,) = rows
    assert row["verified"] is True


def test_a_tampered_bundle_is_not(bundle: Path, tmp_path: Path) -> None:
    tampered = tmp_path / "t.zip"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(tampered, "w") as dst:
        victim = next(
            i.filename for i in src.infolist() if i.filename != "manifest.json" and not i.is_dir()
        )
        for info in src.infolist():
            data = src.read(info)
            dst.writestr(info, data + b"\n" if info.filename == victim else data)
    tampered.replace(bundle)
    _, rows = report_evidence_inventory()
    (row,) = rows
    assert row["verified"] is False
