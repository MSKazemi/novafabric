"""RFC 3161 trusted timestamp support for Evidence Bundles.

Implements ADR-0030: RFC 3161 Trusted Timestamps for the Evidence Bundle.

This module builds a minimal RFC 3161 TimeStampReq DER, POSTs it to a
Timestamp Authority (TSA), and returns the raw DER TimeStampResponse for
storage in the Evidence Bundle.

DER encoding reference: RFC 3161 §2.4.1 (TimeStampReq), §2.4.2 (TimeStampResp).

The TimeStampReq structure (RFC 3161 §2.4.1):

    TimeStampReq ::= SEQUENCE {
       version                      INTEGER  { v1(1) },
       messageImprint               MessageImprint,
       -- a hash algorithm OID and the hash value of the data to be
       -- time-stamped
       reqPolicy             TSAPolicyId              OPTIONAL,
       nonce                 INTEGER                  OPTIONAL,
       certReq               BOOLEAN                  DEFAULT FALSE,
       extensions            [0] IMPLICIT Extensions   OPTIONAL
    }

    MessageImprint ::= SEQUENCE  {
         hashAlgorithm                AlgorithmIdentifier,
         hashedMessage                OCTET STRING  }

    AlgorithmIdentifier ::= SEQUENCE {
         algorithm               OBJECT IDENTIFIER,
         parameters              ANY DEFINED BY algorithm OPTIONAL  }

The TimeStampResp structure (RFC 3161 §2.4.2):

    TimeStampResp ::= SEQUENCE  {
       status                  PKIStatusInfo,
       timeStampToken          TimeStampToken     OPTIONAL  }

    PKIStatusInfo ::= SEQUENCE {
       status        PKIStatus,
       statusString  PKIFreeText     OPTIONAL,
       failInfo      PKIFailureInfo  OPTIONAL  }

    PKIStatus ::= INTEGER {
       granted                (0),
       grantedWithMods        (1),
       rejection              (2),
       waiting                (3),
       revocationWarning      (4),
       revocationNotification (5)
    }

We build the request using only the Python standard library's struct + os.urandom,
and the cryptography package (already a runtime dependency) — no new deps needed.

The response is parsed minimally: we extract just the PKIStatus INTEGER from the
outermost SEQUENCE > first SEQUENCE (PKIStatusInfo) > first INTEGER (status).
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
import struct
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Sequence

import httpx

if TYPE_CHECKING:
    from cryptography import x509 as _x509

logger = logging.getLogger(__name__)

# SHA-256 OID: 2.16.840.1.101.3.4.2.1  (id-sha256)
# DER-encoded OID bytes (tag 0x06 + length + OID content):
_SHA256_OID_BYTES: Final[bytes] = bytes([
    0x06, 0x09,  # OID tag, 9-byte content
    0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01,
])

# TSA_CONTENT_TYPE for the HTTP request
_TSQ_CONTENT_TYPE: Final[str] = "application/timestamp-query"
_TSR_CONTENT_TYPE: Final[str] = "application/timestamp-reply"

# Default public TSA — free, non-eIDAS-qualified (see ADR-0030)
DEFAULT_TSA_URL: Final[str] = "https://freetsa.org/tsr"

# HTTP timeout: 30 s is generous for public TSAs (typical round-trip < 2 s)
_HTTP_TIMEOUT: Final[float] = 30.0


class TimestampError(Exception):
    """Raised when RFC 3161 timestamping fails.

    Follows the NovaFabric convention of named exception classes per module.
    """


# ---------------------------------------------------------------------------
# DER primitives — minimal subset sufficient for TimeStampReq
# ---------------------------------------------------------------------------


def _der_length(n: int) -> bytes:
    """Encode *n* as a DER definite-length field."""
    if n < 0x80:
        return bytes([n])
    # Long form
    length_bytes = []
    tmp = n
    while tmp:
        length_bytes.append(tmp & 0xFF)
        tmp >>= 8
    length_bytes.reverse()
    return bytes([0x80 | len(length_bytes)] + length_bytes)


def _der_tlv(tag: int, value: bytes) -> bytes:
    """Wrap *value* in DER TLV with *tag*."""
    return bytes([tag]) + _der_length(len(value)) + value


def _der_sequence(contents: bytes) -> bytes:
    """Wrap *contents* in a DER SEQUENCE (tag 0x30)."""
    return _der_tlv(0x30, contents)


def _der_integer(n: int) -> bytes:
    """Encode a non-negative integer *n* as a DER INTEGER (tag 0x02)."""
    if n == 0:
        return _der_tlv(0x02, b"\x00")
    length = (n.bit_length() + 8) // 8  # +8 to include sign byte if needed
    raw = n.to_bytes(length, "big")
    # Trim leading 0x00 bytes that aren't needed for sign disambiguation
    while len(raw) > 1 and raw[0] == 0x00 and (raw[1] & 0x80) == 0:
        raw = raw[1:]
    # Prepend 0x00 if high bit set (two's-complement sign)
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der_tlv(0x02, raw)


def _der_boolean_true() -> bytes:
    """Encode DER BOOLEAN TRUE (tag 0x01, value 0xFF per DER)."""
    return _der_tlv(0x01, b"\xff")


def _der_octet_string(data: bytes) -> bytes:
    """Encode DER OCTET STRING (tag 0x04)."""
    return _der_tlv(0x04, data)


def _der_null() -> bytes:
    """Encode DER NULL (tag 0x05) — used as AlgorithmIdentifier parameters."""
    return _der_tlv(0x05, b"")


# ---------------------------------------------------------------------------
# Build TimeStampReq
# ---------------------------------------------------------------------------


def _build_timestamp_request(digest: bytes) -> tuple[bytes, int]:
    """Build a DER-encoded RFC 3161 TimeStampReq for *digest* (SHA-256 bytes).

    Structure:
        TimeStampReq ::= SEQUENCE {
            version         INTEGER { v1(1) }
            messageImprint  MessageImprint
            nonce           INTEGER  (random 64-bit)
            certReq         BOOLEAN  TRUE
        }

    Returns:
        (tsq_bytes, nonce_int) — the DER-encoded request and the nonce value
        used, so the caller can verify the TSR echoes the same nonce.
    """
    # AlgorithmIdentifier for SHA-256: SEQUENCE { OID, NULL }
    algorithm_identifier = _der_sequence(_SHA256_OID_BYTES + _der_null())

    # MessageImprint: SEQUENCE { AlgorithmIdentifier, OCTET STRING }
    message_imprint = _der_sequence(
        algorithm_identifier + _der_octet_string(digest)
    )

    # version INTEGER { v1(1) }
    version = _der_integer(1)

    # nonce: random 64-bit unsigned integer
    nonce_int = struct.unpack(">Q", os.urandom(8))[0]
    nonce = _der_integer(nonce_int)

    # certReq BOOLEAN TRUE
    cert_req = _der_boolean_true()

    # Assemble TimeStampReq SEQUENCE
    return _der_sequence(version + message_imprint + nonce + cert_req), nonce_int


# ---------------------------------------------------------------------------
# Parse PKIStatus and nonce from TimeStampResp
# ---------------------------------------------------------------------------


# id-ct-TSTInfo (RFC 3161 §2.4.2): 1.2.840.113549.1.9.16.1.4, as a DER OID TLV.
_TSTINFO_OID_TLV = bytes(
    [0x06, 0x0B, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x10, 0x01, 0x04]
)


def _read_tlv(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """Read one DER TLV at ``pos``; return ``(tag, value, next_pos)``."""
    tag = buf[pos]
    pos += 1
    first = buf[pos]
    pos += 1
    if first & 0x80 == 0:
        length = first
    else:
        n_bytes = first & 0x7F
        length = int.from_bytes(buf[pos : pos + n_bytes], "big")
        pos += n_bytes
    end = pos + length
    if end > len(buf):
        raise ValueError("DER length runs past the end of the buffer")
    return tag, buf[pos:end], end


def _extract_nonce_from_tsr(der: bytes) -> int | None:
    """Extract the nonce INTEGER from the TSTInfo of a DER TimeStampResp.

    The nonce is a *positional* field of TSTInfo (RFC 3161 §2.4.2)::

        TSTInfo ::= SEQUENCE {
            version         INTEGER,
            policy          TSAPolicyId,
            messageImprint  MessageImprint,
            serialNumber    INTEGER,
            genTime         GeneralizedTime,
            accuracy        Accuracy   OPTIONAL,
            ordering        BOOLEAN    DEFAULT FALSE,
            nonce           INTEGER    OPTIONAL,
            ... }

    so it is the first INTEGER that follows ``genTime``. This used to be a
    best-effort scan that returned the first 4-to-9-byte INTEGER anywhere in the
    response — which is ``serialNumber``, three fields earlier. Against a real
    TSA the comparison therefore failed on *every* request, always reporting the
    TSA's serial number as the returned nonce and raising "possible replay or
    MITM attack" (observed against freetsa.org: sent 12473090696047252391, read
    14044904342208178637 — the same constant for every distinct nonce sent).
    Timestamping is best-effort, so the error was swallowed and every capsule
    silently shipped with no RFC 3161 token at all.

    Returns ``None`` when the TSR carries no nonce (permitted: the field is
    OPTIONAL) or cannot be parsed — an absent nonce degrades, it does not fail.
    """
    try:
        oid_at = der.find(_TSTINFO_OID_TLV)
        if oid_at < 0:
            return None

        # eContent follows the eContentType OID as [0] EXPLICIT OCTET STRING.
        tag, value, _ = _read_tlv(der, oid_at + len(_TSTINFO_OID_TLV))
        if tag == 0xA0:
            inner_tag, inner_value, _ = _read_tlv(value, 0)
            tstinfo_der = inner_value if inner_tag == 0x04 else value
        elif tag == 0x04:
            tstinfo_der = value
        else:
            return None

        tag, tstinfo, _ = _read_tlv(tstinfo_der, 0)
        if tag != 0x30:  # TSTInfo must be a SEQUENCE
            return None

        pos = 0
        seen_gentime = False
        while pos < len(tstinfo):
            tag, value, pos = _read_tlv(tstinfo, pos)
            if tag == 0x18:  # GeneralizedTime — genTime
                seen_gentime = True
            elif tag == 0x02 and seen_gentime:
                return int.from_bytes(value, "big", signed=True)
        return None
    except (IndexError, ValueError):
        return None


def _parse_pki_status(der: bytes) -> int:
    """Extract the PKIStatus INTEGER from a DER-encoded TimeStampResp.

    TimeStampResp ::= SEQUENCE {
        status          PKIStatusInfo,   <- first child
        timeStampToken  OPTIONAL
    }
    PKIStatusInfo ::= SEQUENCE {
        status  PKIStatus,               <- first child (INTEGER)
        ...
    }

    Raises TimestampError on malformed DER.
    """
    try:
        pos = 0

        def read_tlv(buf: bytes, offset: int) -> tuple[int, int, bytes]:
            """Return (tag, next_offset, value_bytes)."""
            tag = buf[offset]
            offset += 1
            first = buf[offset]
            offset += 1
            if first & 0x80 == 0:
                length = first
            else:
                n_bytes = first & 0x7F
                length = int.from_bytes(buf[offset:offset + n_bytes], "big")
                offset += n_bytes
            return tag, offset + length, buf[offset:offset + length]

        # Outer SEQUENCE (TimeStampResp)
        outer_tag, _, outer_value = read_tlv(der, pos)
        if outer_tag != 0x30:
            raise TimestampError(
                f"TimeStampResp: expected SEQUENCE (0x30), got 0x{outer_tag:02x}"
            )

        # First child: PKIStatusInfo (SEQUENCE)
        pki_tag, _, pki_value = read_tlv(outer_value, 0)
        if pki_tag != 0x30:
            raise TimestampError(
                f"PKIStatusInfo: expected SEQUENCE (0x30), got 0x{pki_tag:02x}"
            )

        # First child of PKIStatusInfo: PKIStatus (INTEGER)
        status_tag, status_end, status_value = read_tlv(pki_value, 0)
        if status_tag != 0x02:
            raise TimestampError(
                f"PKIStatus: expected INTEGER (0x02), got 0x{status_tag:02x}"
            )

        return int.from_bytes(status_value, "big") if status_value else 0

    except TimestampError:
        raise
    except Exception as exc:
        raise TimestampError(f"failed to parse TimeStampResp DER: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_rfc3161_timestamp(dsse_bytes: bytes, tsa_url: str) -> bytes:
    """Request an RFC 3161 timestamp from *tsa_url* for *dsse_bytes*.

    Computes SHA-256 of *dsse_bytes*, builds a DER TimeStampReq, POSTs to
    *tsa_url*, verifies PKIStatus is granted (0) or grantedWithMods (1),
    validates that the nonce in the TSR matches the nonce sent in the request
    (replay protection), and returns the raw DER TimeStampResponse bytes.

    Args:
        dsse_bytes: Raw bytes of the assembled DSSE envelope.
        tsa_url: URL of the RFC 3161 Timestamp Authority endpoint.

    Returns:
        Raw DER-encoded TimeStampResponse bytes for storage in the bundle.

    Raises:
        TimestampError: if the TSA is unreachable, returns a non-OK HTTP
            status, returns a PKIStatus other than granted/grantedWithMods,
            or returns a nonce that does not match the request nonce.
    """
    digest = hashlib.sha256(dsse_bytes).digest()
    ts_request, request_nonce = _build_timestamp_request(digest)

    try:
        response = httpx.post(
            tsa_url,
            content=ts_request,
            headers={"Content-Type": _TSQ_CONTENT_TYPE},
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.TransportError as exc:
        raise TimestampError(f"TSA unreachable at {tsa_url!r}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise TimestampError(f"TSA HTTP error from {tsa_url!r}: {exc}") from exc

    if response.status_code != 200:
        raise TimestampError(
            f"TSA returned HTTP {response.status_code} from {tsa_url!r}"
        )

    tsr_der = response.content
    if not tsr_der:
        raise TimestampError(f"TSA returned empty response from {tsa_url!r}")

    pki_status = _parse_pki_status(tsr_der)
    if pki_status not in (0, 1):
        # 0 = granted, 1 = grantedWithMods — both are success per RFC 3161 §2.4.2
        _STATUS_NAMES = {
            2: "rejection",
            3: "waiting",
            4: "revocationWarning",
            5: "revocationNotification",
        }
        status_name = _STATUS_NAMES.get(pki_status, f"unknown({pki_status})")
        raise TimestampError(
            f"TSA at {tsa_url!r} returned PKIStatus={pki_status} ({status_name}); "
            "expected granted(0) or grantedWithMods(1)"
        )

    # Nonce replay protection (ADR-0030 v0.2 item): verify the TSR nonce
    # echoes the nonce we sent.  If the TSR omits the nonce field entirely
    # (some TSAs do this), we degrade gracefully rather than failing — the
    # absence of a nonce in the response is less dangerous than a mismatched
    # nonce.  If the nonce IS present and mismatches, we raise immediately.
    tsr_nonce = _extract_nonce_from_tsr(tsr_der)
    if tsr_nonce is not None and tsr_nonce != request_nonce:
        raise TimestampError(
            f"nonce mismatch: sent {request_nonce}, TSR returned {tsr_nonce}; "
            "possible replay or MITM attack"
        )

    return tsr_der


def add_rfc3161_timestamp_with_fallback(
    dsse_bytes: bytes, tsa_urls: Sequence[str]
) -> tuple[bytes, str]:
    """Try each TSA in *tsa_urls*, in order, falling through on failure.

    REG-ADR-007: production/EU-regulated and air-gapped deployments configure
    a fallback list of TSAs so a single unreachable/misconfigured TSA does
    not block sealing. Returns ``(tsr_der, url_that_succeeded)`` so callers
    can log/record which TSA actually issued the token.

    Args:
        dsse_bytes: Raw bytes of the assembled DSSE envelope.
        tsa_urls:   Ordered list of TSA endpoints; tried in order.

    Raises:
        TimestampError: if *tsa_urls* is empty, or if every URL fails — the
            error raised is the one from the *last* URL tried.
    """
    if not tsa_urls:
        raise TimestampError("add_rfc3161_timestamp_with_fallback: no TSA URLs configured")

    last_exc: TimestampError | None = None
    for url in tsa_urls:
        try:
            return add_rfc3161_timestamp(dsse_bytes, url), url
        except TimestampError as exc:
            logger.warning("TSA %r failed (%s); trying next fallback TSA", url, exc)
            last_exc = exc
            continue

    assert last_exc is not None  # tsa_urls is non-empty, so the loop ran at least once
    raise last_exc


# ---------------------------------------------------------------------------
# TSA trust chain + CRL/OCSP verification
# ---------------------------------------------------------------------------

_REVOCATION_GOOD = "good"
_REVOCATION_REVOKED = "revoked"
_REVOCATION_UNKNOWN = "unknown"

# Timeout for OCSP/CRL network calls (3 s per spec)
_REVOCATION_TIMEOUT: Final[float] = 3.0


@dataclass
class TsaChainResult:
    """Result of TSA trust-chain and revocation verification.

    Attributes:
        chain_ok: True if the signing cert chain is trusted (or could not be
            checked due to missing cert, in which case a note is in ``errors``).
        revocation_status: ``"good"`` | ``"revoked"`` | ``"unknown"``.
            ``"unknown"`` is used when the OCSP responder / CRL is unreachable.
        ocsp_checked: True if an OCSP round-trip was attempted AND returned a
            definitive response.
        errors: Human-readable notes about any degraded checks.
    """

    chain_ok: bool
    revocation_status: str
    ocsp_checked: bool
    errors: list[str] = field(default_factory=list)


def _extract_signing_cert_from_tsr(token_der: bytes) -> "object | None":
    """Extract the first certificate from a CMS SignedData TSR DER.

    Returns a ``cryptography.x509.Certificate`` or ``None`` on failure.
    """
    try:
        from cryptography.x509 import load_der_x509_certificate

        # CMS SignedData is wrapped: SEQUENCE { OID, [0] EXPLICIT SEQUENCE {...} }
        # We use a brute-force walk: find the first SEQUENCE with tag 0x30 that
        # looks like a DER X.509 certificate (starts with 0x30 and contains
        # subjectPublicKeyInfo OID bytes typical of a cert).
        # A proper parser would chase: TSR SEQUENCE → TimeStampToken → CMS
        # SignedData → certificates [0] IMPLICIT SET → first cert.
        # For now we scan for 0x30 0x82 patterns (SEQUENCE, 2-byte length ≥ 64 B).
        pos = 0
        buf = token_der
        # We'll try to locate certificate-like DER blobs (certificates are
        # typically 200-3000 bytes, SEQUENCE tag 0x30, then length).
        while pos < len(buf) - 4:
            if buf[pos] == 0x30:
                # Try to parse a length here
                try:
                    first = buf[pos + 1]
                    if first & 0x80 == 0:
                        length = first
                        content_start = pos + 2
                    else:
                        n_bytes = first & 0x7F
                        if n_bytes == 0 or pos + 2 + n_bytes > len(buf):
                            pos += 1
                            continue
                        length = int.from_bytes(buf[pos + 2:pos + 2 + n_bytes], "big")
                        content_start = pos + 2 + n_bytes
                    total = content_start + length
                    if 200 <= length <= 8192 and total <= len(buf):
                        candidate = buf[pos:total]
                        try:
                            cert = load_der_x509_certificate(candidate)
                            return cert
                        except Exception:
                            pass
                except (IndexError, ValueError):
                    pass
            pos += 1
        return None
    except ImportError:
        return None


def _get_ocsp_url(cert: "object") -> "str | None":
    """Extract the OCSP responder URL from a cert's AIA extension."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import AuthorityInformationAccessOID

        assert isinstance(cert, x509.Certificate)
        try:
            aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
            for access_desc in aia.value:
                if access_desc.access_method == AuthorityInformationAccessOID.OCSP:
                    loc = access_desc.access_location
                    if isinstance(loc, x509.UniformResourceIdentifier):
                        return loc.value
        except x509.ExtensionNotFound:
            return None
        return None
    except (ImportError, Exception):
        return None


