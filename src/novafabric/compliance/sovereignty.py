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
"""Multi-region log sovereignty — jurisdiction site-seals + residency policy.

Implements the pure-code core of ADR-0077 (Multi-Region Log Sovereignty for
Jurisdictional Data Residency). Two properties, both verifiable offline with no
infrastructure:

**Cryptographic proof of residency (ADR-0077 §3).** A capsule carrying
``jurisdiction=EU`` must be countersigned by the *EU* jurisdiction site-seal key.
:func:`verify_site_seal` verifies the seal's signature under the public key
*registered for the jurisdiction the seal claims* — so a capsule that claims ``EU``
but was sealed by any other key is rejected. The residency claim cannot be forged.

**Residency-respecting reads (ADR-0077 §2).** :func:`check_cross_jurisdiction_read`
denies a cross-border read unless the :class:`ResidencyPolicy` explicitly grants
that border. Same-jurisdiction reads are always allowed; the grant is directional.

Deferred to later ADR-0077 slices (infra-gated, not shipped here): per-jurisdiction
capsule stores and the storage-layer routing that binds a capsule to its regional
backend (ADR-0077 §2 second half), and the jurisdiction-scoped lineage traversal
filter (ADR-0077 §4). This module ships only the format + verifier + policy, which
those slices consume without a format change.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

# Domain separation: the byte prefix a site-seal signature covers, so a signature
# minted here can never be confused with any other Ed25519 signature in the system.
_SEAL_CONTEXT = b"novafabric.sovereignty.site-seal.v1\x00"


class SiteSeal(BaseModel):
    """A jurisdiction-scoped countersignature binding a digest to a jurisdiction.

    ``jurisdiction`` is the region the sealing site *claims* (e.g. ``"EU"``).
    ``signature`` is an Ed25519 signature, by that site's key, over the
    domain-separated ``(jurisdiction, content_digest)`` tuple.
    """

    jurisdiction: str
    content_digest: str
    signature: bytes


class ResidencyPolicy(BaseModel):
    """Which cross-jurisdiction reads are permitted.

    ``allow_cross_read`` maps a *data* jurisdiction to the list of *reader*
    jurisdictions permitted to read that data. The grant is directional: listing
    ``CH`` under ``EU`` lets a CH reader read EU data, not the reverse.
    """

    allow_cross_read: dict[str, list[str]] = Field(default_factory=dict)


class ReadDecision(BaseModel):
    """Outcome of a residency check."""

    allowed: bool
    cross_jurisdiction: bool
    reason: str


def _seal_message(jurisdiction: str, content_digest: str) -> bytes:
    return _SEAL_CONTEXT + jurisdiction.encode("utf-8") + b"\x00" + content_digest.encode("utf-8")


def issue_site_seal(
    jurisdiction_key: Ed25519PrivateKey,
    *,
    jurisdiction: str,
    content_digest: str,
) -> SiteSeal:
    """Countersign ``content_digest`` for ``jurisdiction`` with a site-seal key.

    Raises :class:`ValueError` if ``jurisdiction`` or ``content_digest`` is empty —
    an unlabelled seal proves nothing about residency.
    """
    if not jurisdiction:
        raise ValueError("jurisdiction is required for a site-seal")
    if not content_digest:
        raise ValueError("content_digest is required for a site-seal")
    signature = jurisdiction_key.sign(_seal_message(jurisdiction, content_digest))
    return SiteSeal(
        jurisdiction=jurisdiction,
        content_digest=content_digest,
        signature=signature,
    )


def verify_site_seal(
    seal: SiteSeal,
    *,
    jurisdiction_pubkeys: dict[str, bytes],
) -> bool:
    """Verify a site-seal against the key registered for the jurisdiction it claims.

    Returns ``True`` only if the jurisdiction the seal claims has a registered public
    key *and* the seal's signature verifies under that key over its
    ``(jurisdiction, content_digest)`` tuple. A capsule claiming a jurisdiction it was
    not sealed by, a tampered digest, or an unknown jurisdiction all return ``False``.
    Never raises — a verification failure is a ``False``, not an exception.
    """
    raw = jurisdiction_pubkeys.get(seal.jurisdiction)
    if raw is None:
        return False
    try:
        pubkey = Ed25519PublicKey.from_public_bytes(raw)
        pubkey.verify(seal.signature, _seal_message(seal.jurisdiction, seal.content_digest))
    except (InvalidSignature, ValueError):
        return False
    return True


def check_cross_jurisdiction_read(
    capsule_jurisdiction: str,
    reader_jurisdiction: str,
    policy: ResidencyPolicy,
) -> ReadDecision:
    """Decide whether ``reader_jurisdiction`` may read ``capsule_jurisdiction`` data.

    Same-jurisdiction reads are always allowed. A cross-border read is allowed only
    when ``policy.allow_cross_read[capsule_jurisdiction]`` lists the reader.
    """
    if capsule_jurisdiction == reader_jurisdiction:
        return ReadDecision(
            allowed=True,
            cross_jurisdiction=False,
            reason="same-jurisdiction read",
        )
    granted = reader_jurisdiction in policy.allow_cross_read.get(capsule_jurisdiction, [])
    return ReadDecision(
        allowed=granted,
        cross_jurisdiction=True,
        reason=(
            f"cross-jurisdiction read {capsule_jurisdiction}->{reader_jurisdiction} "
            + ("granted by residency policy" if granted else "denied: no federation grant")
        ),
    )
