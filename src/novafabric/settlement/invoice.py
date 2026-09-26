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

"""Invoice / receipt provenance binding — ADR-0163 P3 (NF-319, experimental).

``facets.settlement.invoice_receipt`` binds an invoice, receipt or credit
note by ``document_ref`` digest and records a ``matches_settlement`` verdict
against the NF-313 settlement record in the same facet.

Two rules carry the whole design:

- **Unresolvable is recorded, never fatal.** A ``document_ref`` the resolver
  cannot produce — or produces bytes of a different digest — degrades to
  ``unbound: true`` (spec §3 req. 13). ``unbound`` defaults to ``True``: a
  document nobody re-derived is not a bound one.
- **A match is earned, three-valued.** ``matches_settlement`` is ``True``
  only when the settled amount was compared, agreed, *and* the NF-313
  finality record says ``settled``; ``False`` when a comparison disagreed
  (the reason is in ``mismatches``); ``None`` when there was nothing to
  compare. A model validator refuses any record claiming a match it has not
  earned — including one whose document is unbound — so ``model_validate`` of
  untrusted JSON cannot mint one (the NF-312 ``DiscrepancyLaunderedError``
  pattern).

A credit note is never compared against a settlement: it answers to a
reversal, which is NF-320 reversal lineage (P4), so its verdict is ``None``.
Nothing here decides who owes whom (ADR-0163 I-4).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from novafabric.settlement._refs import validate_digest, validate_identity_ref, with_block
from novafabric.settlement.facet import (
    Money,
    SettlementFacet,
    is_final,
    reject_payment_secrets,
    verify_ref_binding,
)

#: Key of the invoice/receipt block inside ``facets.settlement``.
INVOICE_FIELD = "invoice_receipt"

DocumentType = Literal["invoice", "receipt", "credit_note"]

#: Why a document does not match the settlement record.
InvoiceMismatch = Literal["amount_mismatch", "currency_mismatch", "not_settled"]


class InvoiceMatchLaunderedError(Exception):
    """Raised when an invoice record's verdict disagrees with its own evidence.

    ``matches_settlement: true`` beside a mismatch or an unbound document,
    a mismatch beside a non-``false`` verdict, or ``false`` with no reason.
    Not a ``ValueError``, for the reason given on
    :class:`~novafabric.settlement.facet.DiscrepancyLaunderedError`.
    """


class MalformedInvoiceError(Exception):
    """Raised when a stored ``invoice_receipt`` block cannot be read."""


class InvoiceReceipt(BaseModel):
    """The invoice / receipt provenance record (NF-319). References only."""

    model_config = ConfigDict(extra="allow")

    #: Digest of the invoice / receipt / credit-note document.
    document_ref: str
    document_type: DocumentType
    #: Identity reference of the issuing party.
    issuer_ref: str
    #: The document's total, in integer minor units + ISO-4217 code.
    total_amount: Money
    matches_settlement: bool | None = None
    mismatches: list[InvoiceMismatch] = Field(default_factory=list, max_length=3)
    #: True unless the document was resolved and its digest re-derived.
    unbound: bool = True

    @field_validator("document_ref", mode="before")
    @classmethod
    def _check_document_ref(cls, value: object) -> str:
        return validate_digest(value, field="document_ref")

    @field_validator("issuer_ref", mode="before")
    @classmethod
    def _check_issuer(cls, value: object) -> str:
        return validate_identity_ref(value, field="issuer_ref")

    @model_validator(mode="after")
    def _verdict_must_be_earned(self) -> InvoiceReceipt:
        """Refuse a verdict that contradicts the record's own evidence."""
        if self.matches_settlement is True and (self.unbound or self.mismatches):
            raise InvoiceMatchLaunderedError(
                "invoice_receipt claims matches_settlement=true while "
                + ("its document is unbound" if self.unbound else f"recording {self.mismatches}")
                + "; a match is never recorded without the evidence for it (ADR-0163 I-4)"
            )
        if self.mismatches and self.matches_settlement is not False:
            raise InvoiceMatchLaunderedError(
                f"invoice_receipt records mismatches {self.mismatches} but "
                f"matches_settlement={self.matches_settlement}; a mismatch is a 'false' verdict"
            )
        if self.matches_settlement is False and not self.mismatches:
            raise InvoiceMatchLaunderedError(
                "invoice_receipt says matches_settlement=false without naming a mismatch"
            )
        reject_payment_secrets(self.model_dump(), path=INVOICE_FIELD)
        return self


class InvoiceVerification(BaseModel):
    """Offline re-check of an invoice record against its settlement facet.

    ``document_rechecked`` is ``None`` when no document bytes were supplied:
    the ``unbound`` flag then rests on the producer's resolution, which is
    stated here rather than implied.
    """

    model_config = ConfigDict(frozen=True)

    recorded_matches: bool | None
    recomputed_matches: bool | None
    recomputed_mismatches: list[InvoiceMismatch]
    unbound: bool
    document_rechecked: bool | None
    #: Recorded verdict and mismatches equal the recomputed ones.
    consistent: bool

    @property
    def ok(self) -> bool:
        """True only for a bound, consistent, recomputed match (fail-closed)."""
        return (
            self.consistent
            and self.recomputed_matches is True
            and not self.unbound
            and self.document_rechecked is not False
        )


