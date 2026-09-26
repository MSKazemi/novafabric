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

"""Accountability handoff receipts — ADR-0150 D3/P3 (NF-189).

A :class:`HandoffRecord` records that responsibility for a ``scope`` passed
from one party to another at a conversation turn — "the agent handed the
refund decision back to a human at 14:07".

**Separation of duties.** ``from_party`` and ``to_party`` must be different
parties (ADR-0058's identity-distinctness discipline), decided by
:func:`same_party`. The comparison is made on NFKC-normalised, case-folded
refs, so ``human:fp:ABCD…`` and ``human:fp:abcd…`` count as the same party.
Two fingerprint-shaped refs (``human:fp:<hex>`` / ``agent:fp:<hex>``) are the
same party when one hex string is a prefix of the other — both name a prefix
of the same ``sha256(raw key)``, so ``human:fp:<first 16 hex>`` and
``human:fp:<first 32 hex>`` are one key under two spellings (and ``human:`` vs
``agent:`` does not make one key two parties). A self-handoff hidden behind a
case change or a longer fingerprint is refused, never recorded. The ``fp:``
namespace is reserved: a ``human:fp:`` / ``agent:fp:`` ref must carry 16..64
hex characters, so a short (unbindable) fingerprint cannot sidestep the
prefix comparison.

**Signature (``sig``) — what is and is not verified offline.** Two forms:

- ``ed25519:<base64url>`` with ``signer`` (``from_party`` or ``to_party``)
  and ``signer_key`` (``ed25519:<64 hex>`` raw public key). The signature is
  checked over :func:`handoff_signing_payload` with the ADR-0058 keyring
  primitive (:func:`novafabric.trust.keyring.verify_sig`). When the signer's
  ref is fingerprint-shaped (``human:fp:<hex>`` / ``agent:fp:<hex>``, 16..64
  hex) the key is additionally checked against that fingerprint
  (``sha256(raw key)`` prefix — the keyring's construction), which binds the
  key to the party offline. For any other ref shape (a DID, a SPIFFE id) the
  key→party binding is **not** established here; it is reported ``unbound``,
  not assumed.
- ``sha256:<hex>`` — a digest *reference* to a signature envelope held
  elsewhere (e.g. a DSSE produced by an external signer). Recorded, surfaced as
  ``reference_only``, and never claimed as verified.

Record-only (I-4): a valid signature shows that the key signed this handoff
record; it does not show that the transfer of responsibility was authorised,
lawful, or adequate. Stored as a list at ``facets.conversation.handoff``.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from novafabric.hitl._records import (
    AccountabilityRecordError,
    LoadedRecords,
    RecordContentError,
    RecordOutcome,
    check_code,
    check_code_or_digest,
    check_extras,
    check_timestamp,
    check_turn_ref,
    load_records,
    record_fail_open,
)
from novafabric.hitl.conversation import IdentityRefError, _validate_identity_ref
from novafabric.trust.keyring import sign_payload, verify_sig

logger = logging.getLogger(__name__)

RECORD_KEY = "handoff"

#: Domain tag in the signing payload: a handoff signature can never be replayed
#: as a signature over some other NovaFabric object with the same JSON.
SIGNING_DOMAIN = "novafabric.hitl.handoff.v1"

#: Scope codes per handoff. Responsibility for a few named things passes at a
#: time; hundreds of codes would be a policy document inlined into a receipt.
MAX_SCOPE_CODES = 32

_ED25519_SIG_RE = re.compile(r"ed25519:[A-Za-z0-9_-]{86}")
_SHA256_REF_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SIGNER_KEY_RE = re.compile(r"ed25519:[0-9a-f]{64}")
#: A fingerprint-shaped identity ref: the part after ``fp:`` is a sha256 prefix.
_FP_REF_RE = re.compile(r"(?:human|agent):fp:([0-9a-fA-F]{16,64})")

_HANDOFF_FIELDS = frozenset(
    {
        "turn_ref",
        "from_party",
        "to_party",
        "at",
        "scope",
        "reason",
        "sig",
        "signer",
        "signer_key",
    }
)

SignatureStatus = Literal["valid", "invalid", "reference_only"]
KeyBinding = Literal["fingerprint_match", "fingerprint_mismatch", "unbound", "not_applicable"]


class SelfHandoffError(AccountabilityRecordError):
    """Raised when ``from_party`` and ``to_party`` are the same party.

    A handoff to oneself transfers no responsibility; recording it would
    manufacture a separation-of-duties event that never happened (ADR-0058).
    """


class HandoffSignatureError(AccountabilityRecordError):
    """Raised when the ``sig`` / ``signer`` / ``signer_key`` triple is malformed."""


def party_key(ref: str) -> str:
    """Return the comparison form of an identity ref (NFKC + casefold)."""
    return unicodedata.normalize("NFKC", ref).casefold()


def fingerprint_hex(ref: str) -> str | None:
    """Return the lower-case fingerprint hex of a fingerprint-shaped ref, else None."""
    match = _FP_REF_RE.fullmatch(party_key(ref))
    return None if match is None else match.group(1)


def same_party(a: str, b: str) -> bool:
    """Return True when ``a`` and ``b`` name the same party.

    Equal :func:`party_key` forms are the same party. Two fingerprint-shaped
    refs are also the same party when one fingerprint is a prefix of the other
    (case-insensitive, regardless of the ``human:`` / ``agent:`` kind): both
    are prefixes of one ``sha256(raw key)``, i.e. one key under two names.
    """
    if party_key(a) == party_key(b):
        return True
    fa, fb = fingerprint_hex(a), fingerprint_hex(b)
    if fa is None or fb is None:
        return False
    return fa.startswith(fb) or fb.startswith(fa)


def _check_party(value: object, *, field_name: str) -> str:
    ref = _validate_identity_ref(value, field=field_name)
    if not ref.startswith(("human:", "agent:")):
        raise IdentityRefError(
            f"{field_name} must be a 'human:' or 'agent:' ref — responsibility "
            "passes between people and agents, not system components"
        )
    if party_key(ref).split(":", 2)[1] == "fp" and fingerprint_hex(ref) is None:
        raise IdentityRefError(
            f"{field_name} is a fingerprint ref but not 16..64 hex characters; "
            "the 'fp:' namespace is reserved for sha256 key-fingerprint prefixes"
        )
    return ref


class HandoffRecord(BaseModel):
    """NF-189: responsibility for ``scope`` passed ``from_party`` → ``to_party``."""

    model_config = ConfigDict(extra="allow")

    turn_ref: str
    from_party: str
    to_party: str
    at: str
    scope: list[str]
    reason: str
    sig: str
    signer: str | None = None
    signer_key: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_extras(cls, data: Any) -> Any:
        return check_extras(data, known=_HANDOFF_FIELDS)

    @field_validator("turn_ref", mode="before")
    @classmethod
    def _check_turn_ref(cls, v: object) -> str:
        return check_turn_ref(v)

    @field_validator("from_party", mode="before")
    @classmethod
    def _check_from(cls, v: object) -> str:
        return _check_party(v, field_name="from_party")

    @field_validator("to_party", mode="before")
    @classmethod
    def _check_to(cls, v: object) -> str:
        return _check_party(v, field_name="to_party")

    @field_validator("signer", mode="before")
    @classmethod
    def _check_signer(cls, v: object) -> str | None:
        return None if v is None else _check_party(v, field_name="signer")

    @field_validator("at", mode="before")
    @classmethod
    def _check_at(cls, v: object) -> str:
        return check_timestamp(v, field_name="at")

    @field_validator("scope", mode="before")
    @classmethod
    def _check_scope(cls, v: object) -> list[str]:
        if isinstance(v, str) or not isinstance(v, Sequence):
            raise AccountabilityRecordError("scope must be a list of codes")
        if not 1 <= len(v) <= MAX_SCOPE_CODES:
            raise AccountabilityRecordError(f"scope must hold 1..{MAX_SCOPE_CODES} codes")
        items = [check_code(item, field_name="scope") for item in v]
        if len(set(items)) != len(items):
            raise AccountabilityRecordError("scope lists a code twice")
        return items

    @field_validator("reason", mode="before")
    @classmethod
    def _check_reason(cls, v: object) -> str:
        return check_code_or_digest(v, field_name="reason")

    @field_validator("sig", mode="before")
    @classmethod
    def _check_sig(cls, v: object) -> str:
        if isinstance(v, str) and (_ED25519_SIG_RE.fullmatch(v) or _SHA256_REF_RE.fullmatch(v)):
            return v
        raise RecordContentError(
            "sig must be 'ed25519:<86-char base64url signature>' or a 'sha256:' "
            "digest reference to a signature envelope held elsewhere"
        )

    @field_validator("signer_key", mode="before")
    @classmethod
    def _check_signer_key(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, str) and _SIGNER_KEY_RE.fullmatch(v):
            return v
        raise HandoffSignatureError("signer_key must be 'ed25519:<64 lower-case hex>'")

    @model_validator(mode="after")
    def _check_consistency(self) -> HandoffRecord:
        # Two fingerprint refs that both prefix-match sha256(signer_key) are
        # prefixes of one string, hence of each other: same_party covers the
        # "both parties are the signing key" case without a separate check.
        if same_party(self.from_party, self.to_party):
            raise SelfHandoffError(
                "from_party and to_party are the same party; a handoff must pass "
                "responsibility between two distinct parties (ADR-0058)"
            )
        if self.sig.startswith("ed25519:"):
            if self.signer is None or self.signer_key is None:
                raise HandoffSignatureError("an ed25519 sig requires both signer and signer_key")
            if not (
                same_party(self.signer, self.from_party) or same_party(self.signer, self.to_party)
            ):
                raise HandoffSignatureError("signer must be from_party or to_party")
        elif self.signer_key is not None:
            raise HandoffSignatureError(
                "signer_key is only meaningful with an ed25519 sig; a sha256 sig is "
                "a reference to an external envelope"
            )
        return self


# ── Signing ───────────────────────────────────────────────────────────────


def handoff_signing_payload(fields: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes a handoff signature covers.

    Every non-null field except ``sig`` itself — including ``signer``,
    ``signer_key`` and extension fields — under :data:`SIGNING_DOMAIN`, as
    sorted-key compact ASCII JSON.
    """
    body = {k: v for k, v in fields.items() if k != "sig" and v is not None}
    canonical = json.dumps(
        {"domain": SIGNING_DOMAIN, "handoff": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return canonical.encode("ascii")


def sign_handoff(
    private_key: Ed25519PrivateKey,
    *,
    signer: str,
    turn_ref: str,
    from_party: str,
    to_party: str,
    at: str,
    scope: Sequence[str],
    reason: str,
    **extensions: Any,
) -> HandoffRecord:
    """Build a handoff record signed by ``signer`` with ``private_key``.

    Strict (raises on a malformed or self-handoff record). The signature is
    produced with the ADR-0058 keyring primitive over
    :func:`handoff_signing_payload`.
    """
    fields: dict[str, Any] = {
        "turn_ref": turn_ref,
        "from_party": from_party,
        "to_party": to_party,
        "at": at,
        "scope": scope if isinstance(scope, str) else list(scope),
        "reason": reason,
        "signer": signer,
        "signer_key": "ed25519:" + private_key.public_key().public_bytes_raw().hex(),
        **extensions,
    }
    placeholder = "ed25519:" + "A" * 86
    body = HandoffRecord.model_validate({**fields, "sig": placeholder}).model_dump(
        exclude_none=True
    )
    sig = "ed25519:" + sign_payload(private_key, handoff_signing_payload(body))
    return HandoffRecord.model_validate({**body, "sig": sig})


@dataclass(frozen=True)
class SignatureCheck:
    """What could be checked offline about one handoff's signature."""

    signature: SignatureStatus
    key_binding: KeyBinding

    @property
    def ok(self) -> bool:
        """False only on a positively *bad* signature or key binding.

        ``reference_only`` and ``unbound`` are surfaced, not failed: they are
        the honest "not verifiable here" states, not evidence of tampering.
        """
        return self.signature != "invalid" and self.key_binding != "fingerprint_mismatch"

    def to_dict(self) -> dict[str, Any]:
        """Deterministic JSON-ready form."""
        return {"signature": self.signature, "key_binding": self.key_binding, "ok": self.ok}


def check_handoff_signature(record: HandoffRecord) -> SignatureCheck:
    """Check a handoff's signature offline; never raises."""
    if not record.sig.startswith("ed25519:") or record.signer_key is None:
        return SignatureCheck("reference_only", "not_applicable")
    raw = bytes.fromhex(record.signer_key.removeprefix("ed25519:"))
    try:
        public_key = Ed25519PublicKey.from_public_bytes(raw)
        payload = handoff_signing_payload(record.model_dump(exclude_none=True))
        valid = verify_sig(public_key, record.sig.removeprefix("ed25519:"), payload)
    except (ValueError, binascii.Error):
        valid = False
    binding: KeyBinding = "unbound"
    match = _FP_REF_RE.fullmatch(record.signer or "")
    if match is not None:
        fp = match.group(1).lower()
        binding = (
            "fingerprint_match"
            if hashlib.sha256(raw).hexdigest().startswith(fp)
            else "fingerprint_mismatch"
        )
    return SignatureCheck("valid" if valid else "invalid", binding)


# ── Storage ───────────────────────────────────────────────────────────────


def record_handoff(
    capsule: dict[str, Any], record: HandoffRecord | Mapping[str, Any]
) -> RecordOutcome:
    """Attach a handoff to ``facets.conversation.handoff``; never raises.

    A record whose ed25519 signature does not verify, or whose key contradicts
    a fingerprint-shaped signer ref, is not recorded: storing a receipt that
    already fails its own check would only plant a defect for a later reader.
    """
    try:
        validated = (
            record if isinstance(record, HandoffRecord) else HandoffRecord.model_validate(record)
        )
    except Exception:  # noqa: BLE001 — record_fail_open logs and reports it
        return record_fail_open(capsule, RECORD_KEY, HandoffRecord, record, unique_per_turn=False)
    if not check_handoff_signature(validated).ok:
        logger.warning("hitl: handoff record not attached (signature does not verify)")
        return RecordOutcome(capsule, False, "handoff signature does not verify")
    return record_fail_open(capsule, RECORD_KEY, HandoffRecord, validated, unique_per_turn=False)


def load_handoffs(capsule: Mapping[str, Any]) -> LoadedRecords[HandoffRecord]:
    """Read every stored handoff (malformed or self-handoffs become defects)."""
    return load_records(capsule, RECORD_KEY, HandoffRecord)
