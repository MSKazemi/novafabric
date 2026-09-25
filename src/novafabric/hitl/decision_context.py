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

"""HITL decision-context receipt — ADR-0150 D2/P2 (NF-182).

Records **exactly what the human was shown** at the turn where they decided:
an ordered list of rendered items (``item_kind``, ``item_digest``,
``rendered_at``), the decision code, the reason (code or digest), the
pseudonymous decider, and a ``context_root`` — an RFC 6962-style SHA-256
Merkle root over the shown items, computed with the same leaf/inner
construction as :mod:`novafabric.evidence.merkle`.

Re-performance: given the rendered items (held by whoever ran the review
surface), an auditor recomputes each ``item_digest`` and the root offline. The
receipt lives in ``capsule.yaml``, which the capsule's own Merkle root covers,
so the ``context_root`` is bound into the sealed capsule transitively (ADR-0087).

Storage deviation (recorded in ADR-0150): receipts live as a list at
``facets.conversation.decision_context`` rather than a top-level
``facets.decision_context``, because the run-capsule facet registry is closed.

Record-only (I-4): a receipt says what was shown and what was decided. It never
asserts that the decision was correct or the oversight adequate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.evidence.merkle import _leaf, _merkle_root
from novafabric.hitl._records import (
    AccountabilityRecordError,
    RecordDefect,
    RecordOutcome,
    check_code,
    check_code_or_digest,
    check_digest,
    check_extras,
    check_human_ref,
    check_timestamp,
    check_turn_ref,
    duplicate_turn_ids,
    load_records,
    record_fail_open,
)
from novafabric.hitl.conversation import digest_turn, facet_from_capsule, resolve_turn

RECORD_KEY = "decision_context"

#: Upper bound on shown items per receipt. A review surface that rendered more
#: than this is not something a human read; the cap bounds verification work.
MAX_SHOWN_ITEMS = 1024

_ITEM_KIND_MAX = 64

VerifyStatus = Literal["ok", "defective", "empty"]


class ShownItem(BaseModel):
    """One item rendered to the human at the deciding turn.

    ``extra="forbid"``, unlike the other records: every byte of a shown item is
    a Merkle leaf input, and an unbound extension field would look covered by
    the root while not being covered.
    """

    model_config = ConfigDict(extra="forbid")

    item_kind: str
    item_digest: str
    rendered_at: str

    @field_validator("item_kind", mode="before")
    @classmethod
    def _check_kind(cls, v: object) -> str:
        kind = check_code(v, field_name="item_kind")
        if len(kind) > _ITEM_KIND_MAX:
            raise AccountabilityRecordError(f"item_kind must be <= {_ITEM_KIND_MAX} chars")
        return kind

    @field_validator("item_digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object) -> str:
        return check_digest(v, field_name="item_digest")

    @field_validator("rendered_at", mode="before")
    @classmethod
    def _check_rendered_at(cls, v: object) -> str:
        return check_timestamp(v, field_name="rendered_at")

    def leaf_bytes(self) -> bytes:
        """Canonical JSON of the item — the Merkle leaf pre-image."""
        return json.dumps(
            {
                "item_digest": self.item_digest,
                "item_kind": self.item_kind,
                "rendered_at": self.rendered_at,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")


_RECEIPT_FIELDS = frozenset(
    {
        "turn_ref",
        "shown_context",
        "decision",
        "reason",
        "decided_by",
        "context_root",
        "decided_at",
        "nf086_approval_ref",
    }
)


class DecisionContextReceipt(BaseModel):
    """NF-182 receipt: what the human saw when deciding at ``turn_ref``.

    The model does **not** enforce ``context_root`` == recomputed root: a
    tampered receipt must still load so :func:`verify_decision_contexts` can
    report it. :func:`build_decision_context` always computes the root.
    """

    model_config = ConfigDict(extra="allow")

    turn_ref: str
    shown_context: list[ShownItem] = Field(min_length=1, max_length=MAX_SHOWN_ITEMS)
    decision: str
    reason: str
    decided_by: str
    context_root: str
    decided_at: str | None = None
    #: Reference to the NF-086 approval for the same turn, when one exists —
    #: referenced, never duplicated (spec §3 req. 6). NF-086 is not built yet,
    #: so this is an opaque digest this slice does not resolve.
    nf086_approval_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _check_extras(cls, data: Any) -> Any:
        return check_extras(data, known=_RECEIPT_FIELDS)

    @field_validator("turn_ref", mode="before")
    @classmethod
    def _check_turn_ref(cls, v: object) -> str:
        return check_turn_ref(v)

    @field_validator("decision", mode="before")
    @classmethod
    def _check_decision(cls, v: object) -> str:
        return check_code(v, field_name="decision")

    @field_validator("reason", mode="before")
    @classmethod
    def _check_reason(cls, v: object) -> str:
        return check_code_or_digest(v, field_name="reason")

    @field_validator("decided_by", mode="before")
    @classmethod
    def _check_decided_by(cls, v: object) -> str:
        return check_human_ref(v, field_name="decided_by")

    @field_validator("context_root", mode="before")
    @classmethod
    def _check_root(cls, v: object) -> str:
        return check_digest(v, field_name="context_root")

    @field_validator("decided_at", mode="before")
    @classmethod
    def _check_decided_at(cls, v: object) -> str | None:
        return None if v is None else check_timestamp(v, field_name="decided_at")

    @field_validator("nf086_approval_ref", mode="before")
    @classmethod
    def _check_nf086(cls, v: object) -> str | None:
        return None if v is None else check_digest(v, field_name="nf086_approval_ref")

    def recompute_root(self) -> str:
        """Recompute the Merkle root over ``shown_context`` in stored order."""
        return compute_context_root(self.shown_context)


# ── Construction ──────────────────────────────────────────────────────────


def shown_item(
    item_kind: str,
    *,
    rendered_at: str,
    content: str | bytes | None = None,
    item_digest: str | None = None,
) -> ShownItem:
    """Build one shown item from the rendered bytes or their digest.

    ``content`` is hashed and discarded here — it never reaches the record.

    Raises:
        AccountabilityRecordError: if neither or both of ``content`` /
            ``item_digest`` are given.
    """
    if (content is None) == (item_digest is None):
        raise AccountabilityRecordError(
            "pass exactly one of content= (hashed here, not stored) or item_digest="
        )
    digest = item_digest if content is None else digest_turn(content)
    return ShownItem(item_kind=item_kind, item_digest=digest, rendered_at=rendered_at)  # type: ignore[arg-type]


def compute_context_root(items: Sequence[ShownItem]) -> str:
    """Return the ``sha256:`` Merkle root over ``items`` in the given order.

    Order is part of the evidence — what was shown first is what the human
    read first — so the leaves are not sorted.

    Raises:
        AccountabilityRecordError: for an empty or over-cap item list.
    """
    if not items:
        raise AccountabilityRecordError("shown_context must contain at least one item")
    if len(items) > MAX_SHOWN_ITEMS:
        raise AccountabilityRecordError(f"shown_context exceeds {MAX_SHOWN_ITEMS} items")
    leaves = [_leaf(item.leaf_bytes()) for item in items]
    return "sha256:" + _merkle_root(leaves).hex()


def build_decision_context(
    turn_ref: str,
    shown_context: Iterable[ShownItem],
    *,
    decision: str,
    reason: str,
    decided_by: str,
    decided_at: str | None = None,
    nf086_approval_ref: str | None = None,
) -> DecisionContextReceipt:
    """Build a receipt, computing ``context_root`` from the shown items.

    Raises the named validation errors for malformed input — this is the
    strict builder. Use :func:`record_decision_context` at a capture site that
    must never raise into the workload.
    """
    items = list(shown_context)
    return DecisionContextReceipt(
        turn_ref=turn_ref,
        shown_context=items,
        decision=decision,
        reason=reason,
        decided_by=decided_by,
        context_root=compute_context_root(items),
        decided_at=decided_at,
        nf086_approval_ref=nf086_approval_ref,
    )


def record_decision_context(
    capsule: dict[str, Any],
    receipt: DecisionContextReceipt | Mapping[str, Any],
) -> RecordOutcome:
    """Attach a receipt to ``facets.conversation.decision_context``; never raises.

    Not recorded (capsule returned untouched) when the receipt is malformed,
    there is no conversation facet, its ``turn_ref`` does not resolve, a
    receipt already exists for that turn (one decision surface per turn keeps
    ``nova hitl context show --turn`` unambiguous), or its stored root does not
    match the shown items (a receipt that would fail its own verification is
    not evidence).
    """
    try:
        validated = (
            receipt
            if isinstance(receipt, DecisionContextReceipt)
            else DecisionContextReceipt.model_validate(receipt)
        )
        if validated.context_root != validated.recompute_root():
            return RecordOutcome(capsule, False, "context_root does not match shown_context")
    except Exception as exc:  # noqa: BLE001 — fail-open is the contract (D7)
        return RecordOutcome(capsule, False, type(exc).__name__)
    return record_fail_open(
        capsule, RECORD_KEY, DecisionContextReceipt, validated, unique_per_turn=True
    )


# ── Verification (fail-closed) ────────────────────────────────────────────


@dataclass(frozen=True)
class ReceiptVerdict:
    """Offline re-performance result for one stored receipt."""

    index: int
    turn_ref: str
    turn_resolves: bool
    stored_root: str
    recomputed_root: str
    duplicate_for_turn: bool = False

    @property
    def root_matches(self) -> bool:
        """True when the stored root equals the recomputed one."""
        return self.stored_root == self.recomputed_root

    @property
    def ok(self) -> bool:
        """True only when every check passed."""
        return self.turn_resolves and self.root_matches and not self.duplicate_for_turn

    def to_dict(self) -> dict[str, Any]:
        """Deterministic JSON-ready form."""
        return {
            "index": self.index,
            "turn_ref": self.turn_ref,
            "turn_resolves": self.turn_resolves,
            "root_matches": self.root_matches,
            "duplicate_for_turn": self.duplicate_for_turn,
            "stored_root": self.stored_root,
            "recomputed_root": self.recomputed_root,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class ContextVerification:
    """Verification of every receipt in a capsule."""

    verdicts: list[ReceiptVerdict] = field(default_factory=list)
    defects: list[RecordDefect] = field(default_factory=list)
    duplicate_turn_ids: list[str] = field(default_factory=list)

    @property
    def status(self) -> VerifyStatus:
        """``empty`` (nothing to check), ``ok``, or ``defective``."""
        if not self.verdicts and not self.defects:
            return "empty"
        if self.defects or self.duplicate_turn_ids:
            return "defective"
        return "ok" if all(v.ok for v in self.verdicts) else "defective"


def verify_decision_contexts(capsule: Mapping[str, Any]) -> ContextVerification:
    """Re-perform every stored receipt offline (fail-closed).

    Checks, per receipt: the ``turn_ref`` resolves to exactly one turn in the
    thread, the stored ``context_root`` equals the root recomputed from the
    stored ``shown_context``, and no other receipt claims the same turn.
    Malformed entries and a malformed conversation facet are defects.
    """
    loaded = load_records(capsule, RECORD_KEY, DecisionContextReceipt)
    defects = list(loaded.defects)
    try:
        facet = facet_from_capsule(dict(capsule))
    except Exception as exc:  # noqa: BLE001 — any parse failure is a defect
        defects.append(RecordDefect(-1, f"conversation facet: {type(exc).__name__}"))
        facet = None
    dupes = duplicate_turn_ids(facet) if facet is not None else []
    counts: dict[str, int] = {}
    for _, rec in loaded.records:
        counts[rec.turn_ref] = counts.get(rec.turn_ref, 0) + 1
    verdicts = [
        ReceiptVerdict(
            index=index,
            turn_ref=rec.turn_ref,
            turn_resolves=facet is not None
            and resolve_turn(facet, rec.turn_ref) is not None
            and rec.turn_ref not in dupes,
            stored_root=rec.context_root,
            recomputed_root=rec.recompute_root(),
            duplicate_for_turn=counts[rec.turn_ref] > 1,
        )
        for index, rec in loaded.records
    ]
    return ContextVerification(
        verdicts=verdicts,
        defects=defects,
        duplicate_turn_ids=[d for d in dupes if d in counts],
    )


def receipts_for_turn(
    capsule: Mapping[str, Any], turn_ref: str
) -> list[tuple[int, DecisionContextReceipt]]:
    """Return the valid stored receipts anchored to ``turn_ref``."""
    loaded = load_records(capsule, RECORD_KEY, DecisionContextReceipt)
    return [(i, r) for i, r in loaded.records if r.turn_ref == turn_ref]