def compare_to_settlement(
    document_type: DocumentType, total: Money, facet: SettlementFacet | None
) -> tuple[bool | None, list[InvoiceMismatch]]:
    """Compare a document total against the facet's NF-313 settlement record.

    Returns ``(verdict, mismatches)``. A currency mismatch suppresses the
    amount comparison — no FX rate is chosen (the NF-312 rule). An absent
    amount or finality record yields no finding: unread is not satisfied.
    """
    if document_type == "credit_note" or facet is None:
        return None, []
    mismatches: list[InvoiceMismatch] = []
    if facet.amount is not None:
        if facet.amount.currency != total.currency:
            mismatches.append("currency_mismatch")
        elif facet.amount.amount_minor != total.amount_minor:
            mismatches.append("amount_mismatch")
    if facet.finality is not None and not is_final(facet):
        mismatches.append("not_settled")
    if mismatches:
        return False, mismatches
    if facet.amount is not None and is_final(facet):
        return True, []
    return None, []


def _resolves(document_ref: str, resolver: Callable[[str], str | bytes | None] | None) -> bool:
    """True when ``resolver`` produces bytes whose digest is ``document_ref``."""
    if resolver is None:
        return False
    try:
        document = resolver(document_ref)
    except Exception:  # noqa: BLE001
        # A resolver that raised told us about itself, not the document.
        # Fail-open (I-3): record the document as unbound, never fail the run.
        return False
    return document is not None and verify_ref_binding(document_ref, document)


def bind_invoice(
    *,
    document_ref: str,
    document_type: DocumentType,
    issuer_ref: str,
    total_amount: Money,
    settlement: SettlementFacet | None,
    resolver: Callable[[str], str | bytes | None] | None = None,
) -> InvoiceReceipt:
    """Record an invoice/receipt and its verdict against the settlement.

    Never raises on an unresolvable document — that is ``unbound: true``.
    An unbound document can still *disagree* with the settlement (the
    recorded total is a claim worth surfacing), but it can never *match*:
    a would-be match on an unbound document is recorded as ``None``.

    Raises:
        InvalidReferenceError: if ``document_ref`` is not a digest.
        InvalidIdentityRefError: if ``issuer_ref`` is not a bounded identifier.
        PaymentSecretRejectedError: if any value carries a payment secret.
    """
    unbound = not _resolves(document_ref, resolver)
    verdict, mismatches = compare_to_settlement(document_type, total_amount, settlement)
    if unbound and verdict is True:
        verdict = None
    return InvoiceReceipt(
        document_ref=document_ref,
        document_type=document_type,
        issuer_ref=issuer_ref,
        total_amount=total_amount,
        matches_settlement=verdict,
        mismatches=mismatches,
        unbound=unbound,
    )


def verify_invoice(
    record: InvoiceReceipt,
    facet: SettlementFacet | None,
    *,
    document: str | bytes | None = None,
) -> InvoiceVerification:
    """Recompute an invoice's verdict from the facet and compare. Pure, offline.

    With ``document`` supplied, its digest is re-derived too, and a mismatch
    makes the document unbound for this verification.
    """
    rechecked = None if document is None else verify_ref_binding(record.document_ref, document)
    unbound = record.unbound or rechecked is False
    verdict, mismatches = compare_to_settlement(record.document_type, record.total_amount, facet)
    if unbound and verdict is True:
        verdict = None
    consistent = record.matches_settlement == verdict and sorted(record.mismatches) == sorted(
        mismatches
    )
    return InvoiceVerification(
        recorded_matches=record.matches_settlement,
        recomputed_matches=verdict,
        recomputed_mismatches=mismatches,
        unbound=unbound,
        document_rechecked=rechecked,
        consistent=consistent,
    )


def invoice_from_facet(facet: SettlementFacet) -> InvoiceReceipt | None:
    """Return the facet's invoice/receipt record, or ``None`` when absent.

    Raises:
        MalformedInvoiceError: if the stored block fails validation.
        InvoiceMatchLaunderedError: if its verdict contradicts its evidence.
    """
    raw: Any = (facet.model_extra or {}).get(INVOICE_FIELD)
    if raw is None:
        return None
    try:
        return InvoiceReceipt.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or INVOICE_FIELD
        raise MalformedInvoiceError(f"{INVOICE_FIELD}.{where}: {first['msg']}") from exc


def attach_invoice(facet: SettlementFacet, record: InvoiceReceipt) -> SettlementFacet:
    """Return a new facet carrying ``record`` as ``invoice_receipt``."""
    return with_block(facet, INVOICE_FIELD, record.model_dump(exclude_none=False))
