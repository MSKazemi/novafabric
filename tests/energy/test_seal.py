"""Slice A3 — bind an energy receipt into a DSSE/in-toto envelope (ADR-0093)."""

from __future__ import annotations

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy import ActionRef, unavailable_receipt
from novafabric.energy._seal import (
    ENERGY_PREDICATE_TYPE,
    attest_energy,
    receipt_integrity_errors,
)
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem


def _receipt():
    return unavailable_receipt(
        action_ref=ActionRef(kind="model_call", id=new_ulid()),
        run_id=new_ulid(),
        capsule_id="sha256:" + "a" * 64,
        node_id="n1",
        counters_available=(),
    )


def test_attest_energy_binds_payload_hash_as_subject(tmp_path):
    priv, _ = generate_keypair(tmp_path)
    signer = LocalSigner(priv)
    receipt = _receipt()

    envelope = attest_energy(receipt, signer)

    statement = dsse_verify(
        envelope, lambda pae, sig: verify_with_pem(signer.public_pem, pae, sig)
    )
    assert statement["predicateType"] == ENERGY_PREDICATE_TYPE
    digest = statement["subject"][0]["digest"]["sha256"]
    assert digest == receipt.payload_hash.removeprefix("sha256:")


def test_receipt_integrity_errors_detect_tampered_payload_hash():
    receipt = _receipt()
    assert receipt_integrity_errors(receipt) == []
    # tamper: change a field but keep the old payload_hash
    tampered = receipt.model_copy(update={"capsule_id": "sha256:" + "b" * 64})
    errors = receipt_integrity_errors(tampered)
    assert any("payload_hash" in e for e in errors)


def test_receipt_integrity_errors_include_consistency_violations():
    receipt = _receipt()
    forged = receipt.model_copy(
        update={"measured_joules": 5.0}
    ).with_payload_hash()  # hash now matches, but content is inconsistent
    errors = receipt_integrity_errors(forged)
    # source=unavailable with a joule value is an honesty violation
    assert any("unavailable" in e.lower() for e in errors)
