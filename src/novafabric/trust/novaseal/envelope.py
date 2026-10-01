"""DSSE envelope creation and verification for NovaSeal v0.1.

Implements Dead Simple Signing Envelope (DSSE) with ECDSA P-256 / SHA-256.

Spec: https://github.com/secure-systems-lab/dsse/blob/master/protocol.md

Envelope JSON schema:
    {
      "payload":     "<base64url(manifest_json_bytes)>",
      "payloadType": "application/vnd.novafabric.capsule+json",
      "signatures": [{
        "keyid":  "<sha256-hex of cert DER>",
        "sig":    "<base64url(signature_bytes)>",
        "cert":   "<base64url(cert_der_bytes)>",
        "intent": "<signing_intent string — optional, FDA 21 CFR §11.50(b)>"
      }]
    }

The optional ``intent`` field satisfies FDA 21 CFR Part 11 §11.50(a)(3) which
requires electronic signatures to display "the meaning associated with the
signature."
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509 import load_pem_x509_certificate

if TYPE_CHECKING:
    from novafabric.trust.novaseal.signing_backend import SigningBackend

PAYLOAD_TYPE = "application/vnd.novafabric.capsule+json"


class SigningIntent(str, Enum):
    """Meaning associated with a NovaSeal signature (FDA 21 CFR §11.50(a)(3)).

    Values are stored as plain strings in the DSSE envelope so that any JSON
    consumer can read them without importing NovaFabric.
    """

    AUTHORED = "authored"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    WITNESSED = "witnessed"
    VERIFIED = "verified"


class EnvelopeError(Exception):
    """Raised on DSSE creation or verification failures."""


# ---------------------------------------------------------------------------
# PAE — Pre-Authentication Encoding
#
# DSSE v1 (protocol.md):
#     PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body
# with LEN(s) the ASCII decimal byte length of s.  That is what ``_pae`` computes
# and what every new envelope is signed over, so in-toto, cosign, Rekor's ``dsse``
# entry type and any DSSE library verify a NovaSeal envelope unchanged.
#
# Through v0.102.x NovaSeal signed a non-standard hybrid instead —
# ``b"DSSEv1"`` followed by 8-byte little-endian length prefixes, no separators
# (the pre-v1 signing-spec length encoding under the v1 tag).  No stock verifier
# computes it, so those envelopes verified only with NovaFabric.  They must keep
# verifying forever, so verification falls back to ``_pae_legacy`` and reports
# which encoding matched (``PAE_LEGACY``) instead of silently accepting it.
#
# Accepting both is safe: the two encodings can never produce the same bytes.
# Byte 7 of a DSSE v1 PAE is an ASCII digit (the first digit of LEN(type));
# byte 7 of the legacy PAE is the second byte of a little-endian 64-bit length,
# which is 0x00 for every payloadType shorter than 256 bytes — and NovaFabric only
# verifies the fixed types it defines.  A signature over one encoding is therefore
# never a signature over a different (type, body) pair in the other.
# ---------------------------------------------------------------------------

#: Envelope signed over the DSSE v1 PAE (every envelope created from v0.103 on).
PAE_DSSE_V1 = "dsse-v1"
#: Envelope signed over the pre-spec little-endian PAE (sealed through v0.102.x).
PAE_LEGACY = "legacy-le64"


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 Pre-Authentication Encoding — the bytes every new signature covers."""
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 %d %b %d %b" % (len(type_bytes), type_bytes, len(payload), payload)


def _pae_legacy(payload_type: str, payload: bytes) -> bytes:
    """Pre-spec PAE used through v0.102.x (verification only — never signed again)."""
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1"
        + struct.pack("<Q", len(type_bytes))
        + type_bytes
        + struct.pack("<Q", len(payload))
        + payload
    )


