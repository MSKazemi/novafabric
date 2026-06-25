"""Bind an energy receipt into the NovaSeal/Evidence machinery (ADR-0093 §A3).

A receipt's ``payload_hash`` is the subject of an in-toto Statement carrying the
energy predicate; the statement is DSSE-signed exactly as
``evidence/completeness.py`` signs the completeness assertion. The compute node
emits only the hash; signing happens at the finalizer/collector boundary (no
heavy crypto in the agent hot path).
"""

from __future__ import annotations

from typing import Any

from novafabric.energy._receipt import EnergyReceipt
from novafabric.evidence.intoto import dsse_sign, make_intoto_statement

ENERGY_PREDICATE_TYPE = "https://novafabric.io/energy-receipt/v0"


def attest_energy(receipt: EnergyReceipt, signer: Any) -> dict[str, Any]:
    """Wrap a receipt in an in-toto Statement and DSSE-sign it.

    The subject is the receipt's ``payload_hash`` (sha256 over the canonical
    receipt minus the seal block), so the signature binds the receipt content.
    """
    statement = make_intoto_statement(
        predicate_type=ENERGY_PREDICATE_TYPE,
        subject_name=f"energy-receipt/{receipt.receipt_id}",
        subject_sha256=receipt.payload_hash,
        predicate=receipt.to_record(),
    )
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope


def receipt_integrity_errors(receipt: EnergyReceipt) -> list[str]:
    """All reasons a receipt should be rejected by ``nova energy verify``.

    Combines the payload-hash integrity check (content matches its claimed hash)
    with the honesty/forgery checks (:meth:`EnergyReceipt.consistency_errors`).
    """
    errors: list[str] = []
    expected = receipt.compute_payload_hash()
    if receipt.payload_hash and receipt.payload_hash != expected:
        errors.append(
            f"payload_hash mismatch: recorded {receipt.payload_hash}, "
            f"recomputed {expected}"
        )
    errors.extend(receipt.consistency_errors())
    return errors


__all__ = ["ENERGY_PREDICATE_TYPE", "attest_energy", "receipt_integrity_errors"]
