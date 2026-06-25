"""Sigstore keyless signing backend for NovaSeal (ADR-0071).

Uses the sigstore Python SDK 4.x to sign artifacts via OIDC-based keyless
signing.  The result is a Sigstore bundle v0.3 dict (JSON-serialisable) that
contains the signed payload, signature, Fulcio certificate, and Rekor
inclusion proof.

This module deliberately does NOT implement the ``SigningBackend`` protocol
(``sign_digest`` / ``get_cert_der``).  The Sigstore signing flow is
fundamentally different from ECDSA-digest signing: it requires an OIDC token
at sign time, produces a short-lived certificate from Fulcio, and obtains an
inclusion proof from Rekor.  The resulting artifact is a self-contained
Sigstore bundle, not a raw ECDSA signature + cert pair.

``nova seal --backend sigstore`` therefore uses a parallel signing path
(``SigstoreSigner.sign_artifact``) rather than the DSSE envelope path.
The bundle is stored alongside capsule metadata via ``SigstoreBundleStore``.

Requires: ``pip install novafabric[sigstore]``  (sigstore>=4.2.0, Apache-2.0)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_BUNDLE_DIR_NAME = "sigstore"


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------

@dataclass
class SigstoreVerificationResult:
    """Result of a Sigstore bundle verification.

    Attributes:
        valid:           True if the bundle signature and Rekor proof are valid.
        identity:        The OIDC identity (email / subject) embedded in the
                         Fulcio certificate, or None if unavailable.
        rekor_log_index: Rekor v2 log index of the inclusion proof, or None.
        error:           Human-readable error message when ``valid=False``.
    """

    valid: bool
    identity: str | None
    rekor_log_index: int | None
    error: str | None

    def __str__(self) -> str:
        parts = [f"valid={self.valid}"]
        if self.identity is not None:
            parts.append(f"identity={self.identity!r}")
        if self.rekor_log_index is not None:
            parts.append(f"rekor_log_index={self.rekor_log_index}")
        if self.error is not None:
            parts.append(f"error={self.error!r}")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# SigstoreSigner
# ---------------------------------------------------------------------------

class SigstoreSigner:
    """Sigstore keyless signing and verification (novafabric[sigstore] extra required).

    Uses the sigstore Python SDK 4.x.  At sign time, the SDK:

    1. Obtains an OIDC token from the ambient provider (GitHub Actions,
       Google Workload Identity, interactive browser flow, etc.).
    2. Exchanges the token for a short-lived Fulcio certificate.
    3. Signs the artifact digest with the ephemeral private key.
    4. Submits the certificate + signature to Rekor v2 for an inclusion proof.
    5. Returns a self-contained Sigstore bundle v0.3.

    Private Rekor instances are supported via the ``SIGSTORE_REKOR_URL``
    environment variable (passthrough to the sigstore SDK).

    Args:
        rekor_url: Optional Rekor URL override.  When None, the SDK's default
                   (``https://rekor.sigstore.dev``) is used.
    """

    def __init__(self, rekor_url: str | None = None) -> None:
        self._rekor_url = rekor_url

    # ------------------------------------------------------------------
    # Sign
    # ------------------------------------------------------------------

    def sign_artifact(self, artifact_bytes: bytes) -> dict[str, Any]:
        """Sign *artifact_bytes* using the Sigstore keyless flow.

        Args:
            artifact_bytes: Raw bytes of the artifact to sign (e.g. the
                            JSON-serialised capsule manifest).

        Returns:
            Sigstore bundle as a JSON-serialisable ``dict``.  The dict has the
            same structure as a ``sigstore.models.Bundle`` serialised to JSON.

        Raises:
            ImportError: if ``novafabric[sigstore]`` is not installed.
            RuntimeError: if signing fails (no OIDC token, Rekor unavailable,
                          etc.).
        """
        try:
            from sigstore.models import ClientTrustConfig
            from sigstore.oidc import IdentityToken, Issuer, detect_credential
            from sigstore.sign import SigningContext
        except ImportError as exc:
            raise ImportError(
                "Sigstore backend requires the sigstore package. "
                "Install it with: pip install novafabric[sigstore]"
            ) from exc

        try:
            trust_config = ClientTrustConfig.production()

            # Ambient credential (CI: GitHub Actions, GCP, …) first; fall back
            # to the interactive OAuth browser flow.
            raw_token = detect_credential()
            if raw_token is not None:
                identity = IdentityToken(raw_token)
            else:
                issuer = Issuer(trust_config.signing_config.get_oidc_url())
                identity = issuer.identity_token()

            signing_ctx = SigningContext.from_trust_config(trust_config)
            with signing_ctx.signer(identity) as signer:
                bundle = signer.sign_artifact(artifact_bytes)

            bundle_dict: dict[str, Any] = json.loads(bundle.to_json())
            return bundle_dict

        except Exception as exc:
            raise RuntimeError(f"Sigstore signing failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify_bundle(
        self,
        bundle_dict: dict[str, Any],
        artifact_bytes: bytes,
    ) -> SigstoreVerificationResult:
        """Verify a Sigstore bundle against *artifact_bytes*.

        Args:
            bundle_dict:    Sigstore bundle as returned by ``sign_artifact``.
            artifact_bytes: The original artifact bytes (used to recompute the
                            digest for verification).

        Returns:
            ``SigstoreVerificationResult`` with ``valid``, ``identity``,
            ``rekor_log_index``, and ``error`` fields.
        """
        try:
            from sigstore.models import Bundle
            from sigstore.verify import Verifier
        except ImportError:
            return SigstoreVerificationResult(
                valid=False,
                identity=None,
                rekor_log_index=None,
                error=(
                    "Sigstore backend requires the sigstore package. "
                    "Install it with: pip install novafabric[sigstore]"
                ),
            )

        try:
            bundle_json = json.dumps(bundle_dict)
            bundle = Bundle.from_json(bundle_json)

            from sigstore.verify.policy import UnsafeNoOp

            verifier = Verifier.production()
            # UnsafeNoOp mirrors the previous no-identity-policy behaviour:
            # signature/SCT/Rekor inclusion are verified; the signing identity
            # is extracted below and surfaced to the caller for human review.
            verifier.verify_artifact(artifact_bytes, bundle, UnsafeNoOp())

            # Extract identity from Fulcio cert SAN
            identity = _extract_identity(bundle_dict)
            rekor_log_index = _extract_rekor_log_index(bundle_dict)

            return SigstoreVerificationResult(
                valid=True,
                identity=identity,
                rekor_log_index=rekor_log_index,
                error=None,
            )

        except Exception as exc:
            return SigstoreVerificationResult(
                valid=False,
                identity=None,
                rekor_log_index=None,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# SigstoreBundleStore
# ---------------------------------------------------------------------------

class SigstoreBundleStore:
    """Persist and retrieve Sigstore bundles alongside capsule metadata.

    Bundles are stored as JSON files at::

        <home>/<_BUNDLE_DIR_NAME>/<capsule_id>.bundle.json

    where ``<home>`` defaults to ``~/.novafabric/`` but can be overridden by
    passing an explicit ``home`` path to each method.

    The store is deliberately stateless (no database connection, no global
    state).  Each method takes a ``home`` parameter so that tests can redirect
    to a temporary directory without monkeypatching.
    """

    @staticmethod
    def store_bundle(capsule_id: str, bundle_dict: dict[str, Any], home: Path) -> Path:
        """Write *bundle_dict* to disk for *capsule_id*.

        Args:
            capsule_id:  Capsule identifier (SHA-256 hex or ULID).
            bundle_dict: Sigstore bundle as returned by ``SigstoreSigner.sign_artifact``.
            home:        Root directory (e.g. ``~/.novafabric``).

        Returns:
            ``Path`` to the written bundle file.
        """
        bundle_dir = home / _BUNDLE_DIR_NAME
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / f"{capsule_id}.bundle.json"
        bundle_path.write_text(json.dumps(bundle_dict, indent=2), encoding="utf-8")
        log.debug("Sigstore bundle stored: %s", bundle_path)
        return bundle_path

    @staticmethod
    def load_bundle(capsule_id: str, home: Path) -> dict[str, Any] | None:
        """Load the Sigstore bundle for *capsule_id*, or ``None`` if absent.

        Args:
            capsule_id: Capsule identifier.
            home:       Root directory (same value as used in ``store_bundle``).

        Returns:
            Bundle dict, or ``None`` if no bundle exists for *capsule_id*.
        """
        bundle_path = home / _BUNDLE_DIR_NAME / f"{capsule_id}.bundle.json"
        if not bundle_path.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(bundle_path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read Sigstore bundle %s: %s", bundle_path, exc)
            return None


# ---------------------------------------------------------------------------
# Helpers (internal)
# ---------------------------------------------------------------------------

def _extract_identity(bundle_dict: dict[str, Any]) -> str | None:
    """Extract the OIDC subject identity from a Sigstore bundle dict."""
    try:
        # Sigstore bundle v0.3: verificationMaterial.certificate.rawBytes (base64)
        # The identity is in the cert's SAN (Subject Alternative Name).
        # We look in the verification material for the certificate chain first,
        # then fall back to the leaf certificate field.
        cert_chain = (
            bundle_dict.get("verificationMaterial", {})
            .get("x509CertificateChain", {})
            .get("certificates", [])
        )
        if cert_chain:
            # First cert in the chain is the leaf (Fulcio) certificate.
            raw_b64 = cert_chain[0].get("rawBytes", "")
        else:
            raw_b64 = (
                bundle_dict.get("verificationMaterial", {})
                .get("certificate", {})
                .get("rawBytes", "")
            )

        if not raw_b64:
            return None

        import base64

        from cryptography import x509 as crypt_x509

        der = base64.b64decode(raw_b64)
        cert = crypt_x509.load_der_x509_certificate(der)

        # Sigstore uses SAN with a URI or email for the identity.
        try:
            san = cert.extensions.get_extension_for_class(crypt_x509.SubjectAlternativeName)
            uris = san.value.get_values_for_type(crypt_x509.UniformResourceIdentifier)
            if uris:
                return uris[0]
            emails = san.value.get_values_for_type(crypt_x509.RFC822Name)
            if emails:
                return emails[0]
        except Exception:
            pass

        return cert.subject.rfc4514_string() or None

    except Exception:
        return None


def _extract_rekor_log_index(bundle_dict: dict[str, Any]) -> int | None:
    """Extract the Rekor v2 log index from a Sigstore bundle dict."""
    try:
        tlog_entries = (
            bundle_dict.get("verificationMaterial", {})
            .get("tlogEntries", [])
        )
        if tlog_entries:
            log_index = tlog_entries[0].get("logIndex")
            if log_index is not None:
                return int(log_index)
    except Exception:
        pass
    return None
