"""RFC 3161 TSA trust-chain verification, fully offline (ADR-0070 §1/§2, experimental).

:func:`verify_tsa_trust_chain` answers one question fail-closed: *was this time-stamp
token really issued, at its ``genTime``, by a time-stamping authority the operator
trusts?* It performs, in order, and stops at the first failure:

1. **Parse** the ``TimeStampResp`` / ``TimeStampToken`` strictly
   (:mod:`novafabric.trust.novaseal.tsa_token`).
2. **Identify the signer certificate** named by ``SignerInfo.sid``
   (issuer + serial, or subject key identifier) among the certificates embedded in
   the token plus any the caller supplies.
3. **Bind it** through the signed ``signingCertificate`` (``ESSCertID``, RFC 2634) or
   ``signingCertificateV2`` (``ESSCertIDv2``, RFC 5816) attribute: the certificate
   hash must match, so a token cannot be re-pointed at a different certificate.
4. **CMS signed attributes**: ``contentType`` must be ``id-ct-TSTInfo`` and
   ``messageDigest`` must equal the hash of the ``TSTInfo`` under the
   ``SignerInfo.digestAlgorithm`` (SHA-256/384/512 only).
5. **CMS signature** over the DER signed attributes verifies under the signer
   certificate's public key (RSA PKCS#1 v1.5, ECDSA, Ed25519).
6. **Extended key usage**: the signer certificate carries ``extendedKeyUsage``
   marked *critical* whose only purpose is ``id-kp-timeStamping`` (RFC 3161 §2.3);
   a ``keyUsage`` extension, if present, must allow digitalSignature or
   nonRepudiation.
7. **Message imprint** (optional): ``TSTInfo.messageImprint`` equals the caller's
   expected digest — binding the token to the timestamped data.
8. **Chain**: RFC 5280 path validation to an operator trust anchor via
   :func:`~novafabric.trust.novaseal.x509_identity.validate_certificate_chain`, at
   the token's ``genTime`` (so an expired-since TSA certificate still validates a
   token issued while it was valid), depth limit 10, optionally followed by the
   offline CRL check of :mod:`novafabric.trust.novaseal.crl`: revocation status *as
   of* ``genTime``, from CRLs that are current *at verification time*.
   CA certificates get the WebPKI CA profile, except that a non-critical
   basicConstraints (``cA=TRUE`` still required) is tolerated — see
   :func:`_tsa_ca_policy`.

Everything is offline: no CRL/OCSP fetch, no network, no hand-rolled crypto (the
signature and hash primitives are ``cryptography``'s). Only the DER walk is local.

Honest limits: RSASSA-PSS signatures and SHA-1 message digests are rejected (fail
closed) rather than verified; ``ESSCertID`` v1 (SHA-1 certificate hash, still emitted
by freetsa.org) is accepted unless ``require_ess_cert_id_v2``; a revocation dated
*after* ``genTime`` for a reason other than ``keyCompromise`` / ``cACompromise`` and
without an ``invalidityDate`` at or before ``genTime`` (e.g. ``superseded``,
``cessationOfOperation``) does not invalidate earlier tokens — by design, but it
trusts the CA to have chosen the reason honestly; a CRL that has dropped an expired
TSA certificate's entry cannot report it; the nonce is reported but not checked
(replay protection happens at request time).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.verification import Criticality, ExtensionPolicy

from novafabric.trust.novaseal.crl import (
    CrlStoreError,
    RevocationCheckResult,
    load_crl_directory,
)
from novafabric.trust.novaseal.tsa_token import (
    OID_TST_INFO,
    SignerInfo,
    TimeStampToken,
    TsaTokenError,
    parse_timestamp_token,
)
from novafabric.trust.novaseal.x509_identity import (
    X509ChainError,
    load_ca_bundle,
    validate_certificate_chain,
)

__all__ = [
    "TSA_MAX_CHAIN_DEPTH",
    "TsaTrustChainResult",
    "verify_tsa_trust_chain",
]

#: ADR-0070 §2: maximum certification path depth for the TSA chain.
TSA_MAX_CHAIN_DEPTH = 10
_MAX_UNTRUSTED_CERTS = 32

_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    "2.16.840.1.101.3.4.2.1": hashes.SHA256,
    "2.16.840.1.101.3.4.2.2": hashes.SHA384,
    "2.16.840.1.101.3.4.2.3": hashes.SHA512,
}
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_SHA1 = "1.3.14.3.2.26"
_RSA_SIG: dict[str, type[hashes.HashAlgorithm] | None] = {
    "1.2.840.113549.1.1.1": None,  # rsaEncryption: hash = SignerInfo.digestAlgorithm
    "1.2.840.113549.1.1.11": hashes.SHA256,
    "1.2.840.113549.1.1.12": hashes.SHA384,
    "1.2.840.113549.1.1.13": hashes.SHA512,
}
_ECDSA_SIG: dict[str, type[hashes.HashAlgorithm] | None] = {
    "1.2.840.10045.2.1": None,  # id-ecPublicKey: hash = SignerInfo.digestAlgorithm
    "1.2.840.10045.4.3.2": hashes.SHA256,
    "1.2.840.10045.4.3.3": hashes.SHA384,
    "1.2.840.10045.4.3.4": hashes.SHA512,
}
_OID_ED25519 = "1.3.101.112"


class _Fail(Exception):
    """Internal: a verification step failed with a human-readable reason."""


@dataclass(frozen=True)
class TsaTrustChainResult:
    """Outcome of :func:`verify_tsa_trust_chain`.

    Attributes:
        valid: ``True`` only when every check passed.
        reason: Why it failed, or a one-line success summary.
        signer_subject: RFC 4514 subject of the TSA signer certificate, once found.
        gen_time: The token's ``genTime`` (UTC), once parsed.
        policy: ``TSTInfo.policy`` OID.
        serial_number: ``TSTInfo.serialNumber``.
        nonce: ``TSTInfo.nonce``, if present (reported, not checked).
        imprint_algorithm: ``messageImprint.hashAlgorithm`` OID.
        imprint_digest_hex: ``messageImprint.hashedMessage``, lowercase hex.
        ess_cert_id_version: 1 (``ESSCertID``) or 2 (``ESSCertIDv2``) once bound.
        chain_subjects: The validated path, signer first, trust anchor last.
        trust_anchor_fingerprint: SHA-256 fingerprint (hex) of the anchor.
        revocation: Offline CRL result when a CRL directory was given.
    """

    valid: bool
    reason: str
    signer_subject: str | None = None
    gen_time: datetime.datetime | None = None
    policy: str | None = None
    serial_number: int | None = None
    nonce: int | None = None
    imprint_algorithm: str | None = None
    imprint_digest_hex: str | None = None
    ess_cert_id_version: int | None = None
    chain_subjects: tuple[str, ...] = ()
    trust_anchor_fingerprint: str | None = None
    revocation: RevocationCheckResult | None = None


# ---------------------------------------------------------------------------
# Individual checks (each raises _Fail)
# ---------------------------------------------------------------------------


def _matches_sid(cert: x509.Certificate, signer: SignerInfo) -> bool:
    if signer.sid_key_identifier is not None:
        try:
            ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        except x509.ExtensionNotFound:
            return False
        return hmac.compare_digest(ski.value.digest, signer.sid_key_identifier)
    return (
        cert.serial_number == signer.sid_serial
        and cert.issuer.public_bytes() == signer.sid_issuer_der
    )


def _find_signer(
    token: TimeStampToken, extra: Sequence[x509.Certificate]
) -> tuple[x509.Certificate, list[x509.Certificate]]:
    pool = [*token.certificates, *extra]
    signer = next((c for c in pool if _matches_sid(c, token.signer)), None)
    if signer is None:
        raise _Fail(
            "TSA signer certificate named by SignerInfo.sid is not embedded in the token "
            "(request tokens with certReq=true) and was not supplied"
        )
    others = [c for c in pool if c != signer]
    return signer, others


def _check_ess_cert_id(
    token: TimeStampToken, signer_cert: x509.Certificate, require_v2: bool
) -> int:
    ess = token.signer.ess_cert_id
    if ess is None:
        raise _Fail("signed attributes lack signingCertificate / signingCertificateV2")
    if require_v2 and ess.version != 2:
        raise _Fail("ESSCertIDv2 (RFC 5816) required but the token carries ESSCertID v1")
    der = signer_cert.public_bytes(serialization.Encoding.DER)
    if ess.version == 1:
        expected = hashlib.sha1(der, usedforsecurity=False).digest()
    else:
        algorithm = _HASHES.get(ess.hash_algorithm.oid)
        if algorithm is None:
            raise _Fail(f"unsupported ESSCertIDv2 hash algorithm {ess.hash_algorithm.oid}")
        expected = _digest(algorithm, der)
    if not hmac.compare_digest(expected, ess.cert_hash):
        raise _Fail("ESSCertID certificate hash does not match the TSA signer certificate")
    if ess.issuer_serial is not None and ess.issuer_serial != signer_cert.serial_number:
        raise _Fail("ESSCertID issuerSerial does not match the TSA signer certificate")
    return ess.version


def _digest(algorithm: type[hashes.HashAlgorithm], data: bytes) -> bytes:
    ctx = hashes.Hash(algorithm())
    ctx.update(data)
    return ctx.finalize()


def _check_signed_attrs(token: TimeStampToken) -> type[hashes.HashAlgorithm]:
    signer = token.signer
    if signer.content_type != OID_TST_INFO:
        raise _Fail("signed contentType attribute is missing or not id-ct-TSTInfo")
    oid = signer.digest_algorithm.oid
    algorithm = _HASHES.get(oid)
    if algorithm is None:
        weak = " (SHA-1 is not accepted)" if oid == _OID_SHA1 else ""
        raise _Fail(f"unsupported SignerInfo digest algorithm {oid}{weak}")
    if signer.message_digest is None:
        raise _Fail("signed attributes lack messageDigest")
    if not hmac.compare_digest(_digest(algorithm, token.econtent), signer.message_digest):
        raise _Fail("messageDigest attribute does not match the TSTInfo (token was altered)")
    return algorithm


def _check_signature(
    signer: SignerInfo, cert: x509.Certificate, digest_alg: type[hashes.HashAlgorithm]
) -> None:
    oid = signer.signature_algorithm.oid
    key = cert.public_key()
    data = signer.signed_attrs_der
    try:
        if oid in _RSA_SIG:
            if not isinstance(key, rsa.RSAPublicKey):
                raise _Fail("RSA signature algorithm but the TSA certificate key is not RSA")
            key.verify(signer.signature, data, padding.PKCS1v15(), (_RSA_SIG[oid] or digest_alg)())
        elif oid in _ECDSA_SIG:
            if not isinstance(key, ec.EllipticCurvePublicKey):
                raise _Fail("ECDSA signature algorithm but the TSA certificate key is not EC")
            key.verify(signer.signature, data, ec.ECDSA((_ECDSA_SIG[oid] or digest_alg)()))
        elif oid == _OID_ED25519:
            if not isinstance(key, ed25519.Ed25519PublicKey):
                raise _Fail("Ed25519 signature algorithm but the TSA key is not Ed25519")
            key.verify(signer.signature, data)
        else:
            raise _Fail(f"unsupported CMS signature algorithm {oid} (fail closed)")
    except InvalidSignature as exc:
        raise _Fail("CMS signature over the signed attributes does not verify") from exc
    except UnsupportedAlgorithm as exc:  # pragma: no cover - backend-dependent
        raise _Fail(f"signature algorithm not supported by the backend: {exc}") from exc


def _check_tsa_usage(cert: x509.Certificate) -> None:
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound as exc:
        raise _Fail("TSA certificate has no extendedKeyUsage (id-kp-timeStamping)") from exc
    purposes = list(eku.value)
    if purposes != [ExtendedKeyUsageOID.TIME_STAMPING]:
        raise _Fail(
            "TSA certificate extendedKeyUsage must contain only id-kp-timeStamping "
            f"(RFC 3161 §2.3); got {[p.dotted_string for p in purposes]}"
        )
    if not eku.critical:
        raise _Fail("TSA certificate extendedKeyUsage must be critical (RFC 3161 §2.3)")
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return
    if not (usage.digital_signature or usage.content_commitment):
        raise _Fail("TSA certificate keyUsage allows neither digitalSignature nor nonRepudiation")


def _check_imprint(token: TimeStampToken, expected: bytes, expected_oid: str) -> None:
    """``messageImprint`` must use the expected hash algorithm *and* carry the digest.

    Comparing bytes alone would let a token whose imprint claims another (or an
    unknown) algorithm pass as long as the octets happen to match; the algorithm is
    part of what the TSA attested, so it must match too.
    """
    imprint = token.tst_info.message_imprint
    oid = imprint.hash_algorithm.oid
    if oid != expected_oid:
        raise _Fail(
            f"messageImprint hash algorithm {oid} does not match the expected {expected_oid}"
        )
    if _HASHES[oid]().digest_size != len(imprint.hashed_message):
        raise _Fail("messageImprint length does not match its hash algorithm")
    if not hmac.compare_digest(imprint.hashed_message, expected):
        raise _Fail("messageImprint does not match the expected digest of the timestamped data")


def _require_ca(
    _policy: object, _cert: x509.Certificate, value: x509.BasicConstraints | None
) -> None:
    """CA-profile validator: an issuing certificate must assert ``cA=TRUE``."""
    if value is None or not value.ca:
        raise ValueError("TSA issuing certificate must assert basicConstraints cA=TRUE")


def _tsa_ca_policy() -> ExtensionPolicy:
    """WebPKI CA profile, except basicConstraints may be non-critical.

    RFC 5280 §4.2.1.9 wants basicConstraints critical on CA certificates, but
    long-lived TSA roots do not all comply — freetsa.org's root (NovaFabric's default
    TSA) marks it non-critical. ``cA=TRUE`` is still required; every other WebPKI CA
    check (keyCertSign, path length, name constraints, …) is unchanged.
    """
    return ExtensionPolicy.webpki_defaults_ca().require_present(
        x509.BasicConstraints, Criticality.AGNOSTIC, _require_ca
    )


def _now(when: datetime.datetime | None) -> datetime.datetime:
    """``when`` as an aware UTC datetime, defaulting to the current instant."""
    if when is None:
        return datetime.datetime.now(datetime.timezone.utc)
    return when if when.tzinfo is not None else when.replace(tzinfo=datetime.timezone.utc)


def _anchors(trusted_ca_pems: bytes | Sequence[x509.Certificate]) -> list[x509.Certificate]:
    if isinstance(trusted_ca_pems, bytes):
        try:
            return load_ca_bundle(trusted_ca_pems)
        except X509ChainError as exc:
            raise _Fail(f"TSA CA bundle unusable: {exc}") from exc
    return list(trusted_ca_pems)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_tsa_trust_chain(
    token_bytes: bytes,
    trusted_ca_pems: bytes | Sequence[x509.Certificate],
    crl_cache_dir: Path | None = None,
    *,
    crl_strict: bool = False,
    expected_digest: bytes | None = None,
    expected_digest_algorithm: str = _OID_SHA256,
    untrusted_certs: Sequence[x509.Certificate] = (),
    require_ess_cert_id_v2: bool = False,
    max_chain_depth: int = TSA_MAX_CHAIN_DEPTH,
    verification_time: datetime.datetime | None = None,
) -> TsaTrustChainResult:
    """Verify an RFC 3161 token's TSA signature, EKU and chain, offline and fail-closed.

    Args:
        token_bytes: DER ``TimeStampResp`` (e.g. ``manifest.dsse.tsr``) or bare
            ``TimeStampToken``.
        trusted_ca_pems: Operator TSA trust anchors — concatenated PEM bytes, or
            already-parsed certificates. Never a system/public bundle by default
            (NIST SP 800-89: trust anchors are pre-provisioned).
        crl_cache_dir: Optional directory of operator-synced CRLs (never fetched).
            The validated path is revocation-checked *as of* ``genTime`` using CRLs
            that are current at ``verification_time`` (a CRL synced today has
            ``thisUpdate`` after ``genTime``, so judging its freshness at ``genTime``
            would make every CRL stale). ``keyCompromise`` / ``cACompromise``
            revocations, and entries whose ``invalidityDate`` is at or before
            ``genTime``, revoke even when dated after ``genTime``.
        crl_strict: Treat a missing / stale / unverifiable CRL as a failure.
        expected_digest: When given, ``messageImprint.hashedMessage`` must equal it
            (NovaSeal tokens: SHA-256 of the DSSE envelope bytes).
        expected_digest_algorithm: OID of the hash ``expected_digest`` was computed
            with (default SHA-256); ``messageImprint.hashAlgorithm`` must equal it.
        untrusted_certs: Extra certificates for locating the signer and building
            the path (not trust anchors); at most 32.
        require_ess_cert_id_v2: Reject tokens that bind the signer only via the
            SHA-1 ``ESSCertID`` (ADR-0070 §4 strict mode).
        max_chain_depth: Path-length bound, 1-10.
        verification_time: The instant CRL freshness is judged at; defaults to now
            (UTC). A naive datetime is interpreted as UTC.

    Returns:
        A :class:`TsaTrustChainResult`; ``valid`` is ``True`` only when every check
        passed. Malformed input never raises.

    Raises:
        ValueError: ``max_chain_depth`` out of range, too many ``untrusted_certs`` or
            an unsupported ``expected_digest_algorithm`` (programming errors, not
            token properties).
    """
    if not 1 <= max_chain_depth <= TSA_MAX_CHAIN_DEPTH:
        raise ValueError(f"max_chain_depth must be between 1 and {TSA_MAX_CHAIN_DEPTH}")
    if expected_digest_algorithm not in _HASHES:
        raise ValueError(f"unsupported expected_digest_algorithm {expected_digest_algorithm}")
    if len(untrusted_certs) > _MAX_UNTRUSTED_CERTS:
        raise ValueError(f"at most {_MAX_UNTRUSTED_CERTS} untrusted_certs are accepted")
    try:
        token = parse_timestamp_token(token_bytes)
    except TsaTokenError as exc:
        return TsaTrustChainResult(valid=False, reason=f"malformed time-stamp token: {exc}")
    tst = token.tst_info
    result = TsaTrustChainResult(
        valid=False,
        reason="",
        gen_time=tst.gen_time,
        policy=tst.policy,
        serial_number=tst.serial_number,
        nonce=tst.nonce,
        imprint_algorithm=tst.message_imprint.hash_algorithm.oid,
        imprint_digest_hex=tst.message_imprint.hashed_message.hex(),
    )
    try:
        anchors = _anchors(trusted_ca_pems)
        if not anchors:
            raise _Fail("TSA CA bundle contains no trust anchors")
        signer_cert, pool = _find_signer(token, untrusted_certs)
        result = replace(result, signer_subject=signer_cert.subject.rfc4514_string())
        ess_version = _check_ess_cert_id(token, signer_cert, require_ess_cert_id_v2)
        result = replace(result, ess_cert_id_version=ess_version)
        digest_alg = _check_signed_attrs(token)
        _check_signature(token.signer, signer_cert, digest_alg)
        _check_tsa_usage(signer_cert)
        if expected_digest is not None:
            _check_imprint(token, expected_digest, expected_digest_algorithm)
        try:
            store = (
                replace(load_crl_directory(crl_cache_dir), freshness_time=_now(verification_time))
                if crl_cache_dir is not None
                else None
            )
        except CrlStoreError as exc:
            raise _Fail(str(exc)) from exc
    except _Fail as exc:
        return replace(result, reason=str(exc))
    chain = validate_certificate_chain(
        signer_cert,
        anchors,
        intermediates=pool,
        validation_time=tst.gen_time,
        max_chain_depth=max_chain_depth,
        crl_store=store,
        crl_strict=crl_strict,
        ca_policy=_tsa_ca_policy(),
    )
    reason = (
        f"TSA token signed by a trusted time-stamping authority at {tst.gen_time.isoformat()}"
        if chain.valid
        else f"TSA {chain.reason}"
    )
    return replace(
        result,
        valid=chain.valid,
        reason=reason,
        chain_subjects=chain.chain_subjects,
        trust_anchor_fingerprint=chain.trust_anchor_fingerprint,
        revocation=chain.revocation,
    )
