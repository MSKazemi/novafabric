"""Strict, bounded parser for RFC 3161 time-stamp tokens (ADR-0070 §1, experimental).

``cryptography`` can load the certificates of a PKCS#7 blob but exposes no CMS
``SignedData`` *verification* API, and no ASN.1 library (``asn1crypto``, ``pyasn1``)
is a NovaFabric runtime dependency. This module therefore walks the few DER
structures RFC 3161 / RFC 5652 / RFC 5816 define for a time-stamp token, and nothing
else, so :mod:`novafabric.trust.novaseal.tsa_trust` can verify the TSA's signature with
``cryptography`` primitives.

It is deliberately *strict*, unlike the best-effort scanners in
:mod:`novafabric.trust.novaseal.timestamp` (which it does not replace):

* fields are read **positionally** per the ASN.1 definitions — never by scanning for
  a byte pattern — and trailing bytes after any structure are an error;
* BER-only encodings (indefinite lengths, non-minimal long-form lengths,
  constructed OCTET STRINGs, high tag numbers) are rejected;
* every count is capped (certificates, attributes, children) and the input size is
  bounded, so a hostile token cannot cause unbounded work.

Any deviation raises :class:`TsaTokenError`; callers treat that as a verification
failure (fail closed).

Accepted input is either a DER ``TimeStampResp`` (what NovaFabric stores as
``manifest.dsse.tsr``; its ``PKIStatus`` must be granted/grantedWithMods) or a bare
DER ``TimeStampToken`` (CMS ``ContentInfo``).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from cryptography import x509

__all__ = [
    "MAX_TOKEN_BYTES",
    "OID_CONTENT_TYPE",
    "OID_MESSAGE_DIGEST",
    "OID_SIGNED_DATA",
    "OID_SIGNING_CERTIFICATE",
    "OID_SIGNING_CERTIFICATE_V2",
    "OID_TST_INFO",
    "AlgorithmIdentifier",
    "EssCertId",
    "MessageImprint",
    "SignerInfo",
    "TimeStampToken",
    "TsaTokenError",
    "TstInfo",
    "parse_timestamp_token",
]

#: Largest token accepted (1 MiB). Real TSRs are a few KiB (freetsa.org: ~4.6 KiB).
MAX_TOKEN_BYTES = 1024 * 1024
_MAX_CHILDREN = 256
_MAX_CERTIFICATES = 32
_MAX_OID_BYTES = 64

OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
OID_SIGNING_CERTIFICATE = "1.2.840.113549.1.9.16.2.12"
OID_SIGNING_CERTIFICATE_V2 = "1.2.840.113549.1.9.16.2.47"
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"

_TAG_BOOLEAN = 0x01
_TAG_INTEGER = 0x02
_TAG_OCTET_STRING = 0x04
_TAG_OID = 0x06
_TAG_GENERALIZED_TIME = 0x18
_TAG_SEQUENCE = 0x30
_TAG_SET = 0x31
_TAG_CTX0_CONS = 0xA0
_TAG_CTX1_CONS = 0xA1
_TAG_CTX0_PRIM = 0x80

_GENTIME_RE = re.compile(rb"^(\d{14})(?:\.(\d{1,9}))?Z\Z")


class TsaTokenError(ValueError):
    """The bytes are not a well-formed, supported RFC 3161 time-stamp token."""


# ---------------------------------------------------------------------------
# Strict DER primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tlv:
    """One DER element: its tag byte, content octets and complete encoding."""

    tag: int
    value: bytes
    raw: bytes


def _read(buf: bytes, pos: int) -> tuple[_Tlv, int]:
    """Read the DER element starting at ``pos``; return it and the next offset."""
    if pos + 2 > len(buf):
        raise TsaTokenError("truncated DER element")
    tag = buf[pos]
    if tag & 0x1F == 0x1F:
        raise TsaTokenError("high-tag-number DER form is not supported")
    first = buf[pos + 1]
    cursor = pos + 2
    if first < 0x80:
        length = first
    else:
        n_bytes = first & 0x7F
        if n_bytes == 0:
            raise TsaTokenError("indefinite-length (BER) encoding is not DER")
        if n_bytes > 4 or cursor + n_bytes > len(buf):
            raise TsaTokenError("invalid DER length field")
        length_bytes = buf[cursor : cursor + n_bytes]
        # X.690 §10.1: DER lengths use the minimum number of octets — no leading
        # 0x00 octet, and the long form only for lengths >= 128. Anything else makes
        # the token bytes malleable without changing what they parse to.
        if length_bytes[0] == 0x00:
            raise TsaTokenError("non-minimal DER length (leading zero octet)")
        length = int.from_bytes(length_bytes, "big")
        if length < 0x80:
            raise TsaTokenError("non-minimal DER length (long form for a length < 128)")
        cursor += n_bytes
    end = cursor + length
    if end > len(buf):
        raise TsaTokenError("DER length runs past the end of the input")
    return _Tlv(tag=tag, value=buf[cursor:end], raw=buf[pos:end]), end


def _single(buf: bytes, what: str) -> _Tlv:
    """Parse ``buf`` as exactly one DER element (no trailing bytes)."""
    tlv, end = _read(buf, 0)
    if end != len(buf):
        raise TsaTokenError(f"{what}: trailing bytes after the DER element")
    return tlv


def _children(tlv: _Tlv, what: str) -> list[_Tlv]:
    """Parse the content of a constructed element into its direct children."""
    if not tlv.tag & 0x20:
        raise TsaTokenError(f"{what}: expected a constructed element")
    out: list[_Tlv] = []
    pos = 0
    while pos < len(tlv.value):
        if len(out) >= _MAX_CHILDREN:
            raise TsaTokenError(f"{what}: more than {_MAX_CHILDREN} elements")
        child, pos = _read(tlv.value, pos)
        out.append(child)
    return out


def _expect(tlv: _Tlv, tag: int, what: str) -> _Tlv:
    if tlv.tag != tag:
        raise TsaTokenError(f"{what}: expected tag 0x{tag:02x}, got 0x{tlv.tag:02x}")
    return tlv


def _integer(tlv: _Tlv, what: str) -> int:
    _expect(tlv, _TAG_INTEGER, what)
    if not tlv.value:
        raise TsaTokenError(f"{what}: empty INTEGER")
    return int.from_bytes(tlv.value, "big", signed=True)


def _oid(tlv: _Tlv, what: str) -> str:
    """Decode an OBJECT IDENTIFIER to dotted form (strict, bounded)."""
    _expect(tlv, _TAG_OID, what)
    data = tlv.value
    if not data or len(data) > _MAX_OID_BYTES or data[-1] & 0x80:
        raise TsaTokenError(f"{what}: malformed OBJECT IDENTIFIER")
    arcs: list[int] = []
    value = 0
    fresh = True
    for byte in data:
        if fresh and byte == 0x80:
            raise TsaTokenError(f"{what}: non-minimal OBJECT IDENTIFIER arc")
        value = (value << 7) | (byte & 0x7F)
        fresh = not byte & 0x80
        if fresh:
            arcs.append(value)
            value = 0
    first = min(arcs[0] // 40, 2)
    return ".".join(str(a) for a in [first, arcs[0] - 40 * first, *arcs[1:]])


def _octets(tlv: _Tlv, what: str) -> bytes:
    return _expect(tlv, _TAG_OCTET_STRING, what).value


# ---------------------------------------------------------------------------
# Parsed structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlgorithmIdentifier:
    """``AlgorithmIdentifier``: dotted OID plus raw DER parameters (if any)."""

    oid: str
    parameters: bytes | None = None


@dataclass(frozen=True)
class MessageImprint:
    """``TSTInfo.messageImprint``: the hash algorithm and the time-stamped digest."""

    hash_algorithm: AlgorithmIdentifier
    hashed_message: bytes


@dataclass(frozen=True)
class TstInfo:
    """The fields of ``TSTInfo`` (RFC 3161 §2.4.2) that verification relies on."""

    version: int
    policy: str
    message_imprint: MessageImprint
    serial_number: int
    gen_time: datetime.datetime
    nonce: int | None


@dataclass(frozen=True)
class EssCertId:
    """First ``ESSCertID`` (RFC 2634) or ``ESSCertIDv2`` (RFC 5816) — the signer's."""

    version: int
    hash_algorithm: AlgorithmIdentifier
    cert_hash: bytes
    issuer_serial: int | None


