"""In-test RFC 3161 TSA builder for the ADR-0070 §1 trust-chain tests.

Issues a TSA certificate (critical id-kp-timeStamping EKU by default) under an
in-memory CA from :mod:`tests.seal._x509_pki` and hand-encodes DER ``TimeStampResp``
tokens signed by it — nothing on disk can expire and no TSA is contacted. Knobs let
each test break exactly one property (EKU, ESSCertID, messageDigest, signature, …).
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ._x509_pki import NOW, Node

OID_SHA1 = "1.3.14.3.2.26"
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_SHA512 = "2.16.840.1.101.3.4.2.3"
OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"
OID_RSA_SHA256 = "1.2.840.113549.1.1.11"
OID_RSA_ENCRYPTION = "1.2.840.113549.1.1.1"
OID_RSA_PSS = "1.2.840.113549.1.1.10"
OID_ED25519 = "1.3.101.112"
OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
OID_DATA = "1.2.840.113549.1.7.1"
_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
_OID_SIGNING_CERT = "1.2.840.113549.1.9.16.2.12"
_OID_SIGNING_CERT_V2 = "1.2.840.113549.1.9.16.2.47"

AnyKey = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


# ---------------------------------------------------------------------------
# DER encoding
# ---------------------------------------------------------------------------


def tlv(tag: int, value: bytes) -> bytes:
    n = len(value)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + value


def seq(*parts: bytes) -> bytes:
    return tlv(0x30, b"".join(parts))


def der_set(*parts: bytes) -> bytes:
    return tlv(0x31, b"".join(sorted(parts)))


def integer(n: int) -> bytes:
    length = max(1, (n.bit_length() + 8) // 8)
    return tlv(0x02, n.to_bytes(length, "big", signed=True))


def octets(data: bytes) -> bytes:
    return tlv(0x04, data)


def oid(dotted: str) -> bytes:
    arcs = [int(a) for a in dotted.split(".")]
    body = bytearray()
    for arc in [arcs[0] * 40 + arcs[1], *arcs[2:]]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body.extend(reversed(chunk))
    return tlv(0x06, bytes(body))


def alg(dotted: str, null: bool = True) -> bytes:
    return seq(oid(dotted), tlv(0x05, b"")) if null else seq(oid(dotted))


def gentime(when: datetime.datetime) -> bytes:
    return tlv(0x18, when.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TsaNode:
    """A TSA certificate plus its private key (any supported key type)."""

    cert: x509.Certificate
    key: AnyKey

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(Encoding.PEM)


def make_tsa_cert(
    issuer: Node,
    *,
    cn: str = "novaseal-test-tsa",
    eku: tuple[x509.ObjectIdentifier, ...] | None = (ExtendedKeyUsageOID.TIME_STAMPING,),
    eku_critical: bool = True,
    digital_signature: bool = True,
    key: AnyKey | None = None,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
    crl_urls: tuple[str, ...] = (),
    minimal: bool = False,
) -> TsaNode:
    """Issue a TSA end-entity certificate signed by ``issuer``."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or NOW - datetime.timedelta(days=1))
        .not_valid_after(not_after or NOW + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if not minimal:  # minimal: no keyUsage / SKI / AKI (exercises the optional paths)
        builder = (
            builder.add_extension(
                x509.KeyUsage(
                    digital_signature=digital_signature,
                    content_commitment=False,
                    key_encipherment=not digital_signature,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key()),
                critical=False,
            )
        )
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=eku_critical)
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
    return TsaNode(cert=builder.sign(issuer.key, hashes.SHA256()), key=key)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass
class TokenSpec:
    """Knobs for :func:`make_tsr`; defaults produce a valid ECDSA/SHA-256 token."""

    digest: bytes = field(default_factory=lambda: hashlib.sha256(b"dsse").digest())
    gen_time: datetime.datetime = field(default_factory=lambda: NOW)
    digest_oid: str = OID_SHA256
    imprint_oid: str = OID_SHA256  # messageImprint.hashAlgorithm
    sig_oid: str | None = None  # default derived from the key type
    ess_version: int | None = 2  # 1, 2 or None (no signingCertificate attribute)
    ess_hash_oid: str | None = None  # ESSCertIDv2 hashAlgorithm; None = DEFAULT sha256
    ess_cert: x509.Certificate | None = None  # certificate the ESSCertID hashes
    ess_serial: int | None = None  # issuerSerial.serialNumber to embed
    content_type_oid: str = OID_TST_INFO
    econtent_type_oid: str = OID_TST_INFO
    omit_content_type: bool = False
    omit_message_digest: bool = False
    wrong_message_digest: bool = False
    tamper_tstinfo: bool = False
    tamper_signature: bool = False
    embed_certs: tuple[x509.Certificate, ...] | None = None  # None = [tsa cert]
    sid_by_key_id: bool = False
    nonce: int | None = 0x1234567890ABCDEF
    status: int = 0
    signer_infos: int = 1
    bare_token: bool = False


