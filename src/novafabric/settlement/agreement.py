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

"""Negotiated-agreement evidence — ADR-0163 P3 (NF-316, experimental).

``facets.settlement.agreement`` records *that* terms were agreed and *what*
their digest is: ``agreement_ref`` (the contract artifact), ``terms_digest``
(the canonical terms), ``parties`` and ``negotiation_transcript_ref``. It never
interprets, enforces, or executes the contract (ADR-0163 D3, I-4) — nothing in
this module reads a term's meaning, and no function returns a judgement about
whether a party complied.

**Canonical terms.** :func:`digest_terms` hashes a terms mapping as canonical
JSON (sorted keys, no whitespace). Floats are refused, not rounded: ``162.4``
has no exact binary form, two producers serialising it can disagree in the
last digit, and a terms digest that depends on float formatting stops binding
anything. Amounts go in as integer minor units or strings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from novafabric.settlement._refs import (
    validate_digest,
    validate_identity_ref,
    validate_instant,
    with_block,
)
from novafabric.settlement.facet import (
    SettlementFacet,
    digest_artifact,
    reject_payment_secrets,
    verify_ref_binding,
)

#: Key of the agreement inside ``facets.settlement``.
AGREEMENT_FIELD = "agreement"

#: Upper bound on parties. A multi-agent negotiation with more signatories
#: than this is not a shape this record is designed to carry.
MAX_PARTIES = 64

#: Bounds on a terms mapping handed to :func:`digest_terms`, so hashing a
#: hostile structure is bounded work and never hits the recursion limit.
MAX_TERMS_DEPTH = 32
MAX_TERMS_NODES = 10_000


class NonCanonicalTermsError(Exception):
    """Raised when a terms mapping has no single canonical JSON form.

    A float, a non-string key, a non-JSON value, or a structure deeper or
    larger than the bounds. Not a ``ValueError``: a terms digest that silently
    depends on float formatting is a binding defect, not a shape nit.
    """


class MalformedAgreementError(Exception):
    """Raised when a stored ``agreement`` block cannot be read."""


class AgreementRecord(BaseModel):
    """The negotiated-agreement record (NF-316). References only."""

    model_config = ConfigDict(extra="allow")

    #: Digest of the negotiated contract / terms artifact.
    agreement_ref: str
    #: Digest of the canonical terms (see :func:`digest_terms`).
    terms_digest: str
    #: Identity references of the agreeing parties, in recorded order.
    parties: list[str] = Field(min_length=2, max_length=MAX_PARTIES)
    #: Digest of the negotiation record. Optional: a fixed-price catalogue
    #: purchase has no negotiation. Absent means "no transcript was bound",
    #: never "there was no negotiation".
    negotiation_transcript_ref: str | None = None
    #: RFC-3339 instant, with offset, at which the terms were agreed.
    agreed_at: str

    @field_validator("agreement_ref", "terms_digest", mode="before")
    @classmethod
    def _check_digest(cls, value: object, info: Any) -> str:
        return validate_digest(value, field=info.field_name)

    @field_validator("negotiation_transcript_ref", mode="before")
    @classmethod
    def _check_transcript(cls, value: object) -> str | None:
        if value is None:
            return None
        return validate_digest(value, field="negotiation_transcript_ref")

    @field_validator("parties", mode="before")
    @classmethod
    def _check_parties(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("parties must be a list of identity references")
        if len(value) > MAX_PARTIES:
            raise ValueError(f"parties holds more than {MAX_PARTIES} entries")
        parties = [
            validate_identity_ref(item, field=f"parties[{index}]")
            for index, item in enumerate(value)
        ]
        if len(set(parties)) != len(parties):
            raise ValueError("parties lists the same identity twice")
        return parties

    @field_validator("agreed_at", mode="before")
    @classmethod
    def _check_agreed_at(cls, value: object) -> str:
        return validate_instant(value, field="agreed_at")

    @model_validator(mode="after")
    def _reject_secrets(self) -> AgreementRecord:
        """ADR-0163 I-2 on the record itself, ``extra`` fields included."""
        reject_payment_secrets(self.model_dump(), path=AGREEMENT_FIELD)
        return self


class AgreementVerification(BaseModel):
    """Offline re-derivation of an agreement's digests.

    Each check is ``True`` (re-derived and equal), ``False`` (re-derived and
    different) or ``None`` (nothing was supplied to re-derive from).
    ``interpreted`` is a constant ``False``: NovaFabric never reads what the
    terms mean.
    """

    model_config = ConfigDict(frozen=True)

    agreement_ref_ok: bool | None
    terms_digest_ok: bool | None
    #: ``None`` also when the record binds no transcript.
    transcript_ok: bool | None
    transcript_bound: bool
    interpreted: Literal[False] = False

    @property
    def ok(self) -> bool:
        """True only when every recorded digest was re-derived and matched.

        Fail-closed: an unchecked reference is not a passing one, so a
        recorded transcript that was not re-derived keeps ``ok`` false.
        """
        checks = [self.agreement_ref_ok, self.terms_digest_ok]
        if self.transcript_bound:
            checks.append(self.transcript_ok)
        return all(check is True for check in checks)


def _canonical(value: Any, depth: int, budget: list[int]) -> Any:
    """Return ``value`` checked for a single canonical JSON form."""
    budget[0] -= 1
    if budget[0] < 0:
        raise NonCanonicalTermsError(f"terms hold more than {MAX_TERMS_NODES} values")
    if depth > MAX_TERMS_DEPTH:
        raise NonCanonicalTermsError(f"terms nest deeper than {MAX_TERMS_DEPTH} levels")
    if isinstance(value, float):
        raise NonCanonicalTermsError(
            "terms contain a float; use integer minor units or a string so the "
            "digest has one canonical form (ADR-0163 D3)"
        )
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise NonCanonicalTermsError("terms keys must be strings")
            out[key] = _canonical(child, depth + 1, budget)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonical(child, depth + 1, budget) for child in value]
    raise NonCanonicalTermsError(f"terms contain a non-JSON value of type {type(value).__name__}")


def digest_terms(terms: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` digest of ``terms`` as canonical JSON.

    Raises:
        NonCanonicalTermsError: on a float, a non-string key, a non-JSON
            value, or a structure beyond :data:`MAX_TERMS_DEPTH` /
            :data:`MAX_TERMS_NODES`.
        PaymentSecretRejectedError: if the terms carry a payment secret —
            hashing one would not leak it, but a caller holding a PAN inside
            contract terms has an integration bug worth hearing about.
    """
    canonical = _canonical(terms, 0, [MAX_TERMS_NODES])
    reject_payment_secrets(canonical, path="terms")
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return digest_artifact(encoded)


