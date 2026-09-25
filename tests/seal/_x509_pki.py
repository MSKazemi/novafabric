"""In-test PKI builder for ADR-0055 CA-bundle chain-validation tests.

Generates a root CA -> intermediate CA -> leaf hierarchy (ECDSA P-256) entirely in
memory so no fixture certificate on disk can silently expire.
"""

from __future__ import annotations

import base64
import datetime
import json
import struct
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class Node:
    """A certificate plus its private key."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return self.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def make_cert(
    cn: str,
    *,
    issuer: Node | None,
    ca: bool,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
) -> Node:
    """Issue a certificate for a fresh P-256 key (self-signed when ``issuer`` is None)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    signer_key = issuer.key if issuer is not None else key
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.cert.subject if issuer is not None else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or NOW - datetime.timedelta(days=1))
        .not_valid_after(not_after or NOW + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()),
            critical=False,
        )
    )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False
        )
    return Node(cert=builder.sign(signer_key, hashes.SHA256()), key=key)


@dataclass(frozen=True)
class Pki:
    """A root -> intermediate -> leaf hierarchy."""

    root: Node
    intermediate: Node
    leaf: Node


def make_pki(prefix: str = "novaseal") -> Pki:
    """Build a fresh three-level PKI."""
    root = make_cert(f"{prefix}-root-ca", issuer=None, ca=True)
    intermediate = make_cert(f"{prefix}-intermediate-ca", issuer=root, ca=True)
    leaf = make_cert(f"{prefix}-signer", issuer=intermediate, ca=False)
    return Pki(root=root, intermediate=intermediate, leaf=leaf)


# ---------------------------------------------------------------------------
# Hand-built DSSE envelopes for signer-binding (forgery) tests
# ---------------------------------------------------------------------------

DSSE_PAYLOAD_TYPE = "application/vnd.novafabric.capsule+json"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def dsse_pae(payload: bytes, payload_type: str = DSSE_PAYLOAD_TYPE) -> bytes:
    """DSSE PAE bytes (spec §2.1)."""
    t = payload_type.encode("utf-8")
    return b"DSSEv1" + struct.pack("<Q", len(t)) + t + struct.pack("<Q", len(payload)) + payload


def ecdsa_entry(node: Node, payload: bytes, *, cert: bool = True) -> dict[str, str]:
    """A genuine entry: ``node``'s key signs the PAE, ``node``'s cert is embedded."""
    entry = {"sig": _b64(node.key.sign(dsse_pae(payload), ec.ECDSA(hashes.SHA256())))}
    if cert:
        entry["cert"] = _b64(node.cert.public_bytes(Encoding.DER))
    return entry


def attacker_entry(payload: bytes, legit_cert: x509.Certificate | None) -> dict[str, str]:
    """PoC forgery: attacker Ed25519 ``pubkey`` + ``sig``, optionally a legit ``cert``.

    ``verify_envelope`` verifies under ``pubkey`` and ignores ``cert``; a chain check
    that only looks at ``cert`` would then vouch for a signature it never checked.
    """
    key = ed25519.Ed25519PrivateKey.generate()
    pub_pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    entry = {"sig": _b64(key.sign(dsse_pae(payload))), "pubkey": _b64(pub_pem)}
    if legit_cert is not None:
        entry["cert"] = _b64(legit_cert.public_bytes(Encoding.DER))
    return entry


def dsse_envelope(payload: bytes, entries: list[dict[str, str]]) -> bytes:
    """Serialise a DSSE envelope with the given signature entries."""
    return json.dumps(
        {"payloadType": DSSE_PAYLOAD_TYPE, "payload": _b64(payload), "signatures": entries}
    ).encode("utf-8")
