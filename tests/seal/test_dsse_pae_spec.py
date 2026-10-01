"""NovaSeal DSSE envelopes must use the DSSE v1 PAE, and legacy ones must keep verifying.

Audit finding S1 (2026-10-01): ``envelope._pae`` emitted ``"DSSEv1"`` followed by
8-byte little-endian lengths with no separators — a hybrid of the pre-v1 draft
signing-spec and DSSE v1 that no stock DSSE verifier (in-toto, cosign, Rekor's
``dsse`` entry type, securesystemslib) computes. A capsule seal could therefore only
be verified by NovaFabric itself.

The reference PAE below is written from the DSSE protocol text, independently of any
NovaFabric code, so the interop assertions do not compare NovaFabric with NovaFabric:

    PAE(type, body) = "DSSEv1" + SP + LEN(type) + SP + type + SP + LEN(body) + SP + body
    + = concatenation; SP = ASCII space [0x20];
    LEN(s) = ASCII decimal encoding of the byte length of s, with no leading zeros

    https://github.com/secure-systems-lab/dsse/blob/v1.0.0/protocol.md
"""

from __future__ import annotations

import base64
import datetime
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "novaseal" / "legacy-v0.102"


def reference_pae(payload_type: str, body: bytes) -> bytes:
    """DSSE v1 PAE, transcribed from the spec text (no NovaFabric code)."""
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1"
        + b" "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(body)).encode("ascii")
        + b" "
        + body
    )


