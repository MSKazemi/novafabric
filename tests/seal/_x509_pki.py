"""In-test PKI builder for ADR-0055 CA-bundle chain-validation tests.

Generates a root CA -> intermediate CA -> leaf hierarchy (ECDSA P-256) entirely in
memory so no fixture certificate on disk can silently expire, plus CRLs for the
ADR-0070 §3 offline revocation tests (:func:`make_crl`).
"""

from __future__ import annotations

import base64
import datetime
import json
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
    crl_sign: bool | None = None,
    crl_urls: tuple[str, ...] = (),
) -> Node:
    """Issue a certificate for a fresh P-256 key (self-signed when ``issuer`` is None).

    ``crl_sign`` overrides the keyUsage cRLSign bit (default: set for CAs);
    ``crl_urls`` adds a CRLDistributionPoints extension.
    """
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
                crl_sign=ca if crl_sign is None else crl_sign,
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
    if crl_urls:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(u) for u in crl_urls],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
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
    """DSSE v1 PAE bytes: "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body."""
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %b %d %b" % (len(t), t, len(payload), payload)


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


# ---------------------------------------------------------------------------
# CRLs (ADR-0070 §3 offline revocation tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Revoked:
    """A CRL entry: serial, revocation instant, optional CRLReason."""

    serial: int
    when: datetime.datetime
    reason: x509.ReasonFlags | None = None


def make_crl(
    issuer: Node,
    revoked: tuple[Revoked, ...] = (),
    *,
    this_update: datetime.datetime | None = None,
    next_update: datetime.datetime | None = None,
    signer_key: ec.EllipticCurvePrivateKey | None = None,
    extensions: tuple[tuple[x509.ExtensionType, bool], ...] = (),
) -> x509.CertificateRevocationList:
    """Build a CRL naming ``issuer``, signed by ``signer_key`` (default: the issuer's)."""
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer.cert.subject)
        .last_update(this_update or NOW - datetime.timedelta(hours=1))
        .next_update(next_update or NOW + datetime.timedelta(days=7))
    )
    for entry in revoked:
        rb = (
            x509.RevokedCertificateBuilder().serial_number(entry.serial).revocation_date(entry.when)
        )
        if entry.reason is not None:
            rb = rb.add_extension(x509.CRLReason(entry.reason), critical=False)
        builder = builder.add_revoked_certificate(rb.build())
    for ext, critical in extensions:
        builder = builder.add_extension(ext, critical=critical)
    return builder.sign(signer_key or issuer.key, hashes.SHA256())


def crl_der(crl: x509.CertificateRevocationList) -> bytes:
    """DER encoding of ``crl``."""
    return crl.public_bytes(Encoding.DER)


def crl_pem(crl: x509.CertificateRevocationList) -> bytes:
    """PEM encoding of ``crl``."""
    return crl.public_bytes(Encoding.PEM)
