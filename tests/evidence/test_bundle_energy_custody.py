"""Energy receipts + court-admissibility binding in Evidence Bundles.

Slice 1 (ADR-0093 A3): when a capsule carries `energy-receipts.jsonl`, the
Evidence Bundle gains a signed energy attestation.
Slice 2 (ADR-0095 C2): `with_custody` embeds the chain-of-custody +
self-authentication blocks into the manifest, covered by `manifest_hash`.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import jsonschema

from novafabric.audit._log import AuditLog
from novafabric.audit._models import AuditEventType
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.energy._attribution import attest_capsule, write_receipts
from novafabric.evidence.admissibility import Custodian
from novafabric.evidence.bundle import PREDICATE_ENERGY, EvidenceBundleBuilder
from novafabric.evidence.signing import LocalSigner, generate_keypair

_SCHEMA = json.loads(
    (
        Path(__file__).parents[2]
        / "src/novafabric/schemas/evidence-bundle.schema.json"
    ).read_text()
)


def _capsule(tmp_path: Path, *, with_energy: bool) -> Path:
    cap = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "pass"]
    ).capsule_dir
    if with_energy:
        write_receipts(
            cap, attest_capsule(cap, rapl_base=tmp_path / "no-rapl", node_id="t")
        )
    return cap


def _signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


def _manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))


def test_bundle_includes_energy_attestation_when_receipts_present(tmp_path):
    cap = _capsule(tmp_path, with_energy=True)
    out = tmp_path / "e.zip"
    EvidenceBundleBuilder(cap, _signer(tmp_path), out).build()

    manifest = _manifest(out)
    predicate_types = [a["predicate_type"] for a in manifest["attestations"]]
    assert PREDICATE_ENERGY in predicate_types
    jsonschema.validate(manifest, _SCHEMA, format_checker=jsonschema.FormatChecker())


def test_bundle_has_no_energy_attestation_without_receipts(tmp_path):
    cap = _capsule(tmp_path, with_energy=False)
    out = tmp_path / "n.zip"
    EvidenceBundleBuilder(cap, _signer(tmp_path), out).build()

    manifest = _manifest(out)
    predicate_types = [a["predicate_type"] for a in manifest["attestations"]]
    assert PREDICATE_ENERGY not in predicate_types
    jsonschema.validate(manifest, _SCHEMA, format_checker=jsonschema.FormatChecker())


def test_bundle_with_custody_embeds_admissibility_block(tmp_path):
    cap = _capsule(tmp_path, with_energy=False)
    # an audit entry so the chain of custody has a witnessed event
    audit = tmp_path / "audit.jsonl"
    AuditLog(audit).append(
        AuditEventType.EVIDENCE_EXPORT, "alice@corp", cap.name, {"step": "captured"}
    )
    out = tmp_path / "c.zip"
    EvidenceBundleBuilder(
        cap,
        _signer(tmp_path),
        out,
        with_custody=True,
        custodian=Custodian(identity="alice@corp", provenance="oidc"),
        audit_log_path=audit,
    ).build()

    manifest = _manifest(out)
    assert "chain_of_custody" in manifest
    assert "admissibility_status" in manifest
    assert (
        manifest["self_authentication"]["certification"]["signature_verifies"] is True
    )
    # additive: the manifest still validates against the bundle schema
    jsonschema.validate(manifest, _SCHEMA, format_checker=jsonschema.FormatChecker())


def test_default_bundle_has_no_custody_block(tmp_path):
    cap = _capsule(tmp_path, with_energy=False)
    out = tmp_path / "d.zip"
    EvidenceBundleBuilder(cap, _signer(tmp_path), out).build()
    manifest = _manifest(out)
    assert "chain_of_custody" not in manifest
    jsonschema.validate(manifest, _SCHEMA, format_checker=jsonschema.FormatChecker())