@dataclass(frozen=True)
class SignerInfo:
    """The single CMS ``SignerInfo`` of a time-stamp token.

    ``signed_attrs_der`` is the DER ``SET OF Attribute`` actually covered by the
    signature (the ``[0] IMPLICIT`` tag re-tagged as ``SET``, RFC 5652 §5.4).
    """

    version: int
    sid_issuer_der: bytes | None
    sid_serial: int | None
    sid_key_identifier: bytes | None
    digest_algorithm: AlgorithmIdentifier
    signed_attrs_der: bytes
    signature_algorithm: AlgorithmIdentifier
    signature: bytes
    content_type: str | None
    message_digest: bytes | None
    ess_cert_id: EssCertId | None


@dataclass(frozen=True)
class TimeStampToken:
    """A parsed RFC 3161 token.

    Attributes:
        pki_status: ``PKIStatus`` when the input was a ``TimeStampResp``; ``None`` for
            a bare token.
        tst_info: Parsed ``TSTInfo``.
        econtent: The DER ``TSTInfo`` bytes (the CMS ``eContent``), which the signed
            ``messageDigest`` attribute must hash to.
        certificates: Certificates embedded in ``SignedData.certificates``.
        signer: The single ``SignerInfo``.
    """

    pki_status: int | None
    tst_info: TstInfo
    econtent: bytes
    certificates: tuple[x509.Certificate, ...]
    signer: SignerInfo


