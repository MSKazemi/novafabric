"""RFC 3161 TSA certificate chain depth validation (ADR-0070).

Implements ``verify_tsa_cert_chain()`` which:
  1. Extracts the TSA signing certificate from the TimeStampToken CMS
     SignedData using the ``cryptography`` library.
  2. Walks the certificate chain (issuer → subject matching) up to
     ``max_depth`` levels using the certificates embedded in the TSR.
  3. Returns a ``CertChainResult`` dataclass describing the outcome.

Degraded mode:
  If ``cryptography`` is not importable or the TSR cannot be parsed, the
  function returns ``CertChainResult(valid=False, ..., error=<reason>)``.
  This is consistent with the existing pattern in ``timestamp.py``.

Design decisions (ADR-0070):
  - Only certificates embedded in the TSR itself are used for chain
    building.  No external trust-store lookup is performed here; that
    belongs to a separate trust-anchor validation layer.
  - The chain depth limit prevents pathologically deep chains from causing
    unbounded loops.
  - EKU (id-kp-timeStamping = 1.3.6.1.5.5.7.3.8) presence in the signing
    certificate is verified and reported as a field in CertChainResult but
    is non-fatal (soft-check) to preserve compatibility with self-signed
    test TSAs that omit EKU extensions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# OID for id-kp-timeStamping (RFC 3161 §2.3, RFC 5280 §4.2.1.12)
_EKU_TIMESTAMPING_OID = "1.3.6.1.5.5.7.3.8"


@dataclass
class CertChainResult:
    """Result of ``verify_tsa_cert_chain()``.

    Attributes:
        valid:          Overall validity of the chain check.
        depth:          Number of certificates traversed (1 = leaf only).
        subject:        RFC 4514 subject DN of the TSA signing certificate.
        issuer:         RFC 4514 issuer DN of the TSA signing certificate.
        eku_present:    True if id-kp-timeStamping EKU is in the signing cert.
        self_signed:    True if issuer == subject on the TSA signing cert.
        chain_subjects: DNs of all certificates in the traversed chain.
        error:          Human-readable error string, or None on success.
    """

    valid: bool
    depth: int
    subject: str
    issuer: str
    eku_present: bool = False
    self_signed: bool = False
    chain_subjects: list[str] = field(default_factory=list)
    error: str | None = None


def verify_tsa_cert_chain(
    tsr_bytes: bytes,
    max_depth: int = 4,
) -> CertChainResult:
    """Validate the TSA signing certificate chain embedded in a TSR.

    Parses the RFC 3161 TimeStampResponse, extracts all certificates from
    the TimeStampToken CMS SignedData, then walks from the TSA signing cert
    toward the root up to *max_depth* levels.

    Args:
        tsr_bytes: DER-encoded RFC 3161 TimeStampResponse.
        max_depth: Maximum chain depth to traverse (default 4).

    Returns:
        ``CertChainResult`` describing validity, depth, subject/issuer DNs,
        EKU presence, and any error reason.

    Raises:
        Nothing.  All failures are captured in ``CertChainResult.error``.
    """
    _EMPTY = CertChainResult(
        valid=False,
        depth=0,
        subject="",
        issuer="",
        error="unknown error",
    )

    if not tsr_bytes:
        return CertChainResult(
            valid=False,
            depth=0,
            subject="",
            issuer="",
            error="empty TSR bytes",
        )

    try:
        from cryptography.hazmat.primitives.serialization.pkcs7 import (
            load_der_pkcs7_certificates as _load_certs,
        )
        from cryptography.x509.extensions import ExtendedKeyUsage
    except ImportError:
        return CertChainResult(
            valid=False,
            depth=0,
            subject="",
            issuer="",
            error="cryptography package not available",
        )

    # Step 1: extract the TimeStampToken ContentInfo raw TLV.
    # Reuse the DER helper from timestamp.py to avoid duplicating logic.
    try:
        from novafabric.trust.novaseal.timestamp import _extract_token_raw
    except ImportError:
        return CertChainResult(
            valid=False,
            depth=0,
            subject="",
            issuer="",
            error="could not import _extract_token_raw from timestamp",
        )

    token_raw = _extract_token_raw(tsr_bytes)
    if token_raw is None:
        return CertChainResult(
            valid=False,
            depth=0,
            subject="",
            issuer="",
            error="TimeStampToken absent or unparseable",
        )

    # Step 2: load all certificates embedded in the CMS SignedData.
    try:
        certs = _load_certs(token_raw)
    except Exception as exc:
        return CertChainResult(
            valid=False,
            depth=0,
            subject="",
            issuer="",
            error=f"cert extraction failed: {exc}",
        )

    if not certs:
        return CertChainResult(
            valid=False,
            depth=0,
            subject="",
            issuer="",
            error="no certificates in TimeStampToken",
        )

    # Step 3: the first certificate is the TSA signing certificate.
    signing_cert = certs[0]
    subject_dn = signing_cert.subject.rfc4514_string()
    issuer_dn = signing_cert.issuer.rfc4514_string()
    is_self_signed = signing_cert.subject == signing_cert.issuer

    # Step 4: check EKU.
    eku_present = False
    try:
        eku_ext = signing_cert.extensions.get_extension_for_class(ExtendedKeyUsage)
        eku_present = any(
            u.dotted_string == _EKU_TIMESTAMPING_OID
            for u in eku_ext.value
        )
    except Exception:
        # EKU extension absent or extension API unavailable; soft-fail.
        pass

    if not eku_present:
        logger.debug(
            "verify_tsa_cert_chain: id-kp-timeStamping EKU absent from signing cert %s",
            subject_dn,
        )

    # Step 5: walk the chain.
    chain_subjects: list[str] = [subject_dn]
    depth = 1

    if is_self_signed:
        # Self-signed cert — chain of depth 1.
        return CertChainResult(
            valid=True,
            depth=depth,
            subject=subject_dn,
            issuer=issuer_dn,
            eku_present=eku_present,
            self_signed=True,
            chain_subjects=chain_subjects,
            error=None,
        )

    # Build a lookup map: subject DN → cert for chain walking.
    # Use the same type as the list elements returned by load_der_pkcs7_certificates.
    from cryptography.x509 import Certificate

    cert_by_subject: dict[str, Certificate] = {}
    for c in certs:
        dn = c.subject.rfc4514_string()
        cert_by_subject[dn] = c

    current = signing_cert
    while depth < max_depth:
        issuer_of_current = current.issuer.rfc4514_string()
        if current.subject == current.issuer:
            # Reached self-signed root.
            break
        parent = cert_by_subject.get(issuer_of_current)
        if parent is None:
            # Issuer not embedded in TSR; chain terminates here.
            logger.debug(
                "verify_tsa_cert_chain: chain terminates at depth %d "
                "(issuer %r not in TSR certs)",
                depth,
                issuer_of_current,
            )
            break
        parent_dn = parent.subject.rfc4514_string()
        if parent_dn in chain_subjects:
            # Cycle detected — malformed chain.
            return CertChainResult(
                valid=False,
                depth=depth,
                subject=subject_dn,
                issuer=issuer_dn,
                eku_present=eku_present,
                self_signed=is_self_signed,
                chain_subjects=chain_subjects,
                error=f"certificate chain cycle detected at {parent_dn!r}",
            )
        chain_subjects.append(parent_dn)
        depth += 1
        current = parent

    if depth >= max_depth and current.subject != current.issuer:
        return CertChainResult(
            valid=False,
            depth=depth,
            subject=subject_dn,
            issuer=issuer_dn,
            eku_present=eku_present,
            self_signed=is_self_signed,
            chain_subjects=chain_subjects,
            error=f"certificate chain depth {depth} exceeds max_depth={max_depth}",
        )

    return CertChainResult(
        valid=True,
        depth=depth,
        subject=subject_dn,
        issuer=issuer_dn,
        eku_present=eku_present,
        self_signed=is_self_signed,
        chain_subjects=chain_subjects,
        error=None,
    )
