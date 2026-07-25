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
"""W3C ``did:key`` + Verifiable Credentials for agentic identity (ADR-0075).

``did:key`` is a **self-certifying** DID method: the identifier *encodes* the
public key (multibase base58btc of the multicodec-prefixed key), so resolving a
DID to its key is pure decoding — **no network, no registry**. That makes an
agent's identity and its authorization credentials offline-verifiable, which is
what a sealed capsule needs.

A :class:`VerifiableCredential` records an authorization grant — an issuer DID
attesting that a subject DID holds a scope of capabilities until an expiry — with
an Ed25519 proof over the canonical credential. :func:`verify_credential` resolves
the issuer DID to its key and checks the proof and expiry. This composes with the
ADR-0106 delegation chain: a grant's ``grant_ref`` can point at such a VC.

No new runtime dependency: base58btc is implemented here (stdlib only) and the
signatures use the existing ``cryptography`` Ed25519.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

# multicodec varint prefix for an Ed25519 public key (0xed 0x01).
_ED25519_MULTICODEC = b"\xed\x01"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


class CredentialError(Exception):
    """A DID could not be resolved or a credential failed verification/issuance."""


# --------------------------------------------------------------------------- #
# base58btc (Bitcoin alphabet) — stdlib only
# --------------------------------------------------------------------------- #

def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_ALPHABET[rem] + out
    # Leading zero bytes become leading '1's.
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _B58_INDEX:
            raise CredentialError(f"invalid base58 character: {ch!r}")
        n = n * 58 + _B58_INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


# --------------------------------------------------------------------------- #
# did:key
# --------------------------------------------------------------------------- #

def did_key_from_public_key(public_key: bytes) -> str:
    """Encode a 32-byte Ed25519 public key as a ``did:key`` identifier."""
    if len(public_key) != 32:
        raise CredentialError(f"Ed25519 public key must be 32 bytes, got {len(public_key)}")
    return "did:key:z" + _b58encode(_ED25519_MULTICODEC + public_key)


def public_key_from_did_key(did: str) -> bytes:
    """Resolve a ``did:key`` identifier to its raw Ed25519 public key (no network)."""
    if not did.startswith("did:key:z"):
        raise CredentialError(f"not an Ed25519 did:key: {did!r}")
    decoded = _b58decode(did[len("did:key:z"):])
    if not decoded.startswith(_ED25519_MULTICODEC):
        raise CredentialError("did:key does not carry the Ed25519 multicodec prefix")
    key = decoded[len(_ED25519_MULTICODEC):]
    if len(key) != 32:
        raise CredentialError(f"resolved key is {len(key)} bytes, expected 32")
    return key


# --------------------------------------------------------------------------- #
# Verifiable Credentials
# --------------------------------------------------------------------------- #

class VerifiableCredential(BaseModel):
    """An authorization credential: issuer attests subject holds *authorization* until expiry."""

    model_config = {"frozen": True}

    issuer_did: str
    subject_did: str
    authorization: list[str] = Field(..., description="Capability URIs granted to the subject")
    issued_at: str
    expires_at: str
    proof: bytes = Field(..., description="Ed25519 signature by the issuer over the canonical VC")


class CredentialVerification(BaseModel):
    """The outcome of verifying a credential."""

    model_config = {"frozen": True}

    valid: bool
    issuer_did: str
    subject_did: str
    authorization: list[str]
    reason: str | None = None


def _credential_payload(
    *, issuer_did: str, subject_did: str, authorization: list[str], issued_at: str, expires_at: str
) -> bytes:
    obj = {
        "issuer_did": issuer_did,
        "subject_did": subject_did,
        "authorization": sorted(set(authorization)),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue_credential(
    issuer_key: Ed25519PrivateKey,
    *,
    issuer_did: str,
    subject_did: str,
    authorization: list[str],
    expires_at: str,
    issued_at: str | None = None,
    _skip_key_check: bool = False,
) -> VerifiableCredential:
    """Sign a :class:`VerifiableCredential` from *issuer_did* to *subject_did*.

    Unless ``_skip_key_check`` (tests only), the signing key must match the key
    encoded in *issuer_did*, so a credential can never be issued in a DID's name
    with an unrelated key.
    """
    if not _skip_key_check:
        if issuer_key.public_key().public_bytes_raw() != public_key_from_did_key(issuer_did):
            raise CredentialError("issuer_key does not match the key encoded in issuer_did")
    issued_at = issued_at or datetime.now(timezone.utc).isoformat()
    auth = sorted(set(authorization))
    payload = _credential_payload(
        issuer_did=issuer_did,
        subject_did=subject_did,
        authorization=auth,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return VerifiableCredential(
        issuer_did=issuer_did,
        subject_did=subject_did,
        authorization=auth,
        issued_at=issued_at,
        expires_at=expires_at,
        proof=issuer_key.sign(payload),
    )


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_credential(
    vc: VerifiableCredential, *, now: datetime | None = None
) -> CredentialVerification:
    """Verify a credential's proof against its issuer DID and check its expiry.

    Never raises — a malformed DID, a bad signature, or an expired credential all
    return ``valid=False`` with a reason.
    """
    now = now or datetime.now(timezone.utc)
    base = CredentialVerification(
        valid=False,
        issuer_did=vc.issuer_did,
        subject_did=vc.subject_did,
        authorization=list(vc.authorization),
    )
    try:
        issuer_key = public_key_from_did_key(vc.issuer_did)
    except CredentialError as exc:
        return base.model_copy(update={"reason": f"issuer DID unresolvable: {exc}"})

    payload = _credential_payload(
        issuer_did=vc.issuer_did,
        subject_did=vc.subject_did,
        authorization=list(vc.authorization),
        issued_at=vc.issued_at,
        expires_at=vc.expires_at,
    )
    try:
        Ed25519PublicKey.from_public_bytes(issuer_key).verify(bytes(vc.proof), payload)
    except InvalidSignature:
        return base.model_copy(update={"reason": "credential proof does not verify"})

    try:
        if _parse_dt(vc.expires_at) <= now:
            return base.model_copy(update={"reason": "credential is expired"})
    except ValueError:
        return base.model_copy(update={"reason": "credential has an unparseable expiry"})

    return base.model_copy(update={"valid": True})


__all__ = [
    "CredentialError",
    "CredentialVerification",
    "VerifiableCredential",
    "did_key_from_public_key",
    "issue_credential",
    "public_key_from_did_key",
    "verify_credential",
]
