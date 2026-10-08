"""Signer-identity trust levels reported by ``nova verify`` (ADR-0301, experimental).

A level states what the **verifier** established about who signed, never what the
sealer claims:

* ``none`` — no signature verified; nothing is attested.
* ``self-asserted`` — the signature verifies under the key in the envelope's own
  certificate, and nothing the verifier trusts vouches for that certificate. It proves
  the *holder of that key* signed. The certificate's subject and issuer are claims made
  by the envelope itself.
* ``local-ca-pinned`` — the certificate chains to a CA bundle the verifier supplied,
  and the anchor reached is a NovaFabric local seal CA (``nova seal init``). Proves
  continuity with that one installation, across key rotations — still no external
  identity.
* ``ca-anchored`` — the certificate chains to any other anchor the verifier supplied.
  Identity assurance is whatever that CA's issuance policy gives.

The NovaFabric subject marker can only *lower* a label (an anchor carrying it reads
``local-ca-pinned``, never ``ca-anchored``), so forging the marker gains nothing.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal.local_identity import LOCAL_IDENTITY_ORG

IDENTITY_NONE = "none"
IDENTITY_SELF_ASSERTED = "self-asserted"
IDENTITY_LOCAL_CA_PINNED = "local-ca-pinned"
IDENTITY_CA_ANCHORED = "ca-anchored"

#: Every value ``identity_trust`` can take, weakest first.
IDENTITY_TRUST_LEVELS: tuple[str, ...] = (
    IDENTITY_NONE,
    IDENTITY_SELF_ASSERTED,
    IDENTITY_LOCAL_CA_PINNED,
    IDENTITY_CA_ANCHORED,
)

#: The one-line claim each level permits (ADR-0301 "permitted claim" table).
IDENTITY_TRUST_STATEMENTS: dict[str, str] = {
    IDENTITY_NONE: "no valid signature; nothing about the signer is attested",
    IDENTITY_SELF_ASSERTED: (
        "proves the holder of this key signed; the key is not bound to any person, "
        "organisation or machine by anything you trust"
    ),
    IDENTITY_LOCAL_CA_PINNED: (
        "the key was certified by the NovaFabric local seal CA you supplied (continuity "
        "with that installation); still not an external identity"
    ),
    IDENTITY_CA_ANCHORED: (
        "the signer certificate chains to an anchor in the CA bundle you supplied; "
        "identity assurance is that CA's issuance policy"
    ),
}

#: CLI headline per level.
IDENTITY_TRUST_HEADLINES: dict[str, str] = {
    IDENTITY_NONE: "NONE",
    IDENTITY_SELF_ASSERTED: "SELF-ASSERTED local key",
    IDENTITY_LOCAL_CA_PINNED: "LOCAL CA PINNED (self-asserted installation)",
    IDENTITY_CA_ANCHORED: "CA-ANCHORED",
}


@dataclass(frozen=True)
class SignerCertificateInfo:
    """Unverified description of the first signature's embedded certificate."""

    subject: str
    issuer: str
    local_seal_identity: bool


def is_local_seal_name(name: x509.Name) -> bool:
    """True when *name* carries the ``O=`` marker ``nova seal init`` writes."""
    return any(
        attr.value == LOCAL_IDENTITY_ORG
        for attr in name.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    )


def signer_certificate_info(dsse_bytes: bytes) -> Optional[SignerCertificateInfo]:
    """Describe ``signatures[0].cert``; ``None`` when absent or unparseable. Never raises."""
    if not dsse_bytes:
        return None
    try:
        envelope = json.loads(dsse_bytes)
        cert_b64 = envelope["signatures"][0]["cert"]
        if not isinstance(cert_b64, str) or not cert_b64:
            return None
        normalised = cert_b64.replace("-", "+").replace("_", "/")
        der = base64.b64decode(normalised + "=" * (-len(normalised) % 4))
        cert = x509.load_der_x509_certificate(der)
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error):
        return None
    return SignerCertificateInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        local_seal_identity=is_local_seal_name(cert.subject),
    )


def unanchored_identity_trust(signature_ok: bool) -> str:
    """Level before any CA bundle is consulted: ``self-asserted`` or ``none``."""
    return IDENTITY_SELF_ASSERTED if signature_ok else IDENTITY_NONE


def anchored_identity_trust(anchor: Optional[x509.Certificate]) -> str:
    """Level once a verifier-supplied CA bundle validated the signer chain.

    *anchor* is the bundle certificate the chain ended at. When it cannot be identified
    (``None``) the weaker anchored level, ``local-ca-pinned``, is reported.
    """
    if anchor is not None and not is_local_seal_name(anchor.subject):
        return IDENTITY_CA_ANCHORED
    return IDENTITY_LOCAL_CA_PINNED