# ---------------------------------------------------------------------------
# Structure parsers
# ---------------------------------------------------------------------------


def _algorithm(tlv: _Tlv, what: str) -> AlgorithmIdentifier:
    kids = _children(_expect(tlv, _TAG_SEQUENCE, what), what)
    if not 1 <= len(kids) <= 2:
        raise TsaTokenError(f"{what}: malformed AlgorithmIdentifier")
    params = kids[1].raw if len(kids) == 2 else None
    return AlgorithmIdentifier(oid=_oid(kids[0], what), parameters=params)


def _gen_time(tlv: _Tlv) -> datetime.datetime:
    _expect(tlv, _TAG_GENERALIZED_TIME, "TSTInfo.genTime")
    match = _GENTIME_RE.match(tlv.value)
    if match is None:
        raise TsaTokenError("TSTInfo.genTime: not a UTC GeneralizedTime (YYYYMMDDHHMMSS[.f]Z)")
    try:
        base = datetime.datetime.strptime(match.group(1).decode("ascii"), "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise TsaTokenError(f"TSTInfo.genTime: invalid date: {exc}") from exc
    fraction = match.group(2)
    micros = int(fraction.decode("ascii").ljust(6, "0")[:6]) if fraction else 0
    return base.replace(microsecond=micros, tzinfo=datetime.timezone.utc)


def _tst_info(econtent: bytes) -> TstInfo:
    kids = _children(_expect(_single(econtent, "TSTInfo"), _TAG_SEQUENCE, "TSTInfo"), "TSTInfo")
    if len(kids) < 5:
        raise TsaTokenError("TSTInfo: missing mandatory fields")
    imprint = _children(_expect(kids[2], _TAG_SEQUENCE, "messageImprint"), "messageImprint")
    if len(imprint) != 2:
        raise TsaTokenError("TSTInfo.messageImprint: malformed")
    nonce: int | None = None
    for field in kids[5:]:
        if field.tag == _TAG_INTEGER:
            if nonce is not None:
                raise TsaTokenError("TSTInfo: unexpected second INTEGER after genTime")
            nonce = _integer(field, "TSTInfo.nonce")
        elif field.tag not in (_TAG_SEQUENCE, _TAG_BOOLEAN, _TAG_CTX0_CONS, _TAG_CTX1_CONS):
            raise TsaTokenError(f"TSTInfo: unexpected field tag 0x{field.tag:02x}")
    return TstInfo(
        version=_integer(kids[0], "TSTInfo.version"),
        policy=_oid(kids[1], "TSTInfo.policy"),
        message_imprint=MessageImprint(
            hash_algorithm=_algorithm(imprint[0], "messageImprint.hashAlgorithm"),
            hashed_message=_octets(imprint[1], "messageImprint.hashedMessage"),
        ),
        serial_number=_integer(kids[3], "TSTInfo.serialNumber"),
        gen_time=_gen_time(kids[4]),
        nonce=nonce,
    )


def _issuer_serial(tlv: _Tlv) -> int:
    kids = _children(_expect(tlv, _TAG_SEQUENCE, "IssuerSerial"), "IssuerSerial")
    if len(kids) != 2:
        raise TsaTokenError("IssuerSerial: malformed")
    return _integer(kids[1], "IssuerSerial.serialNumber")


def _ess_cert_id(value: _Tlv, version: int) -> EssCertId:
    """Parse ``SigningCertificate[V2]`` and return its first (signer) ``ESSCertID``."""
    what = f"SigningCertificate{'V2' if version == 2 else ''}"
    outer = _children(_expect(value, _TAG_SEQUENCE, what), what)
    if not 1 <= len(outer) <= 2:
        raise TsaTokenError(f"{what}: malformed")
    ids = _children(_expect(outer[0], _TAG_SEQUENCE, f"{what}.certs"), f"{what}.certs")
    if not ids:
        raise TsaTokenError(f"{what}: empty certs list")
    fields = _children(_expect(ids[0], _TAG_SEQUENCE, "ESSCertID"), "ESSCertID")
    algorithm = AlgorithmIdentifier(oid="1.3.14.3.2.26")  # SHA-1: fixed for ESSCertID v1
    if version == 2:
        algorithm = AlgorithmIdentifier(oid=_OID_SHA256)  # ESSCertIDv2 DEFAULT sha256
        if fields and fields[0].tag == _TAG_SEQUENCE:
            algorithm = _algorithm(fields[0], "ESSCertIDv2.hashAlgorithm")
            fields = fields[1:]
    if not 1 <= len(fields) <= 2:
        raise TsaTokenError("ESSCertID: malformed")
    return EssCertId(
        version=version,
        hash_algorithm=algorithm,
        cert_hash=_octets(fields[0], "ESSCertID.certHash"),
        issuer_serial=_issuer_serial(fields[1]) if len(fields) == 2 else None,
    )


def _signed_attributes(
    tlv: _Tlv,
) -> tuple[str | None, bytes | None, EssCertId | None]:
    """Return ``(contentType, messageDigest, signer ESSCertID)`` from signedAttrs.

    ``ESSCertIDv2`` is preferred when both signingCertificate attributes are present.
    """
    seen: set[str] = set()
    content_type: str | None = None
    digest: bytes | None = None
    ess_v1: EssCertId | None = None
    ess_v2: EssCertId | None = None
    for attr in _children(tlv, "signedAttrs"):
        kids = _children(_expect(attr, _TAG_SEQUENCE, "Attribute"), "Attribute")
        if len(kids) != 2:
            raise TsaTokenError("Attribute: malformed")
        oid = _oid(kids[0], "Attribute.attrType")
        if oid in seen:
            raise TsaTokenError(f"signedAttrs: duplicate attribute {oid}")
        seen.add(oid)
        values = _children(_expect(kids[1], _TAG_SET, "Attribute.attrValues"), "attrValues")
        known = (
            OID_CONTENT_TYPE,
            OID_MESSAGE_DIGEST,
            OID_SIGNING_CERTIFICATE,
            OID_SIGNING_CERTIFICATE_V2,
        )
        if oid in known and len(values) != 1:
            raise TsaTokenError(f"signedAttrs: attribute {oid} must have exactly one value")
        if oid == OID_CONTENT_TYPE:
            content_type = _oid(values[0], "contentType")
        elif oid == OID_MESSAGE_DIGEST:
            digest = _octets(values[0], "messageDigest")
        elif oid == OID_SIGNING_CERTIFICATE:
            ess_v1 = _ess_cert_id(values[0], 1)
        elif oid == OID_SIGNING_CERTIFICATE_V2:
            ess_v2 = _ess_cert_id(values[0], 2)
    return content_type, digest, ess_v2 or ess_v1


def _signer_info(tlv: _Tlv) -> SignerInfo:
    kids = _children(_expect(tlv, _TAG_SEQUENCE, "SignerInfo"), "SignerInfo")
    if len(kids) not in (6, 7):
        raise TsaTokenError(
            "SignerInfo: expected version, sid, digestAlgorithm, signedAttrs, "
            "signatureAlgorithm, signature (signed attributes are mandatory for RFC 3161)"
        )
    if kids[3].tag != _TAG_CTX0_CONS:
        raise TsaTokenError("SignerInfo: signed attributes are missing")
    if len(kids) == 7 and kids[6].tag != _TAG_CTX1_CONS:
        raise TsaTokenError("SignerInfo: unexpected trailing field")
    sid = kids[1]
    issuer_der: bytes | None = None
    serial: int | None = None
    key_id: bytes | None = None
    if sid.tag == _TAG_SEQUENCE:
        parts = _children(sid, "IssuerAndSerialNumber")
        if len(parts) != 2 or parts[0].tag != _TAG_SEQUENCE:
            raise TsaTokenError("SignerInfo.sid: malformed IssuerAndSerialNumber")
        issuer_der = parts[0].raw
        serial = _integer(parts[1], "IssuerAndSerialNumber.serialNumber")
    elif sid.tag == _TAG_CTX0_PRIM:
        key_id = sid.value
    else:
        raise TsaTokenError(f"SignerInfo.sid: unsupported choice 0x{sid.tag:02x}")
    content_type, digest, ess = _signed_attributes(kids[3])
    return SignerInfo(
        version=_integer(kids[0], "SignerInfo.version"),
        sid_issuer_der=issuer_der,
        sid_serial=serial,
        sid_key_identifier=key_id,
        digest_algorithm=_algorithm(kids[2], "SignerInfo.digestAlgorithm"),
        signed_attrs_der=bytes([_TAG_SET]) + kids[3].raw[1:],
        signature_algorithm=_algorithm(kids[4], "SignerInfo.signatureAlgorithm"),
        signature=_octets(kids[5], "SignerInfo.signature"),
        content_type=content_type,
        message_digest=digest,
        ess_cert_id=ess,
    )


def _certificates(tlv: _Tlv) -> tuple[x509.Certificate, ...]:
    certs: list[x509.Certificate] = []
    for choice in _children(tlv, "SignedData.certificates"):
        if choice.tag != _TAG_SEQUENCE:
            continue  # other CertificateChoices (attribute certs, …) are not used
        if len(certs) >= _MAX_CERTIFICATES:
            raise TsaTokenError(f"SignedData: more than {_MAX_CERTIFICATES} certificates")
        try:
            certs.append(x509.load_der_x509_certificate(choice.raw))
        except ValueError as exc:
            raise TsaTokenError(f"SignedData: unparseable certificate: {exc}") from exc
    return tuple(certs)


def _signed_data(content_info: _Tlv) -> tuple[bytes, tuple[x509.Certificate, ...], SignerInfo]:
    kids = _children(_expect(content_info, _TAG_SEQUENCE, "ContentInfo"), "ContentInfo")
    if len(kids) != 2 or _oid(kids[0], "ContentInfo.contentType") != OID_SIGNED_DATA:
        raise TsaTokenError("TimeStampToken: not a CMS SignedData ContentInfo")
    explicit = _children(_expect(kids[1], _TAG_CTX0_CONS, "ContentInfo.content"), "content")
    if len(explicit) != 1:
        raise TsaTokenError("ContentInfo.content: malformed")
    sd = _children(_expect(explicit[0], _TAG_SEQUENCE, "SignedData"), "SignedData")
    if len(sd) < 4:
        raise TsaTokenError("SignedData: missing mandatory fields")
    _integer(sd[0], "SignedData.version")
    _expect(sd[1], _TAG_SET, "SignedData.digestAlgorithms")
    encap = _children(_expect(sd[2], _TAG_SEQUENCE, "encapContentInfo"), "encapContentInfo")
    if len(encap) != 2 or _oid(encap[0], "eContentType") != OID_TST_INFO:
        raise TsaTokenError("encapContentInfo: eContentType is not id-ct-TSTInfo")
    wrapped = _children(_expect(encap[1], _TAG_CTX0_CONS, "eContent"), "eContent")
    if len(wrapped) != 1:
        raise TsaTokenError("eContent: malformed")
    econtent = _octets(wrapped[0], "eContent")
    certs: tuple[x509.Certificate, ...] = ()
    rest = sd[3:]
    if rest and rest[0].tag == _TAG_CTX0_CONS:
        certs = _certificates(rest[0])
        rest = rest[1:]
    if rest and rest[0].tag == _TAG_CTX1_CONS:
        rest = rest[1:]  # crls: not used (revocation comes from the operator directory)
    if len(rest) != 1:
        raise TsaTokenError("SignedData: malformed trailing fields")
    signer_infos = _children(_expect(rest[0], _TAG_SET, "signerInfos"), "signerInfos")
    if len(signer_infos) != 1:
        raise TsaTokenError(
            f"SignedData: RFC 3161 tokens carry exactly one SignerInfo, got {len(signer_infos)}"
        )
    return econtent, certs, _signer_info(signer_infos[0])


def parse_timestamp_token(data: bytes) -> TimeStampToken:
    """Parse a DER ``TimeStampResp`` or bare ``TimeStampToken`` (strict, bounded).

    Args:
        data: DER bytes, at most :data:`MAX_TOKEN_BYTES`.

    Returns:
        The parsed :class:`TimeStampToken`. Parsing verifies **nothing**
        cryptographically; see :func:`novafabric.trust.novaseal.tsa_trust.verify_tsa_trust_chain`.

    Raises:
        TsaTokenError: Oversize, malformed, BER-encoded, not granted, or not a
            time-stamp token.
    """
    if not data:
        raise TsaTokenError("empty time-stamp token")
    if len(data) > MAX_TOKEN_BYTES:
        raise TsaTokenError(f"time-stamp token larger than {MAX_TOKEN_BYTES} bytes")
    outer = _expect(_single(data, "token"), _TAG_SEQUENCE, "token")
    kids = _children(outer, "token")
    status: int | None = None
    content_info = outer
    if kids and kids[0].tag == _TAG_SEQUENCE:  # TimeStampResp { PKIStatusInfo, token }
        status_info = _children(kids[0], "PKIStatusInfo")
        if not status_info:
            raise TsaTokenError("PKIStatusInfo: empty")
        status = _integer(status_info[0], "PKIStatus")
        if status not in (0, 1):
            raise TsaTokenError(f"TimeStampResp PKIStatus={status} is not granted")
        if len(kids) != 2:
            raise TsaTokenError("TimeStampResp carries no TimeStampToken")
        content_info = kids[1]
    econtent, certs, signer = _signed_data(content_info)
    return TimeStampToken(
        pki_status=status,
        tst_info=_tst_info(econtent),
        econtent=econtent,
        certificates=certs,
        signer=signer,
    )
