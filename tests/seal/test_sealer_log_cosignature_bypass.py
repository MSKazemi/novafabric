"""A throw-away co-signature cannot hide a forged seal from the sealer's own log.

Review trust-evidence-01. The sealer-log check (ADR-0297 Amendment 2, the attack-M4
fix) fails a capsule that is absent from a Merkle log holding other seals by the same
signer. It decided "same signer" from the entry's ``keyid`` — written by whoever
wrote ``log-entry.json`` — and the certificate of the **first** signature that
verifies. A holder of the sealing key K forged a capsule outside the sealing path,
put a fresh self-signed key K2 first in the envelope and ``keyid: fp(K2)`` in the
entry: the check then asked about K2 only, the log "holds no seals by this signer",
and the forgery verified, chain-anchored as K. Every verifying signer now counts.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import Encoding

from novafabric.trust.novaseal import (
    LOG_INCLUSION_FAILED,
    LOG_INCLUSION_LOCAL,
    KeyConfig,
    NovaSeal,
    verify_seal_dir,
)
from novafabric.trust.novaseal.envelope import SigningIntent, bind_intent
from novafabric.trust.novaseal.merkle import MerkleLog, _leaf_hash

from ._x509_pki import Node, dsse_envelope, ecdsa_entry, make_pki


def _fingerprint(node: Node) -> str:
    return hashlib.sha256(node.cert.public_bytes(Encoding.DER)).hexdigest()


@pytest.fixture
def sealer(tmp_path: Path) -> tuple[Node, Path]:
    """Key K and a Merkle log that already holds three genuine seals by K."""
    leaf = make_pki("sealer").leaf
    key_path, cert_path = tmp_path / "k.key", tmp_path / "k.crt"
    key_path.write_bytes(leaf.key_pem)
    cert_path.write_bytes(leaf.cert_pem)
    db = tmp_path / "merkle.db"
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(db),
    )
    for i in range(3):
        seal.seal({"run_id": f"legit-{i}"})
    seal._merkle.close()
    return leaf, db


def _forge(tmp_path: Path, signers: list[Node], entry_keyid: str) -> Path:
    """A seal written outside the sealing path, with a self-consistent carried proof."""
    payload = json.dumps(
        bind_intent({"run_id": "forged"}, SigningIntent.AUTHORED),
        sort_keys=True, separators=(",", ":"),
    ).encode()
    entries = [{**ecdsa_entry(node, payload), "intent": "authored"} for node in signers]
    entry = {
        "capsule_id": hashlib.sha256(payload).hexdigest(),
        "has_tsr": False,
        "keyid": entry_keyid,
    }
    leaf = _leaf_hash(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
    record = {
        "leaf_index": 0, "leaf_hash": leaf, "root_hash": leaf, "tree_size": 1,
        "root_version": 2, "entry": entry, "inclusion_proof": [],
    }
    seal_dir = tmp_path / "forged" / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(dsse_envelope(payload, entries))
    (seal_dir / "log-entry.json").write_text(json.dumps(record))
    return seal_dir


def _verify(seal_dir: Path, db: Path):  # type: ignore[no-untyped-def]
    log = MerkleLog(db)
    try:
        return verify_seal_dir(seal_dir, merkle_log=log)
    finally:
        log.close()


def test_control_a_forgery_signed_by_k_alone_fails(
    tmp_path: Path, sealer: tuple[Node, Path]
) -> None:
    k, db = sealer
    result = _verify(_forge(tmp_path, [k], _fingerprint(k)), db)
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert result.valid is False


@pytest.mark.parametrize("throwaway_first", [True, False], ids=["k2-first", "k-first"])
def test_a_throwaway_co_signature_does_not_hide_the_sealer(
    tmp_path: Path, sealer: tuple[Node, Path], throwaway_first: bool
) -> None:
    k, db = sealer
    k2 = make_pki("throwaway").leaf
    signers = [k2, k] if throwaway_first else [k, k2]
    result = _verify(_forge(tmp_path, signers, _fingerprint(k2)), db)
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert result.valid is False
    assert any("not in the local Merkle log" in e for e in result.errors), result.errors


def test_a_genuine_seal_still_verifies_against_its_log(
    tmp_path: Path, sealer: tuple[Node, Path]
) -> None:
    k, db = sealer
    key_path, cert_path = tmp_path / "k.key", tmp_path / "k.crt"
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(db),
    )
    bundle = seal.seal({"run_id": "genuine"})
    seal._merkle.close()
    seal_dir = tmp_path / "genuine" / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    assert _verify(seal_dir, db).log_inclusion == LOG_INCLUSION_LOCAL


def test_signer_fingerprints_lists_every_verifying_entry_only() -> None:
    from novafabric.trust.novaseal.identity_trust import verifying_signer_fingerprints

    k, k2 = make_pki("a").leaf, make_pki("b").leaf
    payload = b'{"run_id":"x"}'
    junk = {"sig": base64.b64encode(b"\0" * 64).decode(), "cert": ecdsa_entry(k2, payload)["cert"]}
    envelope = dsse_envelope(payload, [junk, ecdsa_entry(k, payload), ecdsa_entry(k2, payload)])
    # The junk entry carries k2's certificate but does not verify; k2 is listed
    # because its own genuine entry verifies, and k because its entry does.
    assert verifying_signer_fingerprints(envelope) == (_fingerprint(k), _fingerprint(k2))
    assert verifying_signer_fingerprints(dsse_envelope(payload, [junk])) == ()
    assert verifying_signer_fingerprints(b"not json") == ()
