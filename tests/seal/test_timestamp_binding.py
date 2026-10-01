"""What the RFC 3161 token covers, and that verification checks exactly that.

Audit finding S3 (2026-10-01) said the timestamp token "is not covered by the
signature". That is **by design** and cannot be otherwise: ADR-0030 timestamps the
SHA-256 of the *serialised DSSE envelope* — signature included — so the token is
computed after the signature exists and covers it, not the reverse. RFC 3161 has the
same shape everywhere (a TSA countersigns a hash of already-signed bytes).

What was a real gap is how the token was checked without a TSA trust anchor.
``verify_timestamp`` accepted a token when *any* OCTET STRING anywhere in it equalled
SHA-256(envelope), and verified the CMS signature over the signed attributes without
checking that those attributes' ``messageDigest`` matches the ``TSTInfo``. So:

* a genuine token for envelope A, with A's hash swapped for B's inside ``TSTInfo``,
  verified as a timestamp of B; and
* a "token" with no TSA signature at all — a granted status plus the hash as a bare
  OCTET STRING — verified, and ``nova verify`` printed ``Timestamp (RFC 3161): OK``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal.timestamp import check_timestamp, verify_timestamp

from ._tsa_pki import TokenSpec, TsaNode, make_tsa_cert, make_tsr
from ._x509_pki import Node, make_cert

FREETSA = Path(__file__).parent.parent / "fixtures" / "rfc3161" / "freetsa-response.tsr"


@pytest.fixture(scope="module")
def tsa() -> TsaNode:
    ca: Node = make_cert("s3-tsa-root", issuer=None, ca=True)
    return make_tsa_cert(ca)


@pytest.fixture()
def envelope(tmp_path: Path) -> bytes:
    from novafabric.trust.novaseal.envelope import create_envelope

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "s3")])
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
    return create_envelope(b'{"run_id":"s3"}', key_path, cert_path)


def _tsr_for(tsa: TsaNode, data: bytes, **kw: object) -> bytes:
    return make_tsr(tsa, TokenSpec(digest=hashlib.sha256(data).digest(), **kw))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# By design: the token covers the signed envelope, signature included
# ---------------------------------------------------------------------------


def test_token_over_the_envelope_verifies_strictly(tsa: TsaNode, envelope: bytes) -> None:
    result = check_timestamp(_tsr_for(tsa, envelope), envelope)
    assert result.ok and result.strict, result.reason


def test_changing_the_signature_breaks_the_timestamp(tsa: TsaNode, envelope: bytes) -> None:
    """The DSSE signature is inside what the TSA attested."""
    tsr = _tsr_for(tsa, envelope)
    env = json.loads(envelope)
    sig = env["signatures"][0]["sig"]
    env["signatures"][0]["sig"] = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert verify_timestamp(tsr, json.dumps(env, separators=(",", ":")).encode()) is False


# ---------------------------------------------------------------------------
# The gap: tokens that are not bound to the envelope must fail
# ---------------------------------------------------------------------------


def test_imprint_swapped_inside_a_genuine_token_is_rejected(
    tsa: TsaNode, envelope: bytes
) -> None:
    other = b'{"payload":"something else entirely"}'
    genuine_for_other = _tsr_for(tsa, other)
    forged = genuine_for_other.replace(
        hashlib.sha256(other).digest(), hashlib.sha256(envelope).digest()
    )
    assert forged != genuine_for_other
    assert verify_timestamp(forged, envelope) is False


def test_message_digest_not_matching_tstinfo_is_rejected(
    tsa: TsaNode, envelope: bytes
) -> None:
    assert verify_timestamp(_tsr_for(tsa, envelope, wrong_message_digest=True), envelope) is False


def test_tsa_signature_tampered_is_rejected(tsa: TsaNode, envelope: bytes) -> None:
    assert verify_timestamp(_tsr_for(tsa, envelope, tamper_signature=True), envelope) is False


def test_unsigned_status_plus_hash_is_rejected(envelope: bytes) -> None:
    """Granted status + a bare OCTET STRING of the hash: no TSA signed anything."""
    digest = hashlib.sha256(envelope).digest()
    body = b"\x30\x03\x02\x01\x00" + b"\x04\x20" + digest
    tsr = b"\x30" + bytes([len(body)]) + body
    assert verify_timestamp(tsr, envelope) is False


def test_token_for_other_data_is_rejected(tsa: TsaNode, envelope: bytes) -> None:
    assert verify_timestamp(_tsr_for(tsa, b"other"), envelope) is False


# ---------------------------------------------------------------------------
# Real-world tokens take the strict path
# ---------------------------------------------------------------------------


def test_real_freetsa_token_is_strictly_parsed() -> None:
    """A real TSA's token reaches the strict checks (and fails only the imprint here,
    because the bytes it timestamped are not part of the fixture)."""
    result = check_timestamp(FREETSA.read_bytes(), b"not the timestamped data")
    assert result.strict
    assert not result.ok
    assert "messageImprint" in result.reason
