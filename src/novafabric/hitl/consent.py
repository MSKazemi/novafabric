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

"""Consent receipts — ADR-0150 D3/P3 (NF-183).

A :class:`ConsentReceipt` records, in an ISO/IEC TS 27560-shaped structure,
that a data subject gave consent for a purpose and a processing scope. Field
mapping to the 27560 / W3C DPV consent-record vocabulary:

=================  =============================================
``consent_id``     consent record identifier
``subject_ref``    data subject (pseudonymous ``human:`` ref)
``purpose``        purpose (a DPV concept code, e.g. ``dpv:ServiceProvision``)
``action``         processing operations (DPV concept codes)
``given_at``       consent event time
``expiry``         consent duration end (optional)
``withdrawable``   whether withdrawal is possible
``withdrawn_at``   withdrawal event time (optional)
``receipt_digest`` ``sha256:`` over the canonical receipt body
=================  =============================================

**Record-only (I-4).** NovaFabric records the receipt and re-checks that it is
internally intact. It does **not** determine that the consent was freely
given, informed, specific, lawful or otherwise legally valid.

**Digest coverage.** ``receipt_digest`` covers every field the receipt was
*given* with — including extension fields — except ``receipt_digest`` itself
and ``withdrawn_at``. Withdrawal is a later event on the same receipt: it must
not change the identity of the consent that was withdrawn, so recording it
leaves the digest intact while :func:`withdraw_consent` refuses to un-withdraw
or move a withdrawal.

**Storage.** A list at ``facets.conversation.consent`` (the storage deviation
recorded in ADR-0150). Unlike the other P3 records a consent receipt need not
anchor to a turn — consent is often given before the conversation — so a
receipt may be recorded on a capsule with no turns; ``turn_ref`` is optional
and, when present, must resolve.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from novafabric.hitl._records import (
    MAX_RECORDS_PER_KIND,
    AccountabilityRecordError,
    LoadedRecords,
    RecordContentError,
    RecordDefect,
    RecordOutcome,
    check_digest,
    check_extras,
    check_human_ref,
    check_timestamp,
    check_turn_ref,
    conversation_block,
    load_records,
)
from novafabric.hitl.conversation import (
    FACET_NAME,
    SCHEMA_VERSION,
    ConversationError,
    _parse_at,
    facet_from_capsule,
    resolve_turn,
)

logger = logging.getLogger(__name__)

RECORD_KEY = "consent"

#: Domain tag mixed into every receipt digest so a consent-receipt digest can
#: never collide with a digest of the same JSON computed for another purpose.
RECEIPT_DOMAIN = "novafabric.hitl.consent-receipt.v1"

#: Processing operations per receipt. 27560 records list a handful; a list of
#: hundreds is a data dump routed through a receipt.
MAX_ACTIONS = 32

#: A consent-record id: ULID, UUID, or a short opaque token. No whitespace.
_CONSENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

#: A purpose / processing concept: a DPV term (``dpv:ServiceProvision``), a
#: CURIE, or an IRI (``https://w3id.org/dpv#Store``). Mixed case is allowed
#: because DPV concepts are CamelCase; whitespace is not, so prose cannot pass.
_CONCEPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,255}")

_CONSENT_FIELDS = frozenset(
    {
        "consent_id",
        "subject_ref",
        "purpose",
        "action",
        "given_at",
        "expiry",
        "withdrawable",
        "withdrawn_at",
        "receipt_digest",
        "turn_ref",
    }
)

#: Fields outside the receipt digest (see module docstring).
_UNDIGESTED = frozenset({"receipt_digest", "withdrawn_at"})

CONSENT_NOTICE = (
    "Record-only: NovaFabric records this consent receipt and re-checks its digest. "
    "It does not assert that the consent was freely given, informed, specific, "
    "lawful, or otherwise legally valid (ADR-0150 D3/D7)."
)


class ConsentReceiptError(AccountabilityRecordError):
    """Raised when a consent receipt is internally inconsistent.

    Covers an ``expiry`` or ``withdrawn_at`` earlier than ``given_at``, a
    withdrawal recorded on a non-withdrawable receipt, and a stored
    ``receipt_digest`` that does not match the receipt body on construction.
    """


def _check_concept(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _CONCEPT_RE.fullmatch(value):
        raise RecordContentError(
            f"{field_name} must be a purpose/processing concept code (e.g. "
            "'dpv:ServiceProvision'); no whitespace, <= 256 chars, never prose"
        )
    return value


class ConsentReceipt(BaseModel):
    """NF-183: an ISO/IEC TS 27560-shaped consent receipt."""

    model_config = ConfigDict(extra="allow")

    consent_id: str
    subject_ref: str
    purpose: str
    action: list[str]
    given_at: str
    withdrawable: bool
    receipt_digest: str
    expiry: str | None = None
    withdrawn_at: str | None = None
    turn_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_extras(cls, data: Any) -> Any:
        return check_extras(data, known=_CONSENT_FIELDS)

    @field_validator("consent_id", mode="before")
    @classmethod
    def _check_consent_id(cls, v: object) -> str:
        if not isinstance(v, str) or not _CONSENT_ID_RE.fullmatch(v):
            raise RecordContentError(
                "consent_id must be an opaque id (letters, digits, '._:-'; <= 128 chars)"
            )
        return v

    @field_validator("subject_ref", mode="before")
    @classmethod
    def _check_subject(cls, v: object) -> str:
        return check_human_ref(v, field_name="subject_ref")

    @field_validator("purpose", mode="before")
    @classmethod
    def _check_purpose(cls, v: object) -> str:
        return _check_concept(v, field_name="purpose")

    @field_validator("action", mode="before")
    @classmethod
    def _check_action(cls, v: object) -> list[str]:
        if isinstance(v, str) or not isinstance(v, Sequence):
            raise ConsentReceiptError("action must be a list of processing concept codes")
        if not 1 <= len(v) <= MAX_ACTIONS:
            raise ConsentReceiptError(f"action must hold 1..{MAX_ACTIONS} concept codes")
        items = [_check_concept(item, field_name="action") for item in v]
        if len(set(items)) != len(items):
            raise ConsentReceiptError("action lists a processing concept twice")
        return items

    @field_validator("given_at", mode="before")
    @classmethod
    def _check_given_at(cls, v: object) -> str:
        return check_timestamp(v, field_name="given_at")

    @field_validator("expiry", mode="before")
    @classmethod
    def _check_expiry(cls, v: object) -> str | None:
        return None if v is None else check_timestamp(v, field_name="expiry")

    @field_validator("withdrawn_at", mode="before")
    @classmethod
    def _check_withdrawn_at(cls, v: object) -> str | None:
        return None if v is None else check_timestamp(v, field_name="withdrawn_at")

    @field_validator("withdrawable", mode="before")
    @classmethod
    def _check_withdrawable(cls, v: object) -> bool:
        # Strict: a "false" string or 0 must not coerce into a boolean claim.
        if not isinstance(v, bool):
            raise ConsentReceiptError("withdrawable must be a boolean")
        return v

    @field_validator("receipt_digest", mode="before")
    @classmethod
    def _check_receipt_digest(cls, v: object) -> str:
        return check_digest(v, field_name="receipt_digest")

    @field_validator("turn_ref", mode="before")
    @classmethod
    def _check_turn_ref(cls, v: object) -> str | None:
        return None if v is None else check_turn_ref(v)

    @model_validator(mode="after")
    def _check_times(self) -> ConsentReceipt:
        given = _parse_at(self.given_at, field="given_at")
        if self.expiry is not None and _parse_at(self.expiry, field="expiry") <= given:
            raise ConsentReceiptError("expiry must be later than given_at")
        if self.withdrawn_at is not None:
            if not self.withdrawable:
                raise ConsentReceiptError("withdrawn_at is set on a non-withdrawable receipt")
            if _parse_at(self.withdrawn_at, field="withdrawn_at") < given:
                raise ConsentReceiptError("withdrawn_at is earlier than given_at")
        return self


# ── Digest ────────────────────────────────────────────────────────────────


def _body(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k not in _UNDIGESTED and v is not None}


def compute_receipt_digest(fields: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` receipt digest over the canonical receipt body.

    The body is every non-null field except ``receipt_digest`` and
    ``withdrawn_at``, serialised as sorted-key compact JSON under
    :data:`RECEIPT_DOMAIN`. Pure; the caller's mapping is not modified.
    """
    canonical = json.dumps(
        {"domain": RECEIPT_DOMAIN, "receipt": _body(fields)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def build_consent_receipt(
    *,
    consent_id: str,
    subject_ref: str,
    purpose: str,
    action: Sequence[str],
    given_at: str,
    withdrawable: bool = True,
    expiry: str | None = None,
    turn_ref: str | None = None,
    **extensions: Any,
) -> ConsentReceipt:
    """Build and validate a receipt, computing its ``receipt_digest``.

    Strict (raises): this is the constructor a capture site calls before the
    fail-open :func:`record_consent`. ``withdrawable`` defaults to True
    because consent under GDPR Art. 7(3) is withdrawable; pass False only to
    record what the source system actually said.
    """
    fields: dict[str, Any] = {
        "consent_id": consent_id,
        "subject_ref": subject_ref,
        "purpose": purpose,
        # A bare string is passed through so the validator refuses it, rather
        # than being split into one "concept" per character.
        "action": action if isinstance(action, str) else list(action),
        "given_at": given_at,
        "withdrawable": withdrawable,
        "expiry": expiry,
        "turn_ref": turn_ref,
        **extensions,
    }
    # Validate once without the digest to normalise the body, then bind it.
    probe = ConsentReceipt.model_validate({**fields, "receipt_digest": "sha256:" + "0" * 64})
    body = probe.model_dump(exclude_none=True)
    return ConsentReceipt.model_validate({**body, "receipt_digest": compute_receipt_digest(body)})


def receipt_digest_matches(receipt: ConsentReceipt) -> bool:
    """True when the stored ``receipt_digest`` matches the receipt body."""
    return compute_receipt_digest(receipt.model_dump(exclude_none=True)) == receipt.receipt_digest


def withdraw_consent(receipt: ConsentReceipt, *, withdrawn_at: str) -> ConsentReceipt:
    """Return a copy of ``receipt`` with ``withdrawn_at`` set.

    Raises:
        ConsentReceiptError: the receipt is not withdrawable, is already
            withdrawn (a withdrawal is recorded once and never moved), or
            ``withdrawn_at`` precedes ``given_at``.
    """
    if receipt.withdrawn_at is not None:
        raise ConsentReceiptError("consent receipt is already withdrawn")
    data = receipt.model_dump(exclude_none=True)
    data["withdrawn_at"] = withdrawn_at
    return ConsentReceipt.model_validate(data)


#: Clock skew tolerated when refusing a withdrawal time in the future.
WITHDRAWAL_CLOCK_SKEW = timedelta(minutes=5)


class ConsentWithdrawalError(ConsentReceiptError):
    """A recorded consent receipt cannot be withdrawn as requested.

    Raised for an unknown or ambiguous ``consent_id``, a malformed or
    tampered stored receipt, a future ``withdrawn_at``, and every refusal
    :func:`withdraw_consent` makes (not withdrawable, already withdrawn,
    earlier than ``given_at``).
    """


@dataclass(frozen=True)
class ConsentWithdrawal:
    """A capsule copy with one receipt's ``withdrawn_at`` set (nothing written)."""

    capsule: dict[str, Any]
    receipt: ConsentReceipt
    index: int


def withdraw_recorded_consent(
    capsule: Mapping[str, Any],
    consent_id: str,
    *,
    withdrawn_at: str,
    now: datetime | None = None,
) -> ConsentWithdrawal:
    """Set ``withdrawn_at`` on the stored receipt ``consent_id``; strict, pure.

    The receipt digest excludes ``withdrawn_at``, so the withdrawn receipt still
    verifies. Only the one entry changes; every other receipt, and every other
    capsule field, is carried over untouched. The caller writes the result.

    Raises:
        ConsentWithdrawalError: no receipt or several receipts carry
            ``consent_id``; the stored receipt is malformed or its digest does
            not match its body (withdrawing would launder a tampered record);
            ``withdrawn_at`` is not ISO-8601, is in the future, or
            :func:`withdraw_consent` refuses it.
    """
    block = conversation_block(capsule) or {}
    entries = block.get(RECORD_KEY) if isinstance(block, Mapping) else None
    if not isinstance(entries, list):
        raise ConsentWithdrawalError("capsule records no consent receipts")
    matches = [
        i
        for i, item in enumerate(entries)
        if isinstance(item, Mapping) and item.get("consent_id") == consent_id
    ]
    if not matches:
        raise ConsentWithdrawalError(f"no consent receipt with consent_id {consent_id!r}")
    if len(matches) > 1:
        raise ConsentWithdrawalError(
            f"consent_id {consent_id!r} is recorded {len(matches)} times; "
            "refusing an ambiguous withdrawal"
        )
    index = matches[0]
    try:
        stored = ConsentReceipt.model_validate(entries[index])
    except (ConversationError, ValueError) as exc:  # record error or ValidationError
        raise ConsentWithdrawalError(
            f"stored consent receipt {consent_id!r} is malformed ({type(exc).__name__})"
        ) from exc
    if not receipt_digest_matches(stored):
        raise ConsentWithdrawalError(
            f"stored consent receipt {consent_id!r} fails its digest check; "
            "refusing to withdraw a tampered receipt"
        )
    try:
        when = _parse_at(
            check_timestamp(withdrawn_at, field_name="withdrawn_at"), field="withdrawn_at"
        )
    except ConversationError as exc:
        raise ConsentWithdrawalError(str(exc)) from exc
    current = now if now is not None else datetime.now(timezone.utc)
    if when > current + WITHDRAWAL_CLOCK_SKEW:
        raise ConsentWithdrawalError(
            "withdrawn_at is in the future; a withdrawal is recorded after it happens"
        )
    try:
        withdrawn = withdraw_consent(stored, withdrawn_at=withdrawn_at)
    except ConsentReceiptError as exc:
        raise ConsentWithdrawalError(str(exc)) from exc
    except ValueError as exc:  # pydantic wraps validator errors
        raise ConsentWithdrawalError(_validation_reason(exc)) from exc
    new_entries = list(entries)
    new_entries[index] = withdrawn.model_dump(exclude_none=True)
    new_block = dict(block)
    new_block[RECORD_KEY] = new_entries
    facets = dict(capsule.get("facets") or {})
    facets[FACET_NAME] = new_block
    out = dict(capsule)
    out["facets"] = facets
    return ConsentWithdrawal(out, withdrawn, index)


def _validation_reason(exc: ValueError) -> str:
    """The first validator message from a pydantic error, never the input value."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for err in errors():
            ctx = err.get("ctx") or {}
            inner = ctx.get("error")
            if isinstance(inner, AccountabilityRecordError):
                return str(inner)
    return type(exc).__name__


# ── Storage ───────────────────────────────────────────────────────────────


def _append_consent(capsule: dict[str, Any], receipt: ConsentReceipt) -> RecordOutcome:
    if not receipt_digest_matches(receipt):
        return RecordOutcome(capsule, False, "receipt_digest does not match receipt body")
    facet = facet_from_capsule(capsule)
    if receipt.turn_ref is not None and (
        facet is None or resolve_turn(facet, receipt.turn_ref) is None
    ):
        return RecordOutcome(capsule, False, "turn_ref does not resolve")
    block = dict(conversation_block(capsule) or {"schema_version": SCHEMA_VERSION})
    existing = block.get(RECORD_KEY, [])
    if not isinstance(existing, list):
        return RecordOutcome(capsule, False, f"existing {RECORD_KEY} is not a list")
    if len(existing) >= MAX_RECORDS_PER_KIND:
        return RecordOutcome(capsule, False, f"{RECORD_KEY} record cap reached")
    if any(
        isinstance(item, Mapping) and item.get("consent_id") == receipt.consent_id
        for item in existing
    ):
        return RecordOutcome(capsule, False, "consent_id already recorded")
    block[RECORD_KEY] = [*existing, receipt.model_dump(exclude_none=True)]
    facets = dict(capsule.get("facets") or {})
    facets[FACET_NAME] = block
    out = dict(capsule)
    out["facets"] = facets
    return RecordOutcome(out, True, None)


def record_consent(
    capsule: dict[str, Any], receipt: ConsentReceipt | Mapping[str, Any]
) -> RecordOutcome:
    """Attach a consent receipt to ``facets.conversation.consent``; never raises.

    Not recorded (capsule returned untouched) when the receipt is malformed,
    its digest does not match its body, its ``turn_ref`` does not resolve, its
    ``consent_id`` is already recorded, or the per-kind cap is reached. Unlike
    the turn-anchored records, no conversation turns are required: the
    conversation block is created when absent.
    """
    try:
        validated = (
            receipt
            if isinstance(receipt, ConsentReceipt)
            else ConsentReceipt.model_validate(receipt)
        )
        outcome = _append_consent(capsule, validated)
    except Exception as exc:  # noqa: BLE001 — fail-open is the contract (D7)
        logger.warning(
            "hitl: consent receipt not attached (%s); workload unaffected",
            type(exc).__name__,
        )
        return RecordOutcome(capsule, False, type(exc).__name__)
    if not outcome.recorded:
        logger.warning("hitl: consent receipt not attached (%s)", outcome.reason)
    return outcome


def load_consents(capsule: Mapping[str, Any]) -> LoadedRecords[ConsentReceipt]:
    """Read every stored consent receipt (malformed entries become defects)."""
    return load_records(capsule, RECORD_KEY, ConsentReceipt)


# ── Verification ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConsentVerdict:
    """Offline re-check of one stored consent receipt."""

    index: int
    consent_id: str
    digest_matches: bool
    turn_resolves: bool | None
    duplicate_id: bool
    withdrawn: bool

    @property
    def ok(self) -> bool:
        """True when the receipt is intact (withdrawal is a fact, not a defect)."""
        return self.digest_matches and self.turn_resolves is not False and not self.duplicate_id

    def to_dict(self) -> dict[str, Any]:
        """Deterministic JSON-ready form."""
        return {
            "index": self.index,
            "consent_id": self.consent_id,
            "digest_matches": self.digest_matches,
            "turn_resolves": self.turn_resolves,
            "duplicate_id": self.duplicate_id,
            "withdrawn": self.withdrawn,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class ConsentVerification:
    """Result of :func:`verify_consents` (fail-closed)."""

    verdicts: list[ConsentVerdict] = field(default_factory=list)
    defects: list[RecordDefect] = field(default_factory=list)

    @property
    def status(self) -> Literal["ok", "defective", "empty"]:
        """``empty`` nothing stored; ``defective`` any defect; else ``ok``."""
        if not self.verdicts and not self.defects:
            return "empty"
        if self.defects or not all(v.ok for v in self.verdicts):
            return "defective"
        return "ok"


def verify_consents(capsule: Mapping[str, Any]) -> ConsentVerification:
    """Re-check every stored consent receipt offline (fail-closed).

    Recomputes each ``receipt_digest``, resolves any ``turn_ref``, and flags a
    ``consent_id`` stored more than once. A malformed conversation facet makes
    every ``turn_ref`` unresolvable rather than raising. It never judges
    whether the consent is legally valid, current, or sufficient.
    """
    loaded = load_consents(capsule)
    try:
        facet = facet_from_capsule(dict(capsule))
    except Exception:  # noqa: BLE001 — a broken thread resolves nothing
        facet = None
    counts: dict[str, int] = {}
    for _, rec in loaded.records:
        counts[rec.consent_id] = counts.get(rec.consent_id, 0) + 1
    verdicts = []
    for index, rec in loaded.records:
        turn_ok: bool | None = None
        if rec.turn_ref is not None:
            turn_ok = facet is not None and resolve_turn(facet, rec.turn_ref) is not None
        verdicts.append(
            ConsentVerdict(
                index=index,
                consent_id=rec.consent_id,
                digest_matches=receipt_digest_matches(rec),
                turn_resolves=turn_ok,
                duplicate_id=counts[rec.consent_id] > 1,
                withdrawn=rec.withdrawn_at is not None,
            )
        )
    return ConsentVerification(verdicts=verdicts, defects=list(loaded.defects))
