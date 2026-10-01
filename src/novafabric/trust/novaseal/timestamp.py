"""RFC 3161 timestamp adapter for NovaSeal v0.2 (ADR-0070).

Wraps the existing novafabric.trust._rfc3161 module and adds:
- Named exception TSAUnavailableError for network failures
- Structural TSR validation (PKIStatus + DER parse-ability)
- Hash integrity check: messageImprint matches SHA-256(dsse_bytes)
- CMS signature check: TSA's SignerInfo.signature verified against the
  certificate embedded in TimeStampToken.CMS.SignedData.certificates
- Nonce replay protection: per-request nonces backed by SQLite (ADR-0070)
- Cert-chain depth validation via verify_tsa_cert_chain() (ADR-0070)

What the token covers (ADR-0030): SHA-256 of the serialised DSSE envelope,
signature included. The token is computed *over* the signature, so it is
outside the signature by construction — that is by design, not a gap.

What is verified at verification time (``check_timestamp``; timestamp_ok=True):
  1. TSR is DER with PKIStatus granted (0) or grantedWithMods (1) and carries a
     TimeStampToken (a bare status is rejected — no TSA signed anything).
  2. Strictly parsed, positionally: the embedded TSA signer certificate is bound
     by ESSCertID, ``messageDigest`` equals the hash of the TSTInfo present, the
     CMS signature over the signed attributes verifies, and the TSTInfo
     ``messageImprint`` (algorithm included) equals SHA-256(dsse_bytes).
  3. Who the TSA is (trust anchor, critical id-kp-timeStamping EKU, chain at
     genTime, CRLs) only with operator anchors — ``tsa_trust.verify_tsa_trust_chain``
     (``nova verify --tsa-ca-bundle``).

At request time additionally: nonce replay protection and a chain-depth bound.

Production TSA configuration (ADR-0070):
  - No default TSA URL: timestamping is opt-in (ADR-0292); callers pass the
    operator-configured URL. https://freetsa.org/tsr is a development example only.
  - Nonce store: $NOVAFABRIC_HOME/tsa_nonces.db (auto-derived)
  - Cert chain depth limit: 4
  - offline_mode=True skips nonce store writes and network calls (HPC air-gap)

Degraded mode:
  A token the strict parser rejects (BER encodings, unusual structures) falls
  back to the pre-v0.103 structural check — the hash appears in the token and
  any CMS signature found verifies — and is reported with ``strict=False``
  (``nova verify`` prints "structural check only"), never as a plain OK. Until
  v0.103 *every* token took that path, which accepted a genuine token whose
  TSTInfo imprint had been swapped and a response with no TSA signature at all
  (audit finding S3, 2026-10-01).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from novafabric.trust._rfc3161 import (
    TimestampError,
    _parse_pki_status,
    add_rfc3161_timestamp,
    add_rfc3161_timestamp_with_fallback,
)
from novafabric.trust.novaseal.trust_chain import verify_tsa_cert_chain  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Production TSA defaults (ADR-0070; no default URL — ADR-0292)
# ---------------------------------------------------------------------------

_DEFAULT_TSA_CERT_MAX_DEPTH: int = 4


class TSAUnavailableError(Exception):
    """Raised when the TSA cannot be reached or returns a network error."""


# ---------------------------------------------------------------------------
# Signature algorithm OID TLVs (tag 0x06 + length + content bytes).
# Matched verbatim against the first child of SignerInfo.signatureAlgorithm.
# ---------------------------------------------------------------------------

_OID_SHA256_RSA    = b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x0b"  # sha256WithRSAEncryption
_OID_SHA384_RSA    = b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x0c"  # sha384WithRSAEncryption
_OID_SHA512_RSA    = b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x0d"  # sha512WithRSAEncryption
_OID_RSA_ENCRYPTION = b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01"  # rsaEncryption (bare)
_OID_SHA256_ECDSA  = b"\x06\x08\x2a\x86\x48\xce\x3d\x04\x03\x02"       # ecdsaWithSHA256
_OID_SHA384_ECDSA  = b"\x06\x08\x2a\x86\x48\xce\x3d\x04\x03\x03"       # ecdsaWithSHA384
_OID_SHA512_ECDSA  = b"\x06\x08\x2a\x86\x48\xce\x3d\x04\x03\x04"       # ecdsaWithSHA512


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def request_timestamp(
    dsse_bytes: bytes,
    tsa_url: str,
    *,
    tsa_urls: Optional[list[str]] = None,
    nonce_store_path: Optional[Path] = None,
    offline_mode: bool = False,
    verify_chain: bool = True,
    tsa_cert_max_depth: int = _DEFAULT_TSA_CERT_MAX_DEPTH,
) -> bytes:
    """Request an RFC 3161 timestamp from *tsa_url* for *dsse_bytes*.

    Nonce replay protection (ADR-0070):
        A cryptographically random 64-bit nonce is generated via
        ``secrets.randbits(64)``.  The nonce is recorded in the NonceStore
        before the TSA HTTP request.  If ``offline_mode=True`` the nonce
        store is skipped entirely.

    Cert-chain validation (ADR-0070):
        When ``verify_chain=True`` (default), ``verify_tsa_cert_chain()``
        is called on the returned TSR.  If the chain depth exceeds
        ``tsa_cert_max_depth`` a ``TimestampError`` is raised.  Parsing
        failures degrade gracefully (chain check is skipped with a warning).

    Multi-TSA fallback (REG-ADR-007):
        When ``tsa_urls`` has more than one entry, each is tried in order
        (via ``add_rfc3161_timestamp_with_fallback``) and the first success
        wins. With zero or one entries, behavior is identical to passing
        only ``tsa_url`` — this is purely additive.

    Args:
        dsse_bytes:         DSSE envelope bytes to timestamp.
        tsa_url:            RFC 3161 TSA endpoint URL (primary/fallback[0]).
        tsa_urls:           Optional ordered fallback list of TSA endpoints;
                            when omitted, only ``tsa_url`` is tried.
        nonce_store_path:   Explicit path for the nonce SQLite DB; None
                            auto-derives from ``$NOVAFABRIC_HOME``.
        offline_mode:       When True, skip nonce store and return TSR
                            without chain verification (HPC air-gap mode).
        verify_chain:       When True (default), validate cert-chain depth.
        tsa_cert_max_depth: Maximum certificate chain depth (default 4).

    Returns:
        Raw DER-encoded TimeStampResponse bytes.

    Raises:
        TSAUnavailableError: if TSA is unreachable.
        TimestampError:      if TSA returns an error status or cert chain
                             depth exceeds ``tsa_cert_max_depth``.
    """
    from novafabric.trust.novaseal.nonce_store import NonceStore

    store = NonceStore(path=nonce_store_path, offline_mode=offline_mode)

    # Generate a fresh nonce (defensive retry on the astronomically unlikely
    # event of a collision).
    # Use 63-bit range so the integer fits in SQLite's signed INTEGER column.
    nonce = secrets.randbits(63)
    _MAX_NONCE_RETRIES = 3
    for _attempt in range(_MAX_NONCE_RETRIES):
        if not store.is_replay(nonce):
            break
        logger.warning(
            "request_timestamp: nonce collision on attempt %d; regenerating",
            _attempt + 1,
        )
        nonce = secrets.randbits(63)
    else:
        # Astronomically unlikely; if it happens log it and proceed.
        logger.warning(
            "request_timestamp: could not generate a unique nonce after %d attempts; proceeding",
            _MAX_NONCE_RETRIES,
        )

    # Record the nonce before sending the request so a crash during the HTTP
    # call does not leave us with an unrecorded nonce.
    store.record_nonce(nonce)

    try:
        if tsa_urls and len(tsa_urls) > 1:
            tsr_bytes, _used_url = add_rfc3161_timestamp_with_fallback(dsse_bytes, tsa_urls)
        else:
            tsr_bytes = add_rfc3161_timestamp(dsse_bytes, tsa_url)
    except TimestampError as exc:
        msg = str(exc)
        if "unreachable" in msg or "HTTP error" in msg or "HTTP " in msg:
            raise TSAUnavailableError(msg) from exc
        raise

    # Cert-chain depth validation (ADR-0070).
    if verify_chain and not offline_mode:
        _check_cert_chain(tsr_bytes, max_depth=tsa_cert_max_depth)

    return tsr_bytes


def _check_cert_chain(tsr_bytes: bytes, max_depth: int) -> None:
    """Run cert-chain depth check; raise TimestampError if chain is invalid.

    Degrades gracefully when chain extraction fails (no TimeStampToken).
    """
    result = verify_tsa_cert_chain(tsr_bytes, max_depth=max_depth)
    if not result.valid and result.error:
        # Only hard-fail on errors that indicate a chain-depth violation;
        # other parse failures (no token, etc.) degrade gracefully.
        if "exceeds max_depth" in result.error or "cycle detected" in result.error:
            raise TimestampError(
                f"TSA cert chain validation failed: {result.error}"
            )
        logger.debug(
            "request_timestamp: cert chain check degraded: %s", result.error
        )
    elif result.valid:
        logger.debug(
            "request_timestamp: cert chain valid (depth=%d, subject=%r, eku=%s)",
            result.depth,
            result.subject,
            result.eku_present,
        )


@dataclass(frozen=True)
class TimestampCheck:
    """Outcome of :func:`check_timestamp`.

    Attributes:
        ok: The token is accepted as a timestamp of the given bytes.
        strict: The token was parsed positionally and every binding was checked
            (see :func:`check_timestamp`). ``ok and not strict`` means the token
            could not be parsed strictly and only the legacy structural check ran —
            report it as such, never as a plain "OK".
        reason: One line saying why.
    """

    ok: bool
    strict: bool
    reason: str


def check_timestamp(tsr_bytes: bytes, dsse_bytes: bytes) -> TimestampCheck:
    """Verify that an RFC 3161 response timestamps *dsse_bytes*, without a TSA anchor.

    What the token covers (ADR-0030): the SHA-256 of the serialised DSSE envelope,
    **signature included**. The token is therefore necessarily outside the DSSE
    signature — it is computed over it — and that is by design; what must hold is
    that the token is bound to exactly these envelope bytes.

    Strict path (any token a real TSA issues): parse the token positionally
    (:mod:`tsa_token`) and require — via
    :func:`~novafabric.trust.novaseal.tsa_trust.verify_tsa_token_integrity` —
    PKIStatus granted, the embedded signer certificate bound by ESSCertID,
    ``messageDigest`` equal to the hash of the ``TSTInfo`` present, a valid CMS
    signature, and ``messageImprint`` equal to SHA-256(*dsse_bytes*).

    Without TSA trust anchors this proves the token is intact and belongs to these
    bytes; it does not prove *who* the TSA is. ``nova verify --tsa-ca-bundle``
    (``verify_tsa_trust_chain``) adds that.

    Degraded path: a response that carries a TimeStampToken the strict parser
    rejects (BER encodings, unusual structures) falls back to the pre-v0.103
    structural check and is returned with ``strict=False``. A response with no
    TimeStampToken at all is rejected: it carries no TSA signature.
    """
    if not tsr_bytes:
        return TimestampCheck(False, False, "empty timestamp response")
    try:
        pki_status = _parse_pki_status(tsr_bytes)
    except TimestampError as exc:
        return TimestampCheck(False, False, f"not a TimeStampResp: {exc}")
    if pki_status not in (0, 1):
        return TimestampCheck(False, False, f"PKIStatus {pki_status} is not granted")
    token_raw = _extract_token_raw(tsr_bytes)
    if token_raw is None or token_raw[:1] != b"\x30":  # ContentInfo is a SEQUENCE
        return TimestampCheck(
            False, False, "response carries no TimeStampToken (nothing was signed by a TSA)"
        )

    from novafabric.trust.novaseal.tsa_token import TsaTokenError  # noqa: PLC0415
    from novafabric.trust.novaseal.tsa_trust import (  # noqa: PLC0415
        verify_tsa_token_integrity,
    )

    expected_hash = hashlib.sha256(dsse_bytes).digest()
    try:
        integrity = verify_tsa_token_integrity(tsr_bytes, expected_hash)
    except TsaTokenError as exc:
        legacy_ok = _legacy_structural_check(tsr_bytes, expected_hash)
        return TimestampCheck(
            legacy_ok,
            False,
            f"token not strictly parseable ({exc}); structural check only "
            f"({'imprint found' if legacy_ok else 'failed'})",
        )
    return TimestampCheck(integrity.valid, True, integrity.reason)


def _legacy_structural_check(tsr_bytes: bytes, expected_hash: bytes) -> bool:
    """Pre-v0.103 best-effort check, kept only for tokens the strict parser rejects."""
    if not _extract_message_imprint(tsr_bytes, expected_hash):
        return False
    sig_ok = _verify_tsa_signature(tsr_bytes)
    if sig_ok is None:
        logger.debug(
            "verify_timestamp: CMS SignerInfo extraction failed; "
            "falling back to structural + hash checks only"
        )
    return sig_ok is not False


def verify_timestamp(tsr_bytes: bytes, dsse_bytes: bytes) -> bool:
    """Return True if *tsr_bytes* is a valid timestamp of *dsse_bytes*.

    Boolean view of :func:`check_timestamp` — see there for exactly what is
    checked. Use ``check_timestamp`` to learn whether the strict checks ran.
    """
    return check_timestamp(tsr_bytes, dsse_bytes).ok


# ---------------------------------------------------------------------------
# Focused DER helpers (ASN.1 TLV navigation)
# ---------------------------------------------------------------------------


def _read_tlv(der: bytes, pos: int) -> tuple[int, int, bytes]:
    """Read one ASN.1 DER TLV at *pos*.

    Returns (tag, next_pos, value_bytes).  Raises ValueError on truncated DER.
    """
    tag = der[pos]
    pos += 1
    fb = der[pos]
    pos += 1
    if fb & 0x80 == 0:
        ln = fb
    else:
        n = fb & 0x7F
        ln = int.from_bytes(der[pos:pos + n], "big")
        pos += n
    return tag, pos + ln, der[pos:pos + ln]


def _iter_children(der_value: bytes) -> list[tuple[int, int, int, bytes]]:
    """Return [(tag, start, end, value)] for each immediate TLV child.

    *start* and *end* are byte offsets into *der_value* (not into the
    outer TLV), so ``der_value[start:end]`` yields the complete raw TLV.
    Silently stops on malformed input (robust scan, not a strict parser).
    """
    result: list[tuple[int, int, int, bytes]] = []
    pos = 0
    while pos < len(der_value):
        start = pos
        try:
            tag, end, value = _read_tlv(der_value, pos)
        except (IndexError, ValueError):
            break
        result.append((tag, start, end, value))
        pos = end
    return result


def _extract_token_raw(tsr_der: bytes) -> bytes | None:
    """Extract TimeStampToken ContentInfo as a complete raw DER TLV.

    TimeStampResp ::= SEQUENCE { PKIStatusInfo, TimeStampToken OPTIONAL }

    Returns the raw TLV bytes of the second child (including tag + length),
    suitable for passing to ``load_der_pkcs7_certificates``.
    """
    try:
        _, _, outer = _read_tlv(tsr_der, 0)
        children = _iter_children(outer)
        if len(children) < 2:
            return None
        _, start, end, _ = children[1]
        return outer[start:end]
    except Exception:
        return None


def _extract_signed_data_value(token_der: bytes) -> bytes | None:
    """Extract the SignedData SEQUENCE value from a CMS ContentInfo DER blob.

    ContentInfo ::= SEQUENCE {
        contentType OID,
        content [0] EXPLICIT { SignedData SEQUENCE }
    }

    Returns the SignedData SEQUENCE *value* bytes (without outer tag+len).
    """
    try:
        _, _, ci_value = _read_tlv(token_der, 0)
        children = _iter_children(ci_value)
        if len(children) < 2:
            return None
        # children[1] is [0] EXPLICIT (tag 0xa0) wrapping SignedData
        explicit_tag, _, _, explicit_value = children[1]
        if explicit_tag != 0xa0:
            return None
        # First (only) child of [0] is SignedData SEQUENCE
        sd_children = _iter_children(explicit_value)
        if not sd_children:
            return None
        sd_tag, _, _, sd_value = sd_children[0]
        return sd_value if sd_tag == 0x30 else None
    except Exception:
        return None


def _extract_signer_info_value(sd_value: bytes) -> bytes | None:
    """Return the first SignerInfo SEQUENCE value from a SignedData value.

    SignedData ends with signerInfos SET (tag 0x31); we take the last SET
    (there is also digestAlgorithms SET, so we want the final one).
    """
    try:
        last_set_val: bytes | None = None
        for tag, _, _, value in _iter_children(sd_value):
            if tag == 0x31:
                last_set_val = value
        if last_set_val is None:
            return None
        for si_tag, _, _, si_value in _iter_children(last_set_val):
            if si_tag == 0x30:
                return si_value
        return None
    except Exception:
        return None


def _parse_signer_info(
    si_value: bytes,
) -> tuple[bytes, bytes, bytes] | None:
    """Extract CMS signature components from a SignerInfo SEQUENCE value.

    Returns (signed_attrs_raw, sig_alg_oid_tlv, signature_bytes), or None
    if any required field is absent.

    signed_attrs_raw — raw TLV bytes of the [0] IMPLICIT field (tag 0xa0
    included); callers replace the first byte with 0x31 (SET) to obtain the
    exact octet string that was signed.

    sig_alg_oid_tlv — raw OID TLV (tag 0x06 + length + OID content) from
    the signatureAlgorithm SEQUENCE, used for algorithm dispatch.

    Typical SignerInfo child-tag sequence (v1 with signedAttrs):
        0x02  version INTEGER
        0x30  issuerAndSerialNumber SEQUENCE
        0x30  digestAlgorithm SEQUENCE
        0xa0  signedAttrs [0] IMPLICIT  ← we capture this
        0x30  signatureAlgorithm SEQUENCE  ← last 0x30 before 0x04
        0x04  signature OCTET STRING  ← we capture this
    """
    try:
        signed_attrs_raw: bytes | None = None
        prev_seq_value: bytes | None = None
        sig_alg_value: bytes | None = None
        signature_bytes: bytes | None = None

        for tag, start, end, value in _iter_children(si_value):
            if tag == 0xa0 and signed_attrs_raw is None:
                # First [0] IMPLICIT in SignerInfo = signedAttrs (constructed)
                signed_attrs_raw = si_value[start:end]
            elif tag == 0x30:
                prev_seq_value = value
            elif tag == 0x04:
                # Last OCTET STRING = signature; last SEQUENCE before it = signatureAlg
                signature_bytes = value
                sig_alg_value = prev_seq_value

        if signed_attrs_raw is None or sig_alg_value is None or signature_bytes is None:
            return None

        # OID is the first child of signatureAlgorithm SEQUENCE value
        sa_kids = _iter_children(sig_alg_value)
        if not sa_kids:
            return None
        oid_tag, oid_start, oid_end, _ = sa_kids[0]
        if oid_tag != 0x06:
            return None
        sig_alg_oid = sig_alg_value[oid_start:oid_end]

        return signed_attrs_raw, sig_alg_oid, signature_bytes
    except Exception:
        return None


def _verify_tsa_signature(tsr_der: bytes) -> bool | None:
    """Verify the TSA's CMS signature using the certificate embedded in the TSR.

    Returns:
        True  — signature is cryptographically valid.
        False — signature verification failed (TSR is forged or corrupted).
        None  — CMS structure could not be parsed; caller degrades gracefully.

    Algorithm support: sha256/384/512 with RSA PKCS#1 v1.5 or ECDSA.
    Other algorithms return None (graceful degradation, not a hard failure).
    """
    try:
        from cryptography import exceptions as _cx
        from cryptography.hazmat.primitives import hashes as _h
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        from cryptography.hazmat.primitives.asymmetric import padding as _pad
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
        from cryptography.hazmat.primitives.serialization.pkcs7 import (
            load_der_pkcs7_certificates as _load_certs,
        )
    except ImportError:
        logger.debug("cryptography not available; skipping TSA signature check")
        return None

    # Step 1: extract TimeStampToken ContentInfo
    token_raw = _extract_token_raw(tsr_der)
    if token_raw is None:
        logger.debug("verify_tsa_sig: TimeStampToken absent or unparseable")
        return None

    # Step 2: extract TSA certificate using cryptography's PKCS7 parser
    try:
        certs = _load_certs(token_raw)
    except Exception as exc:
        logger.debug("verify_tsa_sig: cert extraction failed: %s", exc)
        return None
    if not certs:
        logger.debug("verify_tsa_sig: no certificates in TimeStampToken")
        return None
    tsa_cert = certs[0]

    # Step 3: navigate to SignerInfo
    sd_value = _extract_signed_data_value(token_raw)
    if sd_value is None:
        return None
    si_value = _extract_signer_info_value(sd_value)
    if si_value is None:
        return None
    parsed = _parse_signer_info(si_value)
    if parsed is None:
        return None
    signed_attrs_raw, sig_alg_oid, signature_bytes = parsed

    # Step 4: rebuild signed data for verification.
    # CMS spec: when signedAttrs is present, the signature covers
    # DER(SET OF Attributes) — i.e., replace [0] IMPLICIT tag (0xa0) with SET (0x31).
    signed_attrs_for_verify = b"\x31" + signed_attrs_raw[1:]

    # Step 5: dispatch to the appropriate cryptography primitive
    _RSA_HASHES = {
        _OID_SHA256_RSA: _h.SHA256(),
        _OID_SHA384_RSA: _h.SHA384(),
        _OID_SHA512_RSA: _h.SHA512(),
        # rsaEncryption (bare OID) — hash algorithm is in digestAlgorithm field;
        # defaulting to SHA-256 covers the common PKCS7SignatureBuilder output.
        _OID_RSA_ENCRYPTION: _h.SHA256(),
    }
    _ECDSA_HASHES = {
        _OID_SHA256_ECDSA: _h.SHA256(),
        _OID_SHA384_ECDSA: _h.SHA384(),
        _OID_SHA512_ECDSA: _h.SHA512(),
    }

    pub_key = tsa_cert.public_key()
    try:
        if sig_alg_oid in _RSA_HASHES:
            if not isinstance(pub_key, RSAPublicKey):
                logger.debug("verify_tsa_sig: RSA OID but key type is %s", type(pub_key))
                return None
            pub_key.verify(
                signature_bytes,
                signed_attrs_for_verify,
                _pad.PKCS1v15(),
                _RSA_HASHES[sig_alg_oid],
            )
            return True
        elif sig_alg_oid in _ECDSA_HASHES:
            if not isinstance(pub_key, EllipticCurvePublicKey):
                logger.debug("verify_tsa_sig: ECDSA OID but key type is %s", type(pub_key))
                return None
            pub_key.verify(
                signature_bytes,
                signed_attrs_for_verify,
                _ec.ECDSA(_ECDSA_HASHES[sig_alg_oid]),
            )
            return True
        else:
            logger.debug(
                "verify_tsa_sig: unsupported sig alg OID %s; degrading",
                sig_alg_oid.hex(),
            )
            return None
    except _cx.InvalidSignature:
        logger.warning(
            "verify_timestamp: TSA CMS signature INVALID — possible forgery or corruption"
        )
        return False
    except Exception as exc:
        logger.debug("verify_tsa_sig: verification error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Existing helpers (unchanged)
# ---------------------------------------------------------------------------


def _extract_message_imprint(tsr_der: bytes, expected_hash: bytes) -> bool:
    """Navigate TSR DER to find and compare the messageImprint hash.

    Structure traversed:
        TimeStampResp SEQUENCE
          → PKIStatusInfo SEQUENCE (skipped)
          → TimeStampToken [ContentInfo] (CMS SignedData)
              → SEQUENCE { OID, [0] SignedData }
                  → SignedData SEQUENCE
                      → ... encapContentInfo SEQUENCE
                          → eContentType OID
                          → [0] eContent OCTET STRING (contains TSTInfo DER)
                              → TSTInfo SEQUENCE
                                  → version INTEGER
                                  → policy OID
                                  → messageImprint SEQUENCE
                                      → AlgorithmIdentifier SEQUENCE (skipped)
                                      → hashedMessage OCTET STRING  ← target

    This is a best-effort search: we scan all OCTET STRING leaf values in the
    TSR looking for one that matches expected_hash. This avoids a full CMS
    parser while still providing meaningful integrity evidence.
    """
    return _find_bytes_in_der(tsr_der, expected_hash)


def _find_bytes_in_der(der: bytes, target: bytes) -> bool:
    """Recursively scan DER for any OCTET STRING containing *target*."""
    pos = 0
    while pos < len(der):
        try:
            tag = der[pos]
            pos += 1
            first = der[pos]
            pos += 1
            if first & 0x80 == 0:
                length = first
            else:
                n = first & 0x7F
                if pos + n > len(der):
                    return False
                length = int.from_bytes(der[pos:pos + n], "big")
                pos += n
            value = der[pos:pos + length]
            pos += length
        except (IndexError, ValueError):
            return False

        if tag == 0x04:  # OCTET STRING
            if value == target:
                return True
            # Nested DER in OCTET STRING (e.g., eContent wrapping TSTInfo)
            if _find_bytes_in_der(value, target):
                return True
        elif tag in (0x30, 0x31, 0xa0):  # SEQUENCE, SET, [0] IMPLICIT
            if _find_bytes_in_der(value, target):
                return True

    return False
