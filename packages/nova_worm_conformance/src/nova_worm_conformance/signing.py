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
"""Honest signing for WORM conformance reports (FR-13).

This package is standalone by design (no hard ``novafabric`` dependency), so
NovaSeal is imported lazily and optionally. The cardinal rule enforced here:

    A bare hash is never presented as a cryptographic signature.

``sign_report_content`` therefore has two truthful outcomes:

* NovaSeal signing backend importable **and** a key + cert are available
  -> a genuine ECDSA-P256 signature (``status="signed"``).
* otherwise -> no signature at all; an honest ``content_sha256`` integrity
  digest and a ``status="unsigned"`` explanation (``detail``).
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Method label recorded on a genuinely-signed report.
NOVASEAL_ECDSA_P256 = "novaseal-ecdsa-p256"

_UNSIGNED_DETAIL = (
    "NovaSeal signing backend and/or a signing key were unavailable; this report "
    "carries a content_sha256 integrity digest but is NOT cryptographically signed."
)


@dataclass(frozen=True)
class SigningResult:
    """Outcome of an attempt to sign report content — honest by construction."""

    status: str  # "signed" | "unsigned"
    content_sha256: str  # hex SHA-256 digest of the signed payload (always present)
    signature: str | None = None  # base64 real signature, or None when unsigned
    method: str | None = None  # e.g. NOVASEAL_ECDSA_P256, or None when unsigned
    detail: str | None = None  # human-readable note (why unsigned, etc.)
    cert_b64: str | None = None  # base64 DER signer cert, when signed


def _load_novaseal_backend(key_path: Path | None, cert_path: Path | None) -> Any:
    """Return a NovaSeal ``LocalSigningBackend`` if one can genuinely sign, else None.

    Returns None (never raises) whenever real signing is impossible: no key/cert,
    files missing, or NovaSeal not installed. Callers treat None as "unsigned".
    NovaSeal is an optional, standalone-package dependency, hence the guarded import.
    """
    if key_path is None or cert_path is None:
        return None
    if not (Path(key_path).exists() and Path(cert_path).exists()):
        return None
    try:
        from novafabric.trust.novaseal.signing_backend import (  # type: ignore[import-untyped]
            LocalSigningBackend,
        )
    except ImportError:
        return None
    return LocalSigningBackend(Path(key_path), Path(cert_path))


def sign_report_content(
    body_bytes: bytes,
    *,
    key_path: Path | None = None,
    cert_path: Path | None = None,
) -> SigningResult:
    """Sign ``body_bytes`` honestly.

    Always computes an integrity digest. Produces a real ECDSA-P256 signature only
    when a NovaSeal signing backend and a key/cert are actually available; otherwise
    returns an unsigned result that says so plainly.
    """
    digest = hashlib.sha256(body_bytes).digest()
    content_sha256 = digest.hex()

    backend = _load_novaseal_backend(key_path, cert_path)
    if backend is None:
        return SigningResult(
            status="unsigned",
            content_sha256=content_sha256,
            signature=None,
            method=None,
            detail=_UNSIGNED_DETAIL,
        )

    try:
        raw_sig = backend.sign_digest(digest)  # real ECDSA P-256 over the digest
        cert_der = backend.get_cert_der()
    except Exception as exc:  # noqa: BLE001 — degrade to an honest unsigned result
        return SigningResult(
            status="unsigned",
            content_sha256=content_sha256,
            signature=None,
            method=None,
            detail=f"signing backend available but failed: {exc}",
        )

    return SigningResult(
        status="signed",
        content_sha256=content_sha256,
        signature=base64.b64encode(raw_sig).decode("ascii"),
        method=NOVASEAL_ECDSA_P256,
        detail=None,
        cert_b64=base64.b64encode(cert_der).decode("ascii"),
    )


def apply_signing(report: object, *, key_path: Path | None, cert_path: Path | None) -> object:
    """Sign ``report`` in place and copy the honest result onto its signing fields.

    Works on any object exposing ``signable_bytes()`` and the signing attributes
    of :class:`nova_worm_conformance.report.ConformanceReport`.
    """
    body = report.signable_bytes()  # type: ignore[attr-defined]
    result = sign_report_content(body, key_path=key_path, cert_path=cert_path)
    report.signing_status = result.status  # type: ignore[attr-defined]
    report.content_sha256 = result.content_sha256  # type: ignore[attr-defined]
    report.signing_method = result.method  # type: ignore[attr-defined]
    report.signing_detail = result.detail  # type: ignore[attr-defined]
    report.signing_cert = result.cert_b64  # type: ignore[attr-defined]
    report.novaseal_signature = result.signature  # type: ignore[attr-defined]
    return report