def pae_candidates(payload_type: str, payload: bytes) -> list[tuple[str, bytes]]:
    """The PAE byte strings a signature may cover, spec form first.

    Callers verifying a signature try each in order and record which one matched.
    """
    return [
        (PAE_DSSE_V1, _pae(payload_type, payload)),
        (PAE_LEGACY, _pae_legacy(payload_type, payload)),
    ]


# ---------------------------------------------------------------------------
# Key and cert loading helpers
# ---------------------------------------------------------------------------

def _load_private_key(
    key_path: Path,
) -> ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey:
    """Load an EC P-256 or Ed25519 private key from a PEM file."""
    pem = key_path.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if not isinstance(key.curve, ec.SECP256R1):
            raise EnvelopeError(
                f"Key at {key_path} uses {key.curve.name}; "
                "NovaSeal requires P-256 (secp256r1) or Ed25519"
            )
        return key
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key
    raise EnvelopeError(
        f"Key at {key_path} is not an EC P-256 or Ed25519 private key "
        f"(got {type(key).__name__})"
    )


def _load_cert_der(cert_path: Path) -> bytes:
    pem = cert_path.read_bytes()
    cert = load_pem_x509_certificate(pem)
    return cert.public_bytes(serialization.Encoding.DER)


def _keyid_from_cert_der(cert_der: bytes) -> str:
    return hashlib.sha256(cert_der).hexdigest()


# ---------------------------------------------------------------------------
# Base64 helpers
#
# The DSSE spec (and in-toto, and every off-the-shelf verifier) encodes
# ``payload``, ``sig`` and ``cert`` as *standard* base64 with padding —
# RFC 4648 §4.  NovaSeal originally emitted base64**url** without padding,
# which a stock ``base64.b64decode`` cannot read: measured over 399 statement
# payloads, 77% raised "Incorrect padding", and once padding was restored 73%
# still decoded to the wrong bytes because ``-``/``_`` are not in the standard
# alphabet.  That defeats the entire point of using DSSE, which is that someone
# else's tool can verify our envelopes.
#
# So: encode to the spec, decode tolerantly.  The decoder accepts both
# alphabets with or without padding, which is what keeps every envelope signed
# before this change verifiable.  That is safe because DSSE signs the PAE over
# the *decoded* payload bytes (see ``_pae``), never over the base64 text — the
# transport encoding is not covered by the signature.
# ---------------------------------------------------------------------------

def _b64_encode(data: bytes) -> str:
    """Encode to standard, padded base64 — what the DSSE spec requires."""
    return base64.b64encode(data).decode("ascii")


def _b64_decode(s: str) -> bytes:
    """Decode standard *or* URL-safe base64, padded or not.

    Tolerant on purpose: envelopes written before NovaFabric emitted
    spec-compliant base64 are URL-safe and unpadded, and must keep verifying.
    """
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------------------
# Envelope creation
# ---------------------------------------------------------------------------

