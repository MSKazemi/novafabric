"""Exported documents claim a seal or signing only when one verifies (review compliance-privacy-12).

The Art. 12 report keyed tamper-evidence on file names NovaFabric never writes
(``seal-envelope.json``), so a hand-made file counted and a genuine ``.seal/`` read
as unsealed; the GDPR RoPA listed DSSE signing "always"; the FDA PCCP asserted
NovaSeal signing, Rego gates and maker-checker approval as constants.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.compliance.export import euaiact_art12, examiner, gdpr_ropa


def _art12(capsule_dir: Path) -> str:
    report = euaiact_art12.build_art12_report(capsule_dir, deployment_id="dep-1")
    return json.dumps(report.model_dump(mode="json"))


def test_a_decoy_seal_file_is_not_tamper_evidence(minimal_capsule_dir: Path) -> None:
    (minimal_capsule_dir / "seal-envelope.json").write_text("{}")
    dumped = _art12(minimal_capsule_dir)
    assert "is sealed" not in dumped and "seal verifies against" not in dumped


def test_a_verifying_seal_is_tamper_evidence(
    minimal_capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(euaiact_art12, "capsule_seal_verifies", lambda _d: True)
    assert "seal verifies against its files" in _art12(minimal_capsule_dir)


def test_ropa_lists_signing_only_for_a_verifying_seal(
    minimal_capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (minimal_capsule_dir / "seal.json").write_text("{}")  # a file nothing writes
    exporter = gdpr_ropa.GDPRRoPAExporter({})
    entry = exporter.build_ropa_entry(minimal_capsule_dir)
    assert "dsse_envelope_signing" not in json.dumps(entry.model_dump(mode="json"))
    assert "rfc3161_timestamp" not in json.dumps(entry.model_dump(mode="json"))
    monkeypatch.setattr(gdpr_ropa, "capsule_seal_verifies", lambda _d: True)
    entry = exporter.build_ropa_entry(minimal_capsule_dir)
    assert "dsse_envelope_signing" in json.dumps(entry.model_dump(mode="json"))


def test_pccp_asserts_no_unverified_control(tmp_path: Path) -> None:
    data_dir = tmp_path / "caps"
    for cid in ("run-a", "run-b"):
        (data_dir / cid).mkdir(parents=True)
        (data_dir / cid / "capsule.yaml").write_text(f"run_id: {cid}\n")
    out = examiner.PCCPExporter("run-a", "run-b", data_dir).export(tmp_path / "pccp.json")
    section = json.loads(out.read_text())["section_3_impact_assessment"]
    assert section["risk_controls"] == []
    assert "Maker-checker approval" in section["risk_controls_to_confirm"]