def _get_crl_urls(cert: "object") -> "list[str]":
    """Extract CRL distribution point URLs from a cert."""
    try:
        from cryptography import x509

        assert isinstance(cert, x509.Certificate)
        urls: list[str] = []
        try:
            cdps = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints)
            for cdp in cdps.value:
                if cdp.full_name:
                    for gn in cdp.full_name:
                        if hasattr(gn, "value") and isinstance(gn.value, str):
                            urls.append(gn.value)
        except x509.ExtensionNotFound:
            pass
        return urls
    except (ImportError, Exception):
        return []


def _check_ocsp(cert: "object", errors: list[str]) -> "str":
    """Perform a basic OCSP check. Returns ``"good"`` | ``"revoked"`` | ``"unknown"``."""
    ocsp_url = _get_ocsp_url(cert)
    if not ocsp_url:
        return _REVOCATION_UNKNOWN

    try:
        from cryptography import x509

        assert isinstance(cert, x509.Certificate)

        # Build a minimal OCSP request.  We need the issuer cert to build it
        # properly; without it we can at least do a HEAD to verify reachability.
        req = urllib.request.Request(
            ocsp_url,
            method="HEAD",
        )
        req.add_header("Connection", "close")

        try:
            with urllib.request.urlopen(req, timeout=_REVOCATION_TIMEOUT) as resp:
                status_code = resp.getcode()
            if status_code in (200, 405):  # 405 = HEAD not allowed but server is up
                # Reachable; we cannot determine definitive status without issuer
                # cert, so return unknown with a note rather than failing.
                errors.append(
                    "OCSP responder reachable but issuer cert unavailable — "
                    "status not definitively determined"
                )
                return _REVOCATION_UNKNOWN
        except OSError as exc:
            errors.append(f"OCSP HEAD request failed (timeout/network): {exc}")
            return _REVOCATION_UNKNOWN

    except ImportError:
        errors.append("cryptography package not available for OCSP check")
    except Exception as exc:
        errors.append(f"OCSP check error: {exc}")

    return _REVOCATION_UNKNOWN


