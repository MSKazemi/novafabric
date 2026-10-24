"""``pii_scan`` audit evidence is a PII redaction manifest (review compliance-privacy-04).

Every capture writes ``redaction-proof.json`` — the *secret* scanner's proof — so the
built-in ``gdpr`` profile reported ``GDPR-ART17-ERASURE`` "covered" for capsules where
no PII scan or erasure ever ran. Only the ``--pii-subject`` stage's
``redaction_manifest.json`` (a parseable ``RedactionManifest``) is PII evidence now.
"""

from __future__ import annotations

from pathlib import Path

from novafabric.compliance.audit.engine import _has_evidence
from novafabric.compliance.pii.manifest import RedactionManifest
from novafabric.evidence.binding import build_bindings


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "c1"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: c1\n")
    (cap / "redaction-proof.json").write_text('{"findings": []}')  # every capture has one
    return cap


def _art17_refs(cap: Path) -> list[str]:
    return [
        ref.path
        for b in build_bindings(cap, "gdpr") if b.criterion_id == "GDPR-ART17-ERASURE"
        for ref in b.evidence_refs
    ]


def test_the_secret_scanner_proof_is_not_pii_evidence(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    assert _has_evidence(cap, "pii_scan") is False
    assert _art17_refs(cap) == []


def test_an_empty_pii_directory_or_garbage_manifest_is_not_evidence(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "compliance" / "pii").mkdir(parents=True)
    (cap / "compliance" / "pii" / "anything.txt").write_text("x")
    (cap / "redaction_manifest.json").write_text("{not json")
    assert _has_evidence(cap, "pii_scan") is False


def test_a_pii_redaction_manifest_is_evidence(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "redaction_manifest.json").write_text(
        RedactionManifest(capsule_id="c1", entries=[]).model_dump_json()
    )
    assert _has_evidence(cap, "pii_scan") is True
    assert _art17_refs(cap) == ["redaction_manifest.json"]
