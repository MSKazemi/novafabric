"""DSSE envelope helpers and predicate builders for the promote subsystem.

Payload types are distinct from the capsule PAYLOAD_TYPE in envelope.py so
that verify_envelope() (which hard-checks payloadType) never accidentally
accepts a promote envelope as a capsule envelope, and vice-versa.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.resources
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jcs  # type: ignore[import-untyped]
import jsonschema  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import Certificate, NameOID, load_pem_x509_certificate

from novafabric.promote.exceptions import PredicateValidationError

# Base64 helpers — one definition, shared with NovaSeal.  This module used to
# carry its own byte-identical copy of the URL-safe, unpadded pair, so the DSSE
# interop defect fixed in ``trust.novaseal.envelope`` existed here twice.
from novafabric.trust.novaseal.envelope import (
    _b64_decode,
    _b64_encode,
    _pae,
    pae_candidates,
)
from novafabric.trust.novaseal.x509_identity import (
    X509ChainError,
    load_ca_bundle,
    validate_certificate_chain,
)

# ---------------------------------------------------------------------------
# Payload type constants
# ---------------------------------------------------------------------------

PROPOSAL_PAYLOAD_TYPE = "application/vnd.novafabric.promote.proposal+json"
APPROVAL_PAYLOAD_TYPE = "application/vnd.novafabric.promote.approval+json"
POLICY_PAYLOAD_TYPE = "application/vnd.novafabric.promote.policy+json"
BYPASS_PAYLOAD_TYPE = "application/vnd.novafabric.promote.bypass+json"


# ---------------------------------------------------------------------------
# EnvelopeError (local — mirrors trust.novaseal.envelope but for promote)
# ---------------------------------------------------------------------------

class EnvelopeError(Exception):
    """Raised on DSSE creation or verification failures in promote subsystem."""


class UntrustedSignerError(EnvelopeError):
    """The signature verifies, but its certificate does not chain to a pinned trust root.

    A subclass of :class:`EnvelopeError` so every caller that already treats an invalid
    envelope as a failure fails closed on an untrusted signer too (ADR-0315).
    """


# ---------------------------------------------------------------------------
# Trust root for promote signatures (ADR-0315, owner decision D3)
# ---------------------------------------------------------------------------

#: PEM bundle of CA certificates promote signers must chain to.
PROMOTE_CA_BUNDLE_ENV = "NOVAFABRIC_PROMOTE_CA_BUNDLE"
#: ``1`` accepts any self-signed certificate — development and tests only.
PROMOTE_INSECURE_ENV = "NOVAFABRIC_PROMOTE_INSECURE_SELF_SIGNED"

_NO_TRUST_ROOT = (
    "no promote trust root is configured, so the signer's identity cannot be checked "
    "(the certificate inside an envelope is chosen by whoever signed it). Pass "
    f"--ca-bundle <pem> or set {PROMOTE_CA_BUNDLE_ENV}; for local development only, "
    f"--insecure-self-signed or {PROMOTE_INSECURE_ENV}=1 accepts any self-signed signer"
)


@dataclass(frozen=True)
class PromoteTrust:
    """Who may sign promote proposals, approvals, policies and bypasses.

    ``anchors`` are the pinned CA certificates a signer must chain to (validated with
    NovaSeal's offline RFC 5280 path builder). With no anchors, signatures are
    refused unless ``insecure_self_signed`` was chosen explicitly.
    """

    anchors: tuple[Certificate, ...] = ()
    insecure_self_signed: bool = False

    @classmethod
    def resolve(
        cls, ca_bundle: Path | None = None, *, insecure_self_signed: bool = False
    ) -> PromoteTrust:
        """Explicit arguments first, then ``NOVAFABRIC_PROMOTE_*`` environment variables.

        Raises:
            EnvelopeError: the CA bundle cannot be read or holds no certificate.
        """
        path = ca_bundle or (Path(p) if (p := os.environ.get(PROMOTE_CA_BUNDLE_ENV)) else None)
        insecure = insecure_self_signed or os.environ.get(PROMOTE_INSECURE_ENV) == "1"
        if path is None:
            return cls((), insecure)
        try:
            anchors = load_ca_bundle(path.read_bytes())
        except (OSError, X509ChainError) as exc:
            raise EnvelopeError(f"promote CA bundle {path}: {exc}") from exc
        if not anchors:
            raise EnvelopeError(f"promote CA bundle {path} holds no certificate")
        return cls(tuple(anchors), insecure)

    def require(self) -> None:
        """Raise :class:`UntrustedSignerError` when no signer could ever be accepted."""
        if not self.anchors and not self.insecure_self_signed:
            raise UntrustedSignerError(_NO_TRUST_ROOT)

    def check(self, cert: Certificate) -> None:
        """Raise :class:`UntrustedSignerError` unless ``cert`` is an acceptable signer."""
        if self.anchors:
            result = validate_certificate_chain(cert, self.anchors)
            if not result.valid:
                raise UntrustedSignerError(
                    f"signer certificate does not chain to the promote trust root: "
                    f"{result.reason}"
                )
            return
        if not self.insecure_self_signed:
            raise UntrustedSignerError(_NO_TRUST_ROOT)


# PAE — Pre-Authentication Encoding: defined once, in
# ``trust/novaseal/envelope.py`` (DSSE v1 for signing; the pre-v0.103 legacy form is
# accepted on verification only). This module used to carry a byte-identical copy of
# the non-standard legacy encoder.




# ---------------------------------------------------------------------------
# Key / cert loading
# ---------------------------------------------------------------------------

def _load_private_key(key_path: Path) -> ec.EllipticCurvePrivateKey:
    pem = key_path.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise EnvelopeError(
            f"Key at {key_path} is not an EC private key (got {type(key).__name__})"
        )
    if not isinstance(key.curve, ec.SECP256R1):
        raise EnvelopeError(
            f"Key at {key_path} uses {key.curve.name}; promote subsystem requires P-256"
        )
    return key


def _load_cert(cert_path: Path) -> Any:
    pem = cert_path.read_bytes()
    return load_pem_x509_certificate(pem)


def _der_to_pem_cert(der: bytes) -> bytes:
    b64 = base64.b64encode(der).decode("ascii")
    lines = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode()


def _cert_subject(cert: Any) -> str:
    """Extract CN from cert, falling back to email SAN."""
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if attrs:
        return str(attrs[0].value)
    # Fallback: email SAN — only reached with non-CN certs (e.g. Fulcio OIDC certs)
    from cryptography.x509 import RFC822Name, SubjectAlternativeName
    try:
        san = cert.extensions.get_extension_for_class(SubjectAlternativeName)
        emails = san.value.get_values_for_type(RFC822Name)
        if emails:
            return str(emails[0])
    except Exception:
        pass
    return str(cert.subject.rfc4514_string())


# ---------------------------------------------------------------------------
# DSSE sign / verify (promote-specific)
# ---------------------------------------------------------------------------

def sign_promote_envelope(
    payload: bytes,
    payload_type: str,
    key_path: Path,
    cert_path: Path,
) -> bytes:
    """Sign *payload* with the given payload_type and return DSSE JSON bytes.

    Args:
        payload:      Raw predicate bytes (UTF-8 JSON).
        payload_type: One of PROPOSAL_PAYLOAD_TYPE / APPROVAL_PAYLOAD_TYPE / POLICY_PAYLOAD_TYPE.
        key_path:     Path to ECDSA P-256 PEM private key.
        cert_path:    Path to X.509 PEM certificate.

    Returns:
        JSON bytes of the DSSE envelope.
    """
    try:
        private_key = _load_private_key(key_path)
        cert = _load_cert(cert_path)
        cert_der = cert.public_bytes(serialization.Encoding.DER)
    except (ValueError, TypeError, OSError) as exc:
        raise EnvelopeError(f"Failed to load signing material: {exc}") from exc

    pae = _pae(payload_type, payload)

    try:
        signature = private_key.sign(pae, ec.ECDSA(hashes.SHA256()))
    except Exception as exc:  # pragma: no cover
        raise EnvelopeError(f"Signing failed: {exc}") from exc

    keyid = hashlib.sha256(cert_der).hexdigest()
    envelope = {
        "payload": _b64_encode(payload),
        "payloadType": payload_type,
        "signatures": [
            {
                "keyid": keyid,
                "sig": _b64_encode(signature),
                "cert": _b64_encode(cert_der),
            }
        ],
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def verify_promote_envelope(
    envelope_bytes: bytes,
    expected_payload_type: str,
    *,
    trust: PromoteTrust | None = None,
) -> tuple[bytes, str]:
    """Verify a promote DSSE envelope and that its signer is trusted (ADR-0315).

    Args:
        envelope_bytes:       JSON bytes of the DSSE envelope.
        expected_payload_type: Must match the envelope's payloadType.
        trust:                Who may sign; ``None`` resolves it from the
                              ``NOVAFABRIC_PROMOTE_*`` environment, which refuses every
                              signer when nothing is configured.

    Returns:
        Tuple of (payload_bytes, cert_subject).

    Raises:
        EnvelopeError: if malformed, signature invalid, or payload type mismatch.
    """
    try:
        env = json.loads(envelope_bytes)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"Envelope is not valid JSON: {exc}") from exc

    payload_b64 = env.get("payload", "")
    payload_type = env.get("payloadType", "")
    sigs = env.get("signatures", [])

    if not sigs:
        raise EnvelopeError("Envelope has no signatures")
    if trust is None:
        trust = PromoteTrust.resolve()

    if payload_type != expected_payload_type:
        raise EnvelopeError(
            f"Unexpected payloadType {payload_type!r}; expected {expected_payload_type!r}"
        )

    payload = _b64_decode(payload_b64)
    sig_entry = sigs[0]
    sig_bytes = _b64_decode(sig_entry.get("sig", ""))
    cert_der = _b64_decode(sig_entry.get("cert", ""))

    try:
        cert = load_pem_x509_certificate(_der_to_pem_cert(cert_der))
        public_key = cert.public_key()
    except Exception as exc:
        raise EnvelopeError(f"Failed to load cert from envelope: {exc}") from exc

    if not isinstance(public_key, ec.EllipticCurvePublicKey):  # pragma: no cover
        raise EnvelopeError("Cert key is not an EC public key")

    # DSSE v1 PAE first; envelopes signed through v0.102.x used the legacy PAE.
    for _encoding, pae in pae_candidates(payload_type, payload):
        try:
            public_key.verify(sig_bytes, pae, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            continue
        except Exception as exc:  # pragma: no cover
            raise EnvelopeError(f"DSSE signature verification error: {exc}") from exc
        break
    else:
        raise EnvelopeError("DSSE signature verification failed: signature mismatch")

    trust.check(cert)
    subject = _cert_subject(cert)
    return payload, subject


# ---------------------------------------------------------------------------
# Predicate builders
# ---------------------------------------------------------------------------

def build_proposal_predicate(
    capsule_id: str,
    capsule_digest_hex: str,
    target_env: str,
    justification: str,
    proposer_subject: str,
    policy_version: str,
) -> dict[str, Any]:
    """Build a proposal predicate dict."""
    return {
        "role": "proposer",
        "capsule_id": capsule_id,
        "capsule_digest": {"sha256": capsule_digest_hex},
        "target_environment": target_env,
        "justification": justification,
        "proposer_subject": proposer_subject,
        "policy_version": policy_version,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def build_approval_predicate(
    proposal_envelope_bytes: bytes,
    decision: str,
    approver_subject: str,
    justification: str | None = None,
) -> dict[str, Any]:
    """Build an approval predicate dict.

    proposal_digest is SHA-256 of JCS-canonicalized proposal envelope bytes.
    """
    canonical = jcs.canonicalize(json.loads(proposal_envelope_bytes))
    proposal_digest = hashlib.sha256(canonical).hexdigest()

    predicate: dict[str, Any] = {
        "role": "approver",
        "proposal_digest": {"sha256": proposal_digest},
        "decision": decision,
        "approver_subject": approver_subject,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if justification is not None:
        predicate["justification"] = justification
    return predicate


def build_bypass_predicate(
    capsule_id: str,
    capsule_digest_hex: str,
    target_environment: str,
    bypass_reason: str,
    bypass_authorized_by: str,
    valid_until: str,
    notification_sent_to: list[str],
    notification_status: str = "not_configured",
) -> dict[str, Any]:
    """Build the bypass predicate dict (validate against promote_bypass_v1.json)."""
    return {
        "bypass_reason": bypass_reason,
        "capsule_id": capsule_id,
        "capsule_digest": {"sha256": capsule_digest_hex},
        "target_environment": target_environment,
        "bypass_authorized_by": bypass_authorized_by,
        "valid_until": valid_until,
        "notification_sent_to": notification_sent_to,
        "notification_status": notification_status,
    }


def build_policy_predicate(
    proposer_key_ids: list[str],
    approver_key_ids: list[str],
    bypass_valid_duration_hours: int = 24,
) -> dict[str, Any]:
    """Build a policy predicate dict."""
    return {
        "proposer_key_ids": proposer_key_ids,
        "approver_key_ids": approver_key_ids,
        "self_approval": False,
        "approval_threshold": 1,
        "bypass_valid_duration_hours": bypass_valid_duration_hours,
    }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_predicate(schema_name: str, predicate: dict[str, Any]) -> None:
    """Validate *predicate* against the named JSON schema.

    Args:
        schema_name: filename (e.g. "promote_proposal_v1.json")
        predicate:   dict to validate

    Raises:
        PredicateValidationError: on validation failure
    """
    try:
        data = (
            importlib.resources.files("novafabric.schemas")
            .joinpath(schema_name)
            .read_text(encoding="utf-8")
        )
        schema = json.loads(data)
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise PredicateValidationError(f"Schema not found: {schema_name}") from exc

    try:
        jsonschema.validate(predicate, schema)
    except jsonschema.ValidationError as exc:
        raise PredicateValidationError(str(exc.message)) from exc