def _check_crl_reachability(cert: "object", errors: list[str]) -> "str":
    """HEAD each CRL URL; returns ``"unknown"`` (we don't parse the CRL).

    A full CRL parse is out of scope for this implementation — we just
    verify network reachability within the 3 s timeout.
    """
    urls = _get_crl_urls(cert)
    if not urls:
        return _REVOCATION_UNKNOWN

    for url in urls:
        if not url.startswith(("http://", "https://")):
            continue
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("Connection", "close")
            with urllib.request.urlopen(req, timeout=_REVOCATION_TIMEOUT) as resp:
                if resp.getcode() in (200, 206, 301, 302):
                    # CRL is reachable — we can't determine revocation status
                    # without parsing the CRL DER, so return unknown with note.
                    errors.append(
                        "CRL distribution point reachable but CRL not parsed "
                        "— revocation status not definitively determined"
                    )
                    return _REVOCATION_UNKNOWN
        except OSError as exc:
            errors.append(f"CRL HEAD unreachable ({url!r}): {exc}")

    return _REVOCATION_UNKNOWN


def _build_cert_chain(cert: "object", trusted_ca_pem: str | None) -> "tuple[bool, list[str]]":
    """Attempt to validate cert chain. Returns (chain_ok, errors)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding

        assert isinstance(cert, x509.Certificate)

        # Check if self-signed
        if cert.issuer == cert.subject:
            # Self-signed: if a trusted CA PEM is provided, check membership
            if trusted_ca_pem:
                trusted_certs = _parse_pem_bundle(trusted_ca_pem)
                cert_der = cert.public_bytes(Encoding.DER)
                for tc in trusted_certs:
                    if tc.public_bytes(Encoding.DER) == cert_der:
                        return True, []
                return False, [
                    "Self-signed cert not found in provided trusted_ca_pem bundle"
                ]
            # No trusted CA provided: accept self-signed (degraded trust)
            return True, ["Self-signed cert accepted (no trusted CA PEM provided)"]

        # CA-signed: if trusted CA PEM provided, verify issuer is in bundle
        if trusted_ca_pem:
            issuer_certs = _parse_pem_bundle(trusted_ca_pem)
            for ic in issuer_certs:
                if ic.subject == cert.issuer:
                    # Found issuer — do a basic signature verification
                    try:
                        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
                        sig_hash = cert.signature_hash_algorithm
                        if sig_hash is None:
                            return False, [
                                "Unsupported issuer signature algorithm (no hash)"
                            ]
                        pub = ic.public_key()
                        try:
                            if isinstance(pub, ec.EllipticCurvePublicKey):
                                pub.verify(
                                    cert.signature,
                                    cert.tbs_certificate_bytes,
                                    ec.ECDSA(sig_hash),
                                )
                            elif isinstance(pub, rsa.RSAPublicKey):
                                pub.verify(
                                    cert.signature,
                                    cert.tbs_certificate_bytes,
                                    padding.PKCS1v15(),
                                    sig_hash,
                                )
                            else:
                                return False, [
                                    f"Unsupported issuer key type: {type(pub).__name__}"
                                ]
                            return True, []
                        except Exception as sig_exc:
                            return False, [f"Issuer signature verification failed: {sig_exc}"]
                    except ImportError:
                        return True, ["Issuer found but signature not verified (import error)"]
            return False, ["Issuer cert not found in trusted_ca_pem bundle"]

        # No trusted CA: degrade gracefully
        return True, ["CA-signed cert accepted without chain verification (no trusted CA PEM)"]

    except ImportError:
        return False, ["cryptography package not available for chain validation"]
    except Exception as exc:
        return False, [f"Chain validation error: {exc}"]


def _parse_pem_bundle(pem: str) -> "list[_x509.Certificate]":
    """Parse a PEM bundle (possibly multiple certs) into a list of x509.Certificate."""
    try:
        from cryptography.x509 import load_pem_x509_certificate

        certs: list[_x509.Certificate] = []
        lines = pem.encode() if isinstance(pem, str) else pem
        # Split on BEGIN CERTIFICATE markers
        import re
        pattern = re.compile(
            b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            re.DOTALL,
        )
        for match in pattern.finditer(lines):
            try:
                certs.append(load_pem_x509_certificate(match.group(0)))
            except Exception:
                pass
        return certs
    except (ImportError, Exception):
        return []


def verify_tsa_chain(
    token_der: bytes,
    trusted_ca_pem: str | None = None,
) -> TsaChainResult:
    """Verify the TSA token's signing certificate chain and revocation status.

    Args:
        token_der: Raw DER-encoded TSR (TimeStampResponse) bytes.
        trusted_ca_pem: PEM-encoded CA bundle to trust.  If ``None``, uses
            the system trust store path via ``ssl.get_default_verify_paths()``
            (reads from disk; may be empty in stripped environments — degrades
            gracefully).

    Returns:
        ``TsaChainResult`` with chain/revocation checks populated.
        Never raises; all failures are captured in ``errors``.
    """
    errors: list[str] = []

    # Resolve trusted CA PEM
    effective_pem: str | None = trusted_ca_pem
    if effective_pem is None:
        try:
            vp = ssl.get_default_verify_paths()
            ca_file = vp.cafile or vp.openssl_cafile
            if ca_file:
                try:
                    import pathlib
                    effective_pem = pathlib.Path(ca_file).read_text(errors="replace")
                except OSError as e:
                    errors.append(f"Could not read system CA bundle ({ca_file}): {e}")
        except Exception as e:
            errors.append(f"ssl.get_default_verify_paths() failed: {e}")

    # Extract signing cert
    cert = _extract_signing_cert_from_tsr(token_der)
    if cert is None:
        errors.append(
            "Could not extract signing certificate from TSR "
            "(token may be a stub or use unsupported CMS structure)"
        )
        return TsaChainResult(
            chain_ok=False,
            revocation_status=_REVOCATION_UNKNOWN,
            ocsp_checked=False,
            errors=errors,
        )

    # Chain validation
    chain_ok, chain_errors = _build_cert_chain(cert, effective_pem)
    errors.extend(chain_errors)

    # Revocation: try OCSP first, then CRL as fallback
    ocsp_checked = False
    revocation_status = _REVOCATION_UNKNOWN

    ocsp_status = _check_ocsp(cert, errors)
    if ocsp_status != _REVOCATION_UNKNOWN:
        ocsp_checked = True
        revocation_status = ocsp_status
    else:
        # Fall back to CRL reachability check
        crl_status = _check_crl_reachability(cert, errors)
        revocation_status = crl_status

    return TsaChainResult(
        chain_ok=chain_ok,
        revocation_status=revocation_status,
        ocsp_checked=ocsp_checked,
        errors=errors,
    )