def stock_verify(envelope_bytes: bytes) -> bool:
    """Verify signatures[0] the way a third-party DSSE verifier would.

    Standard base64 decode, reference PAE, ECDSA P-256/SHA-256 under the embedded cert.
    """
    env = json.loads(envelope_bytes)
    payload = base64.b64decode(env["payload"])
    sig_entry = env["signatures"][0]
    cert = x509.load_der_x509_certificate(base64.b64decode(sig_entry["cert"]))
    public_key = cert.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    try:
        public_key.verify(
            base64.b64decode(sig_entry["sig"]),
            reference_pae(env["payloadType"], payload),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature:
        return False
    return True


@pytest.fixture()
def signing_material(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pae-spec-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "c.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


# ---------------------------------------------------------------------------
# The reference itself is right (spec test vector)
# ---------------------------------------------------------------------------


def test_reference_pae_matches_the_spec_test_vector() -> None:
    # protocol.md "Test Vectors"
    assert (
        reference_pae("http://example.com/HelloWorld", b"hello world")
        == b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )


def test_novaseal_pae_is_the_spec_pae() -> None:
    from novafabric.trust.novaseal.envelope import _pae

    for payload_type, body in [
        ("http://example.com/HelloWorld", b"hello world"),
        ("application/vnd.novafabric.capsule+json", b""),
        ("application/vnd.novafabric.capsule+json", b'{"a":1}' * 100),
    ]:
        assert _pae(payload_type, body) == reference_pae(payload_type, body)


# ---------------------------------------------------------------------------
# New envelopes verify with stock tooling
# ---------------------------------------------------------------------------


def test_new_capsule_seal_verifies_with_a_stock_dsse_verifier(
    tmp_path: Path, signing_material: tuple[Path, Path]
) -> None:
    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    key_path, cert_path = signing_material
    seal = NovaSeal(
        KeyConfig("local", str(key_path), str(cert_path)),
        tsa_url="",
        db_path=str(tmp_path / "m.db"),
    )
    bundle = seal.seal({"run_id": "stock-interop", "status": "success"})
    assert stock_verify(bundle.dsse_envelope)


def test_new_promote_envelope_verifies_with_a_stock_dsse_verifier(
    signing_material: tuple[Path, Path],
) -> None:
    from novafabric.promote.predicates import PROPOSAL_PAYLOAD_TYPE, sign_promote_envelope

    key_path, cert_path = signing_material
    envelope = sign_promote_envelope(b'{"role":"proposer"}', PROPOSAL_PAYLOAD_TYPE, key_path, cert_path)
    assert stock_verify(envelope)


def test_new_envelope_reports_the_spec_encoding(signing_material: tuple[Path, Path]) -> None:
    from novafabric.trust.novaseal.envelope import (
        PAE_DSSE_V1,
        create_envelope,
        verify_envelope_encoding,
    )

    key_path, cert_path = signing_material
    envelope = create_envelope(b"{}", key_path, cert_path)
    assert verify_envelope_encoding(envelope) == PAE_DSSE_V1


# ---------------------------------------------------------------------------
# Envelopes sealed by released versions keep verifying — flagged as legacy
# ---------------------------------------------------------------------------


def test_released_capsule_envelope_is_not_stock_verifiable() -> None:
    """Documents the defect: the golden envelope fails the reference verifier."""
    assert not stock_verify((FIXTURES / "capsule" / ".seal" / "manifest.dsse").read_bytes())


def test_released_capsule_envelope_still_verifies_and_is_flagged_legacy() -> None:
    from novafabric.trust.novaseal.envelope import (
        PAE_LEGACY,
        verify_envelope,
        verify_envelope_encoding,
    )

    dsse = (FIXTURES / "capsule" / ".seal" / "manifest.dsse").read_bytes()
    assert verify_envelope(dsse) is True
    assert verify_envelope_encoding(dsse) == PAE_LEGACY


def test_released_promote_envelope_still_verifies() -> None:
    from novafabric.promote.predicates import PROPOSAL_PAYLOAD_TYPE, verify_promote_envelope

    payload, subject = verify_promote_envelope(
        (FIXTURES / "promote-proposal.dsse").read_bytes(), PROPOSAL_PAYLOAD_TYPE
    )
    assert json.loads(payload)["role"] == "proposer"
    assert "golden fixture" in subject


def test_novaseal_verify_reports_legacy_encoding_on_released_capsule(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import KeyConfig, NovaSeal
    from novafabric.trust.novaseal.envelope import PAE_LEGACY

    seal = NovaSeal(KeyConfig("local", "", ""), tsa_url="", db_path=str(tmp_path / "m.db"))
    result = seal.verify("", str(FIXTURES / "capsule" / ".seal"))
    assert result.signature_ok
    assert result.pae_encoding == PAE_LEGACY


def test_legacy_signature_is_not_accepted_as_a_spec_signature(
    signing_material: tuple[Path, Path],
) -> None:
    """A signature is accepted over exactly the PAE it was made over — never both."""
    from novafabric.trust.novaseal.envelope import _pae, _pae_legacy

    for payload_type in (
        "application/vnd.novafabric.capsule+json",
        "application/vnd.novafabric.promote.proposal+json",
    ):
        legacy = _pae_legacy(payload_type, b"x")
        spec = _pae(payload_type, b"x")
        # Byte 6 is SP (0x20) in DSSE v1 and the low length byte in the legacy form;
        # byte 7 is an ASCII digit in DSSE v1 and 0x00 (high length byte) in legacy.
        assert spec[6] == 0x20 and spec[7] in b"0123456789"
        assert legacy[7] == 0x00
        assert legacy != spec


def test_tampered_legacy_envelope_fails() -> None:
    from novafabric.trust.novaseal.envelope import EnvelopeError, verify_envelope

    env = json.loads((FIXTURES / "capsule" / ".seal" / "manifest.dsse").read_bytes())
    payload = base64.b64decode(env["payload"] + "=" * (-len(env["payload"]) % 4))
    env["payload"] = base64.b64encode(payload.replace(b"golden", b"forged")).decode()
    with pytest.raises(EnvelopeError):
        verify_envelope(json.dumps(env).encode())


def test_x509_signer_binding_accepts_spec_envelopes(
    signing_material: tuple[Path, Path],
) -> None:
    """ADR-0055 signer binding must recompute the same PAE as the signer."""
    from novafabric.trust.novaseal.envelope import create_envelope
    from novafabric.trust.novaseal.x509_identity import _dsse_entry_bound_cert, _dsse_pae

    key_path, cert_path = signing_material
    envelope = json.loads(create_envelope(b"{}", key_path, cert_path))
    cert, why = _dsse_entry_bound_cert(
        envelope["signatures"][0], _dsse_pae(envelope["payloadType"], b"{}")
    )
    assert cert is not None, why
