# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""x509 offline signing identity (ADR-0055, ``x509`` profile).

The ``x509`` signing profile signs with a long-lived key (ECDSA P-256 or RSA-2048+),
embeds the operator-issued X.509 certificate alongside the signature, and requires no
external service at signing or verification time.

This module ships the offline core:

* :class:`X509SigningIdentity` loads a PKCS#8 PEM key + PEM certificate and signs a
  payload, producing an :class:`X509Signature` that carries the algorithm, the raw
  signature bytes, and the embedded certificate.
* :func:`verify_x509_signature` verifies a signature by (1) resolving trust in the
  embedded certificate and (2) verifying the signature under the public key extracted
  from that certificate.

Trust resolution (ADR-0055 "Trust resolution at verification time", step 2) accepts
the embedded certificate when **either** anchor holds, checked in this order:

1. **Pinned fingerprint** — the certificate's SHA-256 fingerprint is in the operator's
   pinned trust set (the original, air-gap-friendly model; behaviour unchanged).
2. **CA-bundle chain** (*experimental*) — the certificate chains, via RFC 5280 path
   validation performed by ``cryptography.x509.verification``, to a trust anchor in the
   operator-provided ``ca_bundle`` (see :func:`validate_certificate_chain`).

Chain validation can optionally be followed by an **offline CRL revocation check**
(*experimental*, ADR-0070 §3 / ADR-0055 OQ-55-3) against CRLs an operator syncs into a
local directory — see :mod:`novafabric.trust.novaseal.crl`.

Everything is offline and uses only the ``cryptography`` library's standard
primitives — no hand-rolled crypto, no revocation (CRL/OCSP) fetch, no network. The
Rekor inclusion-proof option remains deferred and layers on top of this without
changing the signature format.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.x509 import Certificate, load_pem_x509_certificate
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import (
    Criticality,
    ExtensionPolicy,
    PolicyBuilder,
    Store,
    VerificationError,
)
from pydantic import BaseModel

from novafabric.trust.novaseal.crl import CrlStore, RevocationCheckResult, check_chain_revocation

_ALG_ECDSA_P256 = "ecdsa-p256-sha256"
_ALG_RSA_PSS = "rsa-pss-sha256"

#: Default and hard upper bound on the number of intermediate certificates a chain may
#: traverse (bounded path building; matches ``cryptography``'s own default of 8).
DEFAULT_MAX_CHAIN_DEPTH = 8
_MAX_CHAIN_DEPTH_LIMIT = 16

#: ``X509VerifyResult.trust_basis`` values.
TRUST_BASIS_PINNED = "pinned"
TRUST_BASIS_CA_CHAIN = "ca_chain"


class X509IdentityError(ValueError):
    """Raised when a key/certificate cannot be loaded or a key type is unsupported."""


class X509ChainError(X509IdentityError):
    """Raised when a CA bundle or a DSSE-embedded signer certificate cannot be loaded.

    Chain *validation* never raises (a failed chain is a ``valid=False``
    :class:`ChainValidationResult`); this is only for unusable inputs such as an empty
    or malformed ``ca_bundle`` file, which an operator must fix rather than have
    silently treated as "no CA configured".
    """


class X509Signature(BaseModel):
    """A signature plus the embedded signing certificate (ADR-0055 ``x509`` profile)."""

    algorithm: str
    signature: bytes
    certificate_pem: str


@dataclass
class X509VerifyResult:
    """Outcome of an x509 signature verification."""

    valid: bool
    reason: str
    subject_common_name: str | None = None
    certificate_fingerprint: str | None = None
    #: How trust in the signer certificate was established: ``"pinned"`` or
    #: ``"ca_chain"``; ``None`` when trust was not established.
    trust_basis: str | None = None
    #: RFC 4514 subjects of the validated chain, leaf first (``ca_chain`` basis only).
    chain_subjects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChainValidationResult:
    """Outcome of :func:`validate_certificate_chain` (ADR-0055 trust step 2).

    Attributes:
        valid: ``True`` when the leaf chains to a trust anchor in the CA bundle.
        reason: Human-readable outcome; on failure, why path validation failed.
        chain_subjects: RFC 4514 subjects of the validated chain, leaf first,
            trust anchor last. Empty on failure.
        trust_anchor_fingerprint: ``sha256:``-prefixed fingerprint of the anchor
            the chain terminated at, or ``None`` on failure.
        revocation: Offline CRL check outcome when a ``crl_store`` was supplied and
            the path validated; ``None`` otherwise. Soft-fail warnings live here
            even when ``valid`` is ``True``.
    """

    valid: bool
    reason: str
    chain_subjects: tuple[str, ...] = ()
    trust_anchor_fingerprint: str | None = None
    revocation: RevocationCheckResult | None = None


