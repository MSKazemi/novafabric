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
"""x509 certificate-pinned offline signing identity (ADR-0055, ``x509`` profile).

The ``x509`` signing profile signs with a long-lived key (ECDSA P-256 or RSA-2048+),
embeds the operator-issued X.509 certificate alongside the signature, and requires no
external service at signing or verification time.

This module ships the **offline, certificate-pinned** core:

* :class:`X509SigningIdentity` loads a PKCS#8 PEM key + PEM certificate and signs a
  payload, producing an :class:`X509Signature` that carries the algorithm, the raw
  signature bytes, and the embedded certificate.
* :func:`verify_x509_signature` verifies a signature by (1) checking the embedded
  certificate is in the operator's **pinned trust set** (by SHA-256 fingerprint) and
  (2) verifying the signature under the public key extracted from that certificate.

The trust anchor here is the **pinned fingerprint**, not a CA path: an operator lists the
exact certificates it trusts. This is the air-gap-friendly model and uses only the
``cryptography`` library's standard signature-verification primitives — no hand-rolled
crypto. **Deferred (unchanged design intent):** full CA-bundle chain/path validation
(ADR-0055 verification step 2) and the Rekor inclusion-proof option, both of which layer
on top of this without changing the signature format.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.x509 import Certificate, load_pem_x509_certificate
from cryptography.x509.oid import NameOID
from pydantic import BaseModel

_ALG_ECDSA_P256 = "ecdsa-p256-sha256"
_ALG_RSA_PSS = "rsa-pss-sha256"


class X509IdentityError(ValueError):
    """Raised when a key/certificate cannot be loaded or a key type is unsupported."""


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
            raise X509IdentityError(
                "unsupported key type; expected ECDSA P-256 or RSA (2048+)"
            )
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


def verify_x509_signature(
    payload: bytes,
    signature: X509Signature,
    *,
    pinned_fingerprints: set[str],
) -> X509VerifyResult:
    """Verify an x509 signature against an operator's pinned trust set.

    Two conditions must both hold: the embedded certificate's SHA-256 fingerprint is in
    ``pinned_fingerprints`` (the trust anchor), and the signature verifies under the
    certificate's public key. Never raises — a failure is a ``valid=False`` result.
    """
    try:
        cert = load_pem_x509_certificate(signature.certificate_pem.encode("utf-8"))
    except ValueError as exc:
        return X509VerifyResult(valid=False, reason=f"unparseable certificate: {exc}")

    fingerprint = _fingerprint(cert)
    cn = _subject_cn(cert)
    if fingerprint not in pinned_fingerprints:
        return X509VerifyResult(
            valid=False,
            reason="certificate not in the pinned trust set (untrusted signer)",
            subject_common_name=cn,
            certificate_fingerprint=fingerprint,
        )

    public_key = cert.public_key()
    try:
        if signature.algorithm == _ALG_ECDSA_P256 and isinstance(
            public_key, ec.EllipticCurvePublicKey
        ):
            public_key.verify(signature.signature, payload, ECDSA(hashes.SHA256()))
        elif signature.algorithm == _ALG_RSA_PSS and isinstance(
            public_key, rsa.RSAPublicKey
        ):
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
            return X509VerifyResult(
                valid=False,
                reason=f"unsupported algorithm/key combination: {signature.algorithm}",
                subject_common_name=cn,
                certificate_fingerprint=fingerprint,
            )
    except InvalidSignature:
        return X509VerifyResult(
            valid=False,
            reason="signature does not verify under the certificate's public key",
            subject_common_name=cn,
            certificate_fingerprint=fingerprint,
        )
    return X509VerifyResult(
        valid=True,
        reason="signature verified and certificate is pinned",
        subject_common_name=cn,
        certificate_fingerprint=fingerprint,
    )