def build_agreement(
    *,
    agreement_ref: str,
    terms_digest: str,
    parties: list[str],
    agreed_at: str,
    negotiation_transcript_ref: str | None = None,
) -> AgreementRecord:
    """Record an agreement by reference.

    Raises:
        InvalidReferenceError: if a ``*_ref`` / ``terms_digest`` is not a digest.
        InvalidIdentityRefError: if a party is not a bounded identifier.
        PaymentSecretRejectedError: if any value carries a payment secret.
        pydantic.ValidationError: on fewer than two / duplicate parties or a
            malformed ``agreed_at``.
    """
    return AgreementRecord(
        agreement_ref=agreement_ref,
        terms_digest=terms_digest,
        parties=parties,
        agreed_at=agreed_at,
        negotiation_transcript_ref=negotiation_transcript_ref,
    )


def verify_agreement(
    record: AgreementRecord,
    *,
    artifact: str | bytes | None = None,
    terms: Mapping[str, Any] | None = None,
    transcript: str | bytes | None = None,
) -> AgreementVerification:
    """Re-derive an agreement's digests from whatever was supplied.

    Pure and offline. Never raises on a mismatch: terms that cannot be
    canonicalised re-derive to ``terms_digest_ok = False`` rather than an
    exception, because an unhashable terms object is itself the finding.
    """
    terms_ok: bool | None = None
    if terms is not None:
        try:
            terms_ok = digest_terms(terms) == record.terms_digest
        except NonCanonicalTermsError:
            terms_ok = False
    bound = record.negotiation_transcript_ref is not None
    return AgreementVerification(
        agreement_ref_ok=(
            None if artifact is None else verify_ref_binding(record.agreement_ref, artifact)
        ),
        terms_digest_ok=terms_ok,
        transcript_ok=(
            None
            if transcript is None or not bound
            else verify_ref_binding(record.negotiation_transcript_ref, transcript)
        ),
        transcript_bound=bound,
    )


def agreement_from_facet(facet: SettlementFacet) -> AgreementRecord | None:
    """Return the facet's agreement, or ``None`` when none is recorded.

    Raises:
        MalformedAgreementError: if the stored block fails validation.
        InvalidReferenceError / InvalidIdentityRefError /
        PaymentSecretRejectedError: as raised by the record's validators.
    """
    raw = (facet.model_extra or {}).get(AGREEMENT_FIELD)
    if raw is None:
        return None
    try:
        return AgreementRecord.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or AGREEMENT_FIELD
        raise MalformedAgreementError(f"{AGREEMENT_FIELD}.{where}: {first['msg']}") from exc


def attach_agreement(facet: SettlementFacet, record: AgreementRecord) -> SettlementFacet:
    """Return a new facet carrying ``record`` as ``agreement``."""
    return with_block(facet, AGREEMENT_FIELD, record.model_dump(exclude_none=True))