def _fingerprint(cert: Certificate) -> str:
    return "sha256:" + cert.fingerprint(hashes.SHA256()).hex()


def _subject_cn(cert: Certificate) -> str | None:
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not attrs:
        return None
    value = attrs[0].value
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


class X509SigningIdentity:
    """A long-lived signing key bound to an X.509 certificate."""

    def __init__(
        self,
        private_key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey,
        certificate: Certificate,
    ) -> None:
        self._key = private_key
        self._cert = certificate

    @classmethod
    def from_pem(
        cls,
        key_pem: bytes,
        cert_pem: bytes,
        *,
        password: bytes | None = None,
    ) -> "X509SigningIdentity":
        """Load a PKCS#8 PEM private key and a PEM certificate."""
        try:
            key = serialization.load_pem_private_key(key_pem, password=password)
        except (ValueError, TypeError) as exc:
            raise X509IdentityError(f"could not load private key: {exc}") from exc
        try:
            cert = load_pem_x509_certificate(cert_pem)
        except ValueError as exc:
            raise X509IdentityError(f"could not load certificate: {exc}") from exc
        if not isinstance(key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
            raise X509IdentityError("unsupported key type; expected ECDSA P-256 or RSA (2048+)")
        return cls(key, cert)

    @property
    def certificate_fingerprint(self) -> str:
        """SHA-256 fingerprint of the certificate, ``sha256:``-prefixed."""
        return _fingerprint(self._cert)

    def sign(self, payload: bytes) -> X509Signature:
        """Sign ``payload`` with the long-lived key and embed the certificate."""
        cert_pem = self._cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        if isinstance(self._key, ec.EllipticCurvePrivateKey):
            signature = self._key.sign(payload, ECDSA(hashes.SHA256()))
            algorithm = _ALG_ECDSA_P256
        else:  # RSAPrivateKey (guaranteed by from_pem)
            signature = self._key.sign(
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            algorithm = _ALG_RSA_PSS
        return X509Signature(
            algorithm=algorithm,
            signature=signature,
            certificate_pem=cert_pem,
        )


def load_ca_bundle(pem: bytes) -> list[Certificate]:
    """Parse an operator CA bundle (one or more concatenated PEM certificates).

    Every certificate in the bundle is treated as a **trust anchor** — roots and any
    intermediate the operator chooses to trust directly.

    Args:
        pem: Raw bytes of the PEM ``ca_bundle`` file.

    Returns:
        The parsed certificates, in file order (at least one).

    Raises:
        X509ChainError: The bundle is empty or contains no parseable PEM certificate.
    """
    try:
        certs = x509.load_pem_x509_certificates(pem)
    except ValueError as exc:
        raise X509ChainError(f"could not load CA bundle: {exc}") from exc
    return certs


def _not_a_ca(_policy: object, _cert: Certificate, value: x509.BasicConstraints | None) -> None:
    """End-entity extension validator: a signing certificate must not be a CA."""
    if value is not None and value.ca:
        raise ValueError("end-entity signing certificate must not be a CA certificate")


def _end_entity_policy() -> ExtensionPolicy:
    """Extension policy for the *signing* (end-entity) certificate.

    The WebPKI end-entity defaults require a subjectAltName and a TLS EKU, which a
    document/code-signing certificate issued by an internal CA need not carry, so the
    leaf policy starts from ``permit_all`` and only forbids ``BasicConstraints:
    CA=TRUE``. CA certificates keep the full WebPKI CA defaults (basicConstraints,
    keyUsage keyCertSign, path length, name constraints, …).
    """
    return ExtensionPolicy.permit_all().may_be_present(
        x509.BasicConstraints, Criticality.AGNOSTIC, _not_a_ca
    )


def _revocation_path(
    chain: Sequence[Certificate], pool: Sequence[Certificate], max_extra: int
) -> list[Certificate]:
    """Extend a validated path past a non-self-issued anchor for revocation checks.

    Path building stops at the first trust anchor it reaches. When an operator bundles
    ``root + intermediate`` (the CLI must, since the envelope carries only the leaf),
    the intermediate terminates the path and would never be revocation-checked. Walk
    upward through ``pool`` while the top certificate is not self-issued and some pool
    certificate both names it as subject and *directly signed* it, so the root's CRL
    can revoke the intermediate. Bounded by ``max_extra`` hops; never raises.
    """
    path = list(chain)
    for _ in range(max_extra):
        top = path[-1]
        if top.issuer == top.subject:
            break
        parent = next(
            (c for c in pool if c.subject == top.issuer and _directly_issued(top, c)), None
        )
        if parent is None or parent in path:
            break
        path.append(parent)
    return path


def _directly_issued(cert: Certificate, issuer: Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def validate_certificate_chain(
    leaf: Certificate,
    trust_anchors: Sequence[Certificate],
    *,
    intermediates: Iterable[Certificate] = (),
    validation_time: datetime.datetime | None = None,
    max_chain_depth: int = DEFAULT_MAX_CHAIN_DEPTH,
    crl_store: CrlStore | None = None,
    crl_strict: bool = False,
    ca_policy: ExtensionPolicy | None = None,
) -> ChainValidationResult:
    """Validate ``leaf`` against an operator CA bundle, fully offline (experimental).

    Builds and validates a certification path with ``cryptography``'s
    ``x509.verification`` RFC 5280 path builder: every certificate in the path must
    be within its validity window at ``validation_time``, each issuer signature must
    verify, CA certificates must satisfy the WebPKI CA extension profile, and the path
    must end at one of ``trust_anchors``. Without ``crl_store`` no revocation check is
    made (unchanged behaviour); with it, every non-anchor certificate on the validated
    path is checked offline against the store (ADR-0070 §3): a revoked certificate
    always fails, missing/stale/invalid CRLs fail only when ``crl_strict``.

    Args:
        leaf: The signer (end-entity) certificate.
        trust_anchors: Trust anchors, typically from :func:`load_ca_bundle`.
        intermediates: Untrusted intermediate certificates available for path
            building (they are *not* trust anchors).
        validation_time: Instant to validate at; defaults to now (UTC). A naive
            datetime is interpreted as UTC.
        max_chain_depth: Maximum intermediates on the path, 1-16 (bounded search).
        crl_store: Optional CRLs from ``crl.load_crl_directory`` (experimental).
        crl_strict: Treat ``no_crl`` / ``stale`` / ``invalid_crl`` as failures.
        ca_policy: Extension policy for CA certificates on the path; defaults to
            the WebPKI CA profile. The RFC 3161 TSA chain (ADR-0070 §1) passes a
            profile that tolerates a non-critical basicConstraints on the CA.

    Returns:
        A :class:`ChainValidationResult`. Never raises for an untrusted or malformed
        chain; only a programming error (e.g. an out-of-range ``max_chain_depth``)
        raises ``ValueError``.
    """
    if not 1 <= max_chain_depth <= _MAX_CHAIN_DEPTH_LIMIT:
        raise ValueError(
            f"max_chain_depth must be between 1 and {_MAX_CHAIN_DEPTH_LIMIT}, got {max_chain_depth}"
        )
    anchors = list(trust_anchors)
    untrusted = list(intermediates)
    if not anchors:
        return ChainValidationResult(valid=False, reason="CA bundle contains no trust anchors")
    when = validation_time or datetime.datetime.now(datetime.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    verifier = (
        PolicyBuilder()
        .store(Store(anchors))
        .time(when)
        .max_chain_depth(max_chain_depth)
        .extension_policies(
            ca_policy=ca_policy or ExtensionPolicy.webpki_defaults_ca(),
            ee_policy=_end_entity_policy(),
        )
        .build_client_verifier()
    )
    try:
        verified = verifier.verify(leaf, untrusted)
    except VerificationError as exc:
        return ChainValidationResult(
            valid=False, reason=f"certificate chain validation failed: {exc}"
        )
    chain = verified.chain
    subjects = tuple(c.subject.rfc4514_string() for c in chain)
    anchor = _fingerprint(chain[-1])
    revocation = (
        check_chain_revocation(
            _revocation_path(chain, [*anchors, *untrusted], max_chain_depth),
            crl_store,
            validation_time=when,
            strict=crl_strict,
        )
        if crl_store is not None
        else None
    )
    if revocation is not None and not revocation.ok:
        return ChainValidationResult(
            valid=False,
            reason=f"certificate revocation check failed: {revocation.summary}",
            chain_subjects=subjects,
            trust_anchor_fingerprint=anchor,
            revocation=revocation,
        )
    return ChainValidationResult(
        valid=True,
        reason="certificate chains to a trust anchor in the CA bundle",
        chain_subjects=subjects,
        trust_anchor_fingerprint=anchor,
        revocation=revocation,
    )


def signer_certificate_from_dsse(dsse_bytes: bytes) -> Certificate:
    """Extract the signer certificate embedded in a NovaSeal DSSE envelope.

    Reads ``signatures[0].cert`` (base64 or base64url DER, ADR-0054). Used by
    ``nova verify --ca-bundle`` to chain-validate the certificate the DSSE signature
    was verified under.

    Raises:
        X509ChainError: The envelope is not JSON, has no signature, carries a bare
            ``pubkey`` instead of a certificate, or the certificate is not valid DER.
    """
    try:
        envelope = json.loads(dsse_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise X509ChainError(f"DSSE envelope is not valid JSON: {exc}") from exc
    sigs = envelope.get("signatures") if isinstance(envelope, dict) else None
    if not isinstance(sigs, list) or not sigs or not isinstance(sigs[0], dict):
        raise X509ChainError("DSSE envelope has no signatures")
    cert_b64 = sigs[0].get("cert")
    if not isinstance(cert_b64, str) or not cert_b64:
        raise X509ChainError(
            "DSSE signature carries no X.509 certificate (bare-key envelopes cannot "
            "be chain-validated)"
        )
    padded = cert_b64 + "=" * (-len(cert_b64) % 4)
    try:
        der = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
        return x509.load_der_x509_certificate(der)
    except (binascii.Error, ValueError) as exc:
        raise X509ChainError(f"DSSE signer certificate is unparseable: {exc}") from exc


def _b64_any(value: str) -> bytes:
    """Decode standard or URL-safe base64, padded or not (DSSE field tolerance)."""
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)


def _dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 Pre-Authentication Encoding — delegates to ``envelope._pae``."""
    from novafabric.trust.novaseal.envelope import _pae  # noqa: PLC0415

    return _pae(payload_type, payload)


def _dsse_pae_candidates(payload_type: str, payload: bytes) -> list[bytes]:
    """Spec PAE, then the legacy PAE envelopes were signed with through v0.102.x."""
    from novafabric.trust.novaseal.envelope import pae_candidates  # noqa: PLC0415

    return [pae for _encoding, pae in pae_candidates(payload_type, payload)]


def _spki(key: object) -> bytes:
    """SubjectPublicKeyInfo DER of a public key, for key-equality comparison."""
    return key.public_bytes(  # type: ignore[attr-defined, no-any-return]
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _dsse_entry_bound_cert(entry: object, pae: bytes) -> tuple[Certificate | None, str]:
    """Return the entry's certificate iff the entry's signature verifies under *its* key.

    Binds signature and certificate: the signature over the PAE bytes is checked with
    the public key of the embedded ``cert`` itself, never with a sibling ``pubkey``.
    An entry whose ``pubkey`` disagrees with its ``cert`` is rejected outright.
    """
    if not isinstance(entry, dict):
        return None, "signature entry is not an object"
    cert_b64 = entry.get("cert")
    if not isinstance(cert_b64, str) or not cert_b64:
        return None, ("no X.509 certificate (bare-key signatures cannot be chain-validated)")
    try:
        cert = x509.load_der_x509_certificate(_b64_any(cert_b64))
    except (binascii.Error, ValueError) as exc:
        return None, f"certificate is unparseable: {exc}"
    public_key = cert.public_key()
    pubkey_b64 = entry.get("pubkey")
    if pubkey_b64:
        try:
            if not isinstance(pubkey_b64, str):
                raise ValueError("pubkey is not a string")
            declared = serialization.load_pem_public_key(_b64_any(pubkey_b64))
        except (binascii.Error, ValueError, TypeError) as exc:
            return None, f"'pubkey' is unparseable: {exc}"
        if _spki(declared) != _spki(public_key):
            return None, "'pubkey' does not match the certificate's public key"
    sig_raw = entry.get("sig")
    try:
        if not isinstance(sig_raw, str):
            raise ValueError("sig is not a string")
        sig = _b64_any(sig_raw)
    except (binascii.Error, ValueError) as exc:
        return None, f"signature is unparseable: {exc}"
    try:
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(sig, pae, ECDSA(hashes.SHA256()))
        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(sig, pae)
        else:
            return None, f"unsupported certificate key type: {type(public_key).__name__}"
    except InvalidSignature:
        return None, "signature does not verify under the certificate's public key"
    return cert, "signature verified under the certificate's public key"


def verify_dsse_signer_chain(
    dsse_bytes: bytes,
    trust_anchors: Sequence[Certificate],
    *,
    intermediates: Iterable[Certificate] = (),
    validation_time: datetime.datetime | None = None,
    crl_store: CrlStore | None = None,
    crl_strict: bool = False,
) -> ChainValidationResult:
    """Bind a DSSE signature to a CA-validated certificate (ADR-0055 steps 1+2).

    Chain-validating an embedded certificate proves nothing unless that certificate's
    key is the one that produced the signature: ``verify_envelope`` prefers a
    ``pubkey`` over ``cert`` and accepts any one valid entry, so a forged envelope can
    pair an attacker key/signature with a legitimately issued leaf. This check is
    self-contained and fail-closed: it passes only when **at least one** signature
    entry both (1) verifies over the DSSE PAE bytes under the public key of its own
    ``cert`` (any ``pubkey`` must match that key) and (2) that same certificate
    chains to ``trust_anchors`` (and, with ``crl_store``, passes the offline CRL
    revocation policy — see :func:`validate_certificate_chain`).

    Raises:
        X509ChainError: The envelope is not JSON or has no signatures.

    Returns:
        A :class:`ChainValidationResult`; on failure ``reason`` names every entry's
        individual reason and ``revocation`` carries the first CRL check that ran
        (so a revoked signer is reported per certificate, not just as a failure).
    """
    try:
        envelope = json.loads(dsse_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise X509ChainError(f"DSSE envelope is not valid JSON: {exc}") from exc
    sigs = envelope.get("signatures") if isinstance(envelope, dict) else None
    if not isinstance(sigs, list) or not sigs:
        raise X509ChainError("DSSE envelope has no signatures")
    payload_type = envelope.get("payloadType", "")
    payload_b64 = envelope.get("payload", "")
    try:
        if not isinstance(payload_type, str) or not isinstance(payload_b64, str):
            raise ValueError("payload/payloadType is not a string")
        payload = _b64_any(payload_b64)
    except (binascii.Error, ValueError) as exc:
        raise X509ChainError(f"DSSE payload is unparseable: {exc}") from exc
    paes = _dsse_pae_candidates(payload_type, payload)
    extra = list(intermediates)

    failures: list[str] = []
    revocation: RevocationCheckResult | None = None
    for index, entry in enumerate(sigs):
        cert, why = _dsse_entry_bound_cert(entry, paes[0])
        if cert is None:
            legacy_cert, _ = _dsse_entry_bound_cert(entry, paes[1])
            cert = legacy_cert
        if cert is None:
            failures.append(f"signatures[{index}]: {why}")
            continue
        outcome = validate_certificate_chain(
            cert,
            trust_anchors,
            intermediates=extra,
            validation_time=validation_time,
            crl_store=crl_store,
            crl_strict=crl_strict,
        )
        if outcome.valid:
            return ChainValidationResult(
                valid=True,
                reason=(
                    f"signatures[{index}] verified under a certificate that chains "
                    "to a trust anchor in the CA bundle"
                ),
                chain_subjects=outcome.chain_subjects,
                trust_anchor_fingerprint=outcome.trust_anchor_fingerprint,
                revocation=outcome.revocation,
            )
        failures.append(f"signatures[{index}]: {outcome.reason}")
        revocation = revocation or outcome.revocation
    return ChainValidationResult(
        valid=False,
        reason="no signature is bound to a CA-trusted certificate: " + "; ".join(failures),
        revocation=revocation,
    )


def _verify_signature_bytes(
    payload: bytes, signature: X509Signature, cert: Certificate
) -> str | None:
    """Verify the raw signature under ``cert``'s key; return a failure reason or None."""
    public_key = cert.public_key()
    try:
        if signature.algorithm == _ALG_ECDSA_P256 and isinstance(
            public_key, ec.EllipticCurvePublicKey
        ):
            public_key.verify(signature.signature, payload, ECDSA(hashes.SHA256()))
        elif signature.algorithm == _ALG_RSA_PSS and isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                signature.signature,
                payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            return f"unsupported algorithm/key combination: {signature.algorithm}"
    except InvalidSignature:
        return "signature does not verify under the certificate's public key"
    return None


def verify_x509_signature(
    payload: bytes,
    signature: X509Signature,
    *,
    pinned_fingerprints: set[str] | frozenset[str] | None = None,
    ca_bundle: Sequence[Certificate] | None = None,
    intermediates: Iterable[Certificate] = (),
    validation_time: datetime.datetime | None = None,
    crl_store: CrlStore | None = None,
    crl_strict: bool = False,
) -> X509VerifyResult:
    """Verify an x509 signature against the operator's trust anchors.

    Two conditions must both hold: the embedded certificate is **trusted**, and the
    signature verifies under the certificate's public key. Trust is resolved in
    ADR-0055 order — pinned fingerprint first, then (when ``ca_bundle`` is given)
    CA-bundle chain validation via :func:`validate_certificate_chain`. Either anchor
    suffices ("pinned OR chain"). Never raises — a failure is a ``valid=False`` result.

    Args:
        payload: The signed bytes.
        signature: The signature with its embedded certificate.
        pinned_fingerprints: ``sha256:``-prefixed fingerprints the operator pins.
        ca_bundle: Trust anchors for chain validation (experimental); ``None``
            disables chain validation, preserving pinned-only behaviour.
        intermediates: Untrusted intermediates available for path building.
        validation_time: Instant for chain validity checks; defaults to now (UTC).
        crl_store: Optional offline CRLs checked on the CA-chain path only (a pinned
            certificate is trusted by fingerprint, not revocation-checked).
        crl_strict: Fail on ``no_crl`` / ``stale`` / ``invalid_crl`` too.
    """
    try:
        cert = load_pem_x509_certificate(signature.certificate_pem.encode("utf-8"))
    except ValueError as exc:
        return X509VerifyResult(valid=False, reason=f"unparseable certificate: {exc}")

    fingerprint = _fingerprint(cert)
    cn = _subject_cn(cert)
    trust_basis: str | None = None
    chain_subjects: list[str] = []
    untrusted_reason = "certificate not in the pinned trust set (untrusted signer)"
    if fingerprint in (pinned_fingerprints or ()):
        trust_basis = TRUST_BASIS_PINNED
    elif ca_bundle is not None:
        chain = validate_certificate_chain(
            cert,
            ca_bundle,
            intermediates=intermediates,
            validation_time=validation_time,
            crl_store=crl_store,
            crl_strict=crl_strict,
        )
        if chain.valid:
            trust_basis = TRUST_BASIS_CA_CHAIN
            chain_subjects = list(chain.chain_subjects)
        else:
            untrusted_reason = (
                f"certificate not pinned and not trusted by the CA bundle "
                f"(untrusted signer): {chain.reason}"
            )
    if trust_basis is None:
        return X509VerifyResult(
            valid=False,
            reason=untrusted_reason,
            subject_common_name=cn,
            certificate_fingerprint=fingerprint,
        )

    failure = _verify_signature_bytes(payload, signature, cert)
    if failure is not None:
        return X509VerifyResult(
            valid=False,
            reason=failure,
            subject_common_name=cn,
            certificate_fingerprint=fingerprint,
        )
    reason = (
        "signature verified and certificate is pinned"
        if trust_basis == TRUST_BASIS_PINNED
        else "signature verified and certificate chains to a trusted CA"
    )
    return X509VerifyResult(
        valid=True,
        reason=reason,
        subject_common_name=cn,
        certificate_fingerprint=fingerprint,
        trust_basis=trust_basis,
        chain_subjects=chain_subjects,
    )