def _tst_info(spec: TokenSpec) -> bytes:
    fields = [
        integer(1),
        oid("1.2.3.4.1"),
        seq(alg(spec.imprint_oid), octets(spec.digest)),
        integer(0x4F5985),
        gentime(spec.gen_time),
    ]
    if spec.nonce is not None:
        fields.append(integer(spec.nonce))
    return seq(*fields)


def _ess_attr(spec: TokenSpec, cert: x509.Certificate) -> bytes:
    target = spec.ess_cert or cert
    der = target.public_bytes(Encoding.DER)
    issuer_serial = b""
    if spec.ess_serial is not None:
        directory_name = tlv(0xA4, cert.issuer.public_bytes())
        issuer_serial = seq(seq(directory_name), integer(spec.ess_serial))
    if spec.ess_version == 1:
        ess_id = seq(octets(hashlib.sha1(der).digest()), issuer_serial)
        return seq(oid(_OID_SIGNING_CERT), der_set(seq(seq(ess_id))))
    hash_oid = spec.ess_hash_oid
    hashed = hashlib.new(
        {OID_SHA512: "sha512", OID_SHA1: "sha1"}.get(hash_oid or "", "sha256"), der
    )
    alg_part = alg(hash_oid) if hash_oid else b""
    ess_id = seq(alg_part, octets(hashed.digest()), issuer_serial)
    return seq(oid(_OID_SIGNING_CERT_V2), der_set(seq(seq(ess_id))))


def _sign(key: AnyKey, data: bytes, sig_oid: str) -> bytes:
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.sign(data)
    if isinstance(key, rsa.RSAPrivateKey):
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return key.sign(data, ec.ECDSA(hashes.SHA256()))


def _default_sig_oid(key: AnyKey) -> str:
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return OID_ED25519
    if isinstance(key, rsa.RSAPrivateKey):
        return OID_RSA_SHA256
    return OID_ECDSA_SHA256


def make_tsr(tsa: TsaNode, spec: TokenSpec | None = None) -> bytes:
    """Encode a DER ``TimeStampResp`` (or bare token) signed by ``tsa``."""
    spec = spec or TokenSpec()
    tst = _tst_info(spec)
    hash_name = {OID_SHA512: "sha512", OID_SHA1: "sha1"}.get(spec.digest_oid, "sha256")
    md = hashlib.new(hash_name, tst).digest()
    if spec.wrong_message_digest:
        md = bytes(len(md))
    attrs: list[bytes] = []
    if not spec.omit_content_type:
        attrs.append(seq(oid(_OID_CONTENT_TYPE), der_set(oid(spec.content_type_oid))))
    if not spec.omit_message_digest:
        attrs.append(seq(oid(_OID_MESSAGE_DIGEST), der_set(octets(md))))
    if spec.ess_version is not None:
        attrs.append(_ess_attr(spec, tsa.cert))
    signed_set = der_set(*attrs)
    sig_oid = spec.sig_oid or _default_sig_oid(tsa.key)
    signature = _sign(tsa.key, signed_set, sig_oid)
    if spec.tamper_signature:
        signature = signature[:-1] + bytes([signature[-1] ^ 0x01])
    if spec.sid_by_key_id:
        ski = tsa.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        sid = tlv(0x80, ski.digest)
    else:
        sid = seq(tsa.cert.issuer.public_bytes(), integer(tsa.cert.serial_number))
    signer_info = seq(
        integer(3 if spec.sid_by_key_id else 1),
        sid,
        alg(spec.digest_oid),
        b"\xa0" + signed_set[1:],
        alg(sig_oid, null=sig_oid.startswith("1.2.840.113549")),
        octets(signature),
    )
    if spec.tamper_tstinfo:
        tst = tst.replace(integer(0x4F5985), integer(0x4F5986))
    certs = spec.embed_certs if spec.embed_certs is not None else (tsa.cert,)
    cert_field = tlv(0xA0, b"".join(c.public_bytes(Encoding.DER) for c in certs)) if certs else b""
    signed_data = seq(
        integer(3),
        der_set(alg(spec.digest_oid)),
        seq(oid(spec.econtent_type_oid), tlv(0xA0, octets(tst))),
        cert_field,
        tlv(0x31, signer_info * spec.signer_infos),
    )
    token = seq(oid(_OID_SIGNED_DATA), tlv(0xA0, signed_data))
    if spec.bare_token:
        return token
    return seq(seq(integer(spec.status)), token)
