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
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jcs  # type: ignore[import-untyped]
import jsonschema  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import NameOID, load_pem_x509_certificate

from novafabric.promote.exceptions import PredicateValidationError

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


# ---------------------------------------------------------------------------
# PAE — Pre-Authentication Encoding (DSSE spec §2.1)
# ---------------------------------------------------------------------------

def _sp(s: bytes) -> bytes:
    return struct.pack("<Q", len(s)) + s


def _pae(payload_type: str, payload: bytes) -> bytes:
    return b"DSSEv1" + _sp(payload_type.encode("utf-8")) + _sp(payload)


# ---------------------------------------------------------------------------
# Base64url helpers (no padding)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


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
        "payload": _b64url_encode(payload),
        "payloadType": payload_type,
        "signatures": [
            {
                "keyid": keyid,
                "sig": _b64url_encode(signature),
                "cert": _b64url_encode(cert_der),
            }
        ],
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def verify_promote_envelope(
    envelope_bytes: bytes,
    expected_payload_type: str,
) -> tuple[bytes, str]:
    """Verify a promote DSSE envelope.

    Args:
        envelope_bytes:       JSON bytes of the DSSE envelope.
        expected_payload_type: Must match the envelope's payloadType.

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

    if payload_type != expected_payload_type:
        raise EnvelopeError(
            f"Unexpected payloadType {payload_type!r}; expected {expected_payload_type!r}"
        )

    payload = _b64url_decode(payload_b64)
    sig_entry = sigs[0]
    sig_bytes = _b64url_decode(sig_entry.get("sig", ""))
    cert_der = _b64url_decode(sig_entry.get("cert", ""))

    try:
        cert = load_pem_x509_certificate(_der_to_pem_cert(cert_der))
        public_key = cert.public_key()
    except Exception as exc:
        raise EnvelopeError(f"Failed to load cert from envelope: {exc}") from exc

    if not isinstance(public_key, ec.EllipticCurvePublicKey):  # pragma: no cover
        raise EnvelopeError("Cert key is not an EC public key")

    pae = _pae(payload_type, payload)

    try:
        public_key.verify(sig_bytes, pae, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise EnvelopeError("DSSE signature verification failed: signature mismatch")
    except Exception as exc:  # pragma: no cover
        raise EnvelopeError(f"DSSE signature verification error: {exc}") from exc

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