def create_envelope(
    payload: bytes,
    key_path: Path,
    cert_path: Path,
    intent: Optional[SigningIntent] = None,
    *,
    backend: "Optional[SigningBackend]" = None,
) -> bytes:
    """Create a DSSE envelope for *payload* and return the JSON bytes.

    Args:
        payload:   Raw capsule manifest bytes (UTF-8 JSON or YAML).
        key_path:  Path to ECDSA P-256 PEM private key (ignored when *backend*
                   is provided).
        cert_path: Path to X.509 PEM certificate (ignored when *backend* is
                   provided).
        intent:    Optional signing intent per FDA 21 CFR §11.50(a)(3).
                   When provided, stored as ``intent`` in the signature entry.
        backend:   Optional ``SigningBackend`` instance.  When supplied the
                   *key_path* / *cert_path* arguments are not used; the backend
                   is responsible for signing the PAE digest and returning the
                   certificate DER bytes.  Pass this to use a Cloud KMS key.

    Returns:
        JSON bytes of the DSSE envelope.

    Raises:
        EnvelopeError: on key loading, signing, or cert errors.
    """
    pae = _pae(PAYLOAD_TYPE, payload)

    if backend is not None:
        # Cloud KMS / external backend path.
        try:
            import hashlib as _hashlib
            digest = _hashlib.sha256(pae).digest()
            signature = backend.sign_digest(digest)
            cert_der = backend.get_cert_der()
        except Exception as exc:
            raise EnvelopeError(f"Backend signing failed: {exc}") from exc
    else:
        # Local PEM key path (original behaviour — fully backward compatible).
        try:
            private_key = _load_private_key(key_path)
            cert_der = _load_cert_der(cert_path)
        except (ValueError, TypeError, OSError) as exc:
            raise EnvelopeError(f"Failed to load signing material: {exc}") from exc

        try:
            if isinstance(private_key, ec.EllipticCurvePrivateKey):
                signature = private_key.sign(pae, ec.ECDSA(hashes.SHA256()))
            elif isinstance(private_key, ed25519.Ed25519PrivateKey):
                # Ed25519 sign() takes no algorithm argument.
                signature = private_key.sign(pae)
            else:
                raise EnvelopeError(f"Unsupported key type: {type(private_key).__name__}")
        except EnvelopeError:
            raise
        except Exception as exc:
            raise EnvelopeError(f"Signing failed: {exc}") from exc

    keyid = _keyid_from_cert_der(cert_der)

    sig_entry: dict[str, str] = {
        "keyid": keyid,
        "sig": _b64_encode(signature),
        "cert": _b64_encode(cert_der),
    }
    if intent is not None:
        sig_entry["intent"] = intent.value

    envelope = {
        "payload": _b64_encode(payload),
        "payloadType": PAYLOAD_TYPE,
        "signatures": [sig_entry],
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Envelope verification
# ---------------------------------------------------------------------------

def _verify_signature_entry(sig_entry: dict[str, Any], pae: bytes) -> None:
    """Verify one DSSE ``signatures`` entry, or raise :class:`EnvelopeError`.

    This is the body that used to live inline in :func:`verify_envelope` for
    ``signatures[0]``. The key-type branches are unchanged — only their location
    is — so a single-signature envelope takes exactly the same path and raises
    exactly the same errors as before.
    """
    sig_bytes = _b64_decode(sig_entry.get("sig", ""))

    # Determine key type from the envelope.  Envelopes produced by Go
    # (Ed25519) embed a raw PEM public key in "pubkey"; envelopes produced by
    # Python NovaSeal embed an X.509 cert DER in "cert".
    pubkey_pem_b64 = sig_entry.get("pubkey")
    cert_b64 = sig_entry.get("cert", "")

    if pubkey_pem_b64:
        # Ed25519 or raw-PEM path (used by Go collector).
        try:
            pubkey_pem = _b64_decode(pubkey_pem_b64)
            public_key = serialization.load_pem_public_key(pubkey_pem)
        except Exception as exc:
            raise EnvelopeError(
                f"Failed to load public key from envelope 'pubkey' field: {exc}"
            ) from exc

        if isinstance(public_key, ed25519.Ed25519PublicKey):
            try:
                public_key.verify(sig_bytes, pae)
            except InvalidSignature:
                raise EnvelopeError(
                    "DSSE Ed25519 signature verification failed: signature mismatch"
                )
            except Exception as exc:
                raise EnvelopeError(
                    f"DSSE Ed25519 signature verification error: {exc}"
                ) from exc
            return

        if isinstance(public_key, ec.EllipticCurvePublicKey):
            try:
                public_key.verify(sig_bytes, pae, ec.ECDSA(hashes.SHA256()))
            except InvalidSignature:
                raise EnvelopeError(
                    "DSSE ECDSA signature verification failed: signature mismatch"
                )
            except Exception as exc:
                raise EnvelopeError(
                    f"DSSE ECDSA signature verification error: {exc}"
                ) from exc
            return

        raise EnvelopeError(
            f"Unsupported key type in 'pubkey' field: {type(public_key).__name__}"
        )

    # Legacy / standard path: X.509 cert DER in "cert" field (Python NovaSeal).
    if not cert_b64:
        raise EnvelopeError(
            "Envelope has no 'cert' or 'pubkey' field; cannot verify signature"
        )

    cert_der = _b64_decode(cert_b64)
    try:
        cert = load_pem_x509_certificate(
            _der_to_pem_cert(cert_der)
        )
        public_key = cert.public_key()
    except Exception as exc:
        raise EnvelopeError(f"Failed to load cert from envelope: {exc}") from exc

    if isinstance(public_key, ec.EllipticCurvePublicKey):
        try:
            public_key.verify(sig_bytes, pae, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise EnvelopeError(
                "DSSE signature verification failed: signature mismatch"
            )
        except Exception as exc:
            raise EnvelopeError(
                f"DSSE signature verification error: {exc}"
            ) from exc
        return

    if isinstance(public_key, ed25519.Ed25519PublicKey):
        try:
            public_key.verify(sig_bytes, pae)
        except InvalidSignature:
            raise EnvelopeError(
                "DSSE Ed25519 signature verification failed: signature mismatch"
            )
        except Exception as exc:
            raise EnvelopeError(
                f"DSSE Ed25519 signature verification error: {exc}"
            ) from exc
        return

    raise EnvelopeError(
        f"Unsupported key type in cert: {type(public_key).__name__}"
    )


def _verify_signature_entry_any_pae(
    sig_entry: dict[str, Any], candidates: list[tuple[str, bytes]]
) -> str:
    """Verify one entry over the spec PAE, else the legacy PAE; return which matched.

    Raises the spec-PAE error when neither matches, so a failing envelope reports
    exactly the message it always did.
    """
    first_error: EnvelopeError | None = None
    for encoding, pae in candidates:
        try:
            _verify_signature_entry(sig_entry, pae)
        except EnvelopeError as exc:
            if first_error is None:
                first_error = exc
            continue
        return encoding
    assert first_error is not None  # candidates is never empty
    raise first_error


def verify_envelope(
    envelope_bytes: bytes,
    expected_payload: bytes | None = None,
    *,
    require: str = "any",
) -> bool:
    """Verify the DSSE envelope signatures.

    Every entry in ``signatures`` is considered. Until 2026-09-02 only
    ``signatures[0]`` was examined, which meant a second signature — valid or
    forged — was neither honoured nor detected. That was the recorded blocker for
    ADR-0151 NF-191 (hybrid post-quantum signing), because a hybrid envelope
    carries a classic and a post-quantum signature side by side.

    Each signature is checked over the DSSE v1 PAE and, failing that, over the
    legacy PAE envelopes were signed with through v0.102.x (see ``_pae``). Use
    :func:`verify_envelope_encoding` to learn which one matched.

    Args:
        envelope_bytes:    JSON bytes of the DSSE envelope.
        expected_payload:  If provided, also verify the decoded payload matches.
        require:
            ``"any"`` (default) — hybrid-OR: the envelope verifies if **at least
            one** signature verifies. This is what a hybrid classic+PQ envelope
            needs, since a verifier that knows only one of the two algorithms
            must still be able to accept it.

            ``"all"`` — threshold: **every** signature must verify. Use when an
            envelope is meant to carry independent co-signatures and a single
            valid signature is not sufficient.

        For a single-signature envelope — which is every envelope this project
        currently produces — the two policies are identical, and identical to the
        behaviour before multi-signature support existed. No classic path changes.

    Returns:
        True if the envelope verifies under *require* (and the payload matches if
        *expected_payload* was given).

    Raises:
        EnvelopeError: if the envelope is malformed, or verification fails under
            *require*. With more than one signature the message names every
            signature index and its individual reason, because reporting only the
            first would hide which of a hybrid pair actually broke.
    """
    _verify_envelope(envelope_bytes, expected_payload, require=require)
    return True


def _verify_envelope(
    envelope_bytes: bytes,
    expected_payload: bytes | None = None,
    *,
    require: str = "any",
) -> str:
    """Shared body of the two public verifiers; returns the matched PAE encoding."""
    if require not in ("any", "all"):
        raise EnvelopeError(
            f"Unknown verification policy {require!r}; expected 'any' or 'all'"
        )

    try:
        env = json.loads(envelope_bytes)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"Envelope is not valid JSON: {exc}") from exc

    payload = _b64_decode(env.get("payload", ""))
    payload_type = env.get("payloadType", "")
    sigs = env.get("signatures", [])

    if not sigs:
        raise EnvelopeError("Envelope has no signatures")

    if payload_type != PAYLOAD_TYPE:
        raise EnvelopeError(
            f"Unexpected payloadType {payload_type!r}; expected {PAYLOAD_TYPE!r}"
        )

    if expected_payload is not None and payload != expected_payload:
        raise EnvelopeError("Envelope payload does not match expected bytes")

    candidates = pae_candidates(payload_type, payload)

    failures: list[str] = []
    encodings: list[str] = []
    for index, sig_entry in enumerate(sigs):
        try:
            encodings.append(_verify_signature_entry_any_pae(sig_entry, candidates))
        except EnvelopeError as exc:
            failures.append(f"signatures[{index}]: {exc}")
    verified = len(encodings)

    if require == "all" and failures:
        raise EnvelopeError(
            f"{len(failures)} of {len(sigs)} signature(s) failed under "
            f"require='all': " + "; ".join(failures)
        )
    if require == "any" and verified == 0:
        # Preserve the historical single-signature message verbatim: for one
        # signature the caller should see exactly the error it always saw.
        if len(sigs) == 1:
            raise EnvelopeError(failures[0].split(": ", 1)[1])
        raise EnvelopeError(
            f"No signature verified ({len(sigs)} tried): " + "; ".join(failures)
        )

    # Any verified signature over the legacy PAE makes the envelope legacy: a stock
    # verifier would not accept that signature.
    return PAE_LEGACY if PAE_LEGACY in encodings else PAE_DSSE_V1


def verify_envelope_encoding(
    envelope_bytes: bytes,
    expected_payload: bytes | None = None,
    *,
    require: str = "any",
) -> str:
    """Verify like :func:`verify_envelope` and return the PAE encoding that matched.

    Returns :data:`PAE_DSSE_V1` for a spec envelope (verifiable with stock DSSE
    tooling) or :data:`PAE_LEGACY` for one sealed through v0.102.x (verifiable with
    NovaFabric only). Raises :class:`EnvelopeError` exactly as ``verify_envelope``.
    """
    return _verify_envelope(envelope_bytes, expected_payload, require=require)


def extract_intent(envelope_bytes: bytes) -> Optional[SigningIntent]:
    """Return the signing intent from the first signature entry, or None.

    Does not verify the signature — call verify_envelope() first if you need
    authenticated intent extraction.
    """
    try:
        env = json.loads(envelope_bytes)
        sigs = env.get("signatures", [])
        if not sigs:
            return None
        raw = sigs[0].get("intent")
        if raw is None:
            return None
        try:
            return SigningIntent(raw)
        except ValueError:
            return None
    except Exception:
        return None


def _der_to_pem_cert(der: bytes) -> bytes:
    """Wrap DER bytes in PEM armor."""
    b64 = base64.b64encode(der).decode("ascii")
    lines = "\n".join(b64[i:i+64] for i in range(0, len(b64), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode()


def extract_payload(envelope_bytes: bytes) -> bytes:
    """Return the raw payload bytes from a DSSE envelope."""
    try:
        env = json.loads(envelope_bytes)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"Envelope is not valid JSON: {exc}") from exc
    return _b64_decode(env.get("payload", ""))
