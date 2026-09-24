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
"""The evidence cart — ADR-0239 D1/D2/D5/D7/D8.

Collect while investigating; resolve **once**, at the end.

## Why it holds references and not copies (D2)

    Copying at add-time would snapshot inconsistently: twelve items captured at
    twelve moments during an investigation, silently mixing states if anything
    changed between the first click and the last.

A cart of copies attests to a state that **never existed at any single instant**.
Holding references and resolving once gives the result one coherent read point,
which is the only thing that can honestly be signed. That is not a performance
argument; it is what makes the artifact defensible.

## Why the manifest says it is curated (D5)

A cart is by construction an *operator-assembled subset*. `evidence/completeness.py`
covers exhaustive exports; this is the opposite, and a curated bundle that does not
say so invites being read as complete — **in an adversarial setting that is the
difference between evidence and a misleading exhibit.** So the selection bias is
disclosed in the artifact, by the artifact, rather than left to a covering note
that travels separately and gets lost.

## Ephemeral by design (D7)

The cart is session-scoped and holds no path to any store. That preserves the
dashboard's *"no persistent state of its own"* property: a durable cart is a saved
investigation, which is a different feature with different lifecycle and
access-control questions.

## What this module does not do

**Export is not implemented here, and the reason is recorded rather than worked
around.** ADR-0239 D3 says export produces *a* real Evidence Bundle via
`EvidenceBundleBuilder` — but that builder takes a **single** ``capsule_dir`` and
returns one ZIP, while a cart is multi-item and heterogeneous. Whether N capsules
become one bundle (a new multi-capsule builder) or N bundles bound by a manifest
is a design decision the ADR does not make, and inventing one here would be
choosing a public artifact shape by implementation accident.

:meth:`EvidenceCart.resolve` therefore produces the **manifest** — the
chain-of-custody and curation record that any export shape needs — and stops
there.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CURATION_DISCLOSURE",
    "CartItem",
    "CartItemKind",
    "EvidenceCart",
    "ResolvedCart",
    "ResolvedItem",
]


class CartItemKind(str, enum.Enum):
    """What a cart entry points at. A closed set — an unrecognised kind cannot
    be resolved, and silently carrying one would put an unresolvable reference
    into a signed manifest."""

    RUN = "run"
    CAPSULE = "capsule"
    LINEAGE_QUERY = "lineage_query"
    DIFF = "diff"
    CHART = "chart"
    POLICY_DECISION = "policy_decision"
    AUDIT_RECORD = "audit_record"


#: D5 — the sentence the manifest carries. Written out rather than generated so
#: it reads the same in every artifact and can be grepped for in a review.
CURATION_DISCLOSURE: Final[str] = (
    "This selection was assembled by an operator during an investigation. It is a "
    "curated subset, not an exhaustive set, and no claim is made that it contains "
    "all relevant evidence."
)


@dataclass(frozen=True)
class CartItem:
    """A **reference**, never a copy (D2).

    ``added_from`` records the view the operator was looking at when they added
    it. That is chain-of-custody information — "who added what, when, and from
    which view" — and it is what makes a hand-assembled selection defensible
    rather than arbitrary.
    """

    kind: CartItemKind
    ref: str
    added_by: str
    added_at: str
    added_from: str | None = None
    note: str | None = None

    def identity(self) -> tuple[str, str]:
        """What makes two entries the same item. Deliberately excludes who added
        it and when: adding the same run twice is one item, not two."""
        return (self.kind.value, self.ref)


@dataclass(frozen=True)
class ResolvedItem:
    """One item as it stood at the cart's single read point."""

    kind: str
    ref: str
    added_by: str
    added_at: str
    added_from: str | None
    note: str | None
    #: ``None`` when the reference could not be resolved. Never silently dropped
    #: — see :class:`ResolvedCart`.
    digest: str | None
    #: D8 — an active legal hold travels with the item. An exported exhibit whose
    #: hold status is invisible invites being treated as unencumbered.
    legal_holds: tuple[str, ...] = ()
    unresolved_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "ref": self.ref,
            "added_by": self.added_by,
            "added_at": self.added_at,
            "digest": self.digest,
        }
        if self.added_from:
            payload["added_from"] = self.added_from
        if self.note:
            payload["note"] = self.note
        if self.legal_holds:
            payload["legal_holds"] = list(self.legal_holds)
        if self.unresolved_reason:
            payload["unresolved_reason"] = self.unresolved_reason
        return payload


@dataclass(frozen=True)
class ResolvedCart:
    """The cart at one coherent read point, plus what it could not resolve."""

    resolved_at: str
    resolved_by: str
    items: tuple[ResolvedItem, ...]

    @property
    def unresolved(self) -> tuple[ResolvedItem, ...]:
        """Items whose reference could not be read.

        Reported, never dropped. A manifest silently missing an item the
        investigator added is the same class of defect as a silently truncated
        aggregate (ADR-0234): the artifact looks whole and is not.
        """
        return tuple(item for item in self.items if item.digest is None)

    @property
    def complete(self) -> bool:
        """Every reference resolved. **Not** a claim that the selection is
        exhaustive — see :data:`CURATION_DISCLOSURE`. The two are different
        claims and conflating them is exactly what D5 guards against."""
        return not self.unresolved

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            # D5 — machine-readable and human-readable, together. A flag alone
            # gets ignored by a person; a sentence alone gets ignored by a tool.
            "operator_assembled": True,
            "exhaustive": False,
            "disclosure": CURATION_DISCLOSURE,
            "items": [item.as_dict() for item in self.items],
            "item_count": len(self.items),
            "all_references_resolved": self.complete,
            "unresolved_count": len(self.unresolved),
            "legal_holds_present": sorted(
                {hold for item in self.items for hold in item.legal_holds}
            ),
        }


@dataclass
class EvidenceCart:
    """A session-scoped, ordered set of references (D1/D7).

    Mutable by design — it is a working surface during an investigation — while
    :class:`ResolvedCart` and :class:`CartItem` are frozen, so a resolution is a
    snapshot that later cart edits cannot reach back into.
    """

    owner: str
    _items: list[CartItem] = field(default_factory=list)

    def add(self, item: CartItem) -> bool:
        """Add a reference. Returns False if it was already present.

        Idempotent on ``(kind, ref)``: clicking "add" twice during an
        investigation is one item. Order is preserved, because the sequence in
        which an investigator collected things is itself evidence of how they
        reasoned.
        """
        identity = item.identity()
        if any(existing.identity() == identity for existing in self._items):
            return False
        self._items.append(item)
        return True

    def remove(self, kind: CartItemKind, ref: str) -> bool:
        before = len(self._items)
        self._items = [
            item for item in self._items if item.identity() != (kind.value, ref)
        ]
        return len(self._items) < before

    def clear(self) -> None:
        self._items.clear()

    @property
    def items(self) -> tuple[CartItem, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def resolve(
        self,
        *,
        resolved_at: str,
        resolved_by: str,
        digest_for: Callable[[CartItem], str | None],
        holds_for: Callable[[CartItem], Sequence[str]] | None = None,
    ) -> ResolvedCart:
        """Read every reference **once**, at one instant (D2).

        *resolved_at* is supplied rather than read from the clock so the caller
        owns the read point and the result is testable; every item in one
        resolution shares it, which is precisely the property that makes the
        manifest attestable.

        *digest_for* returns ``None`` for a reference it cannot read. That item
        is carried into the manifest **marked unresolved**, never omitted: an
        investigator who added twelve items and received a manifest of eleven,
        with no indication which vanished, has been handed a quietly wrong
        exhibit.

        Resolving twice produces two independent snapshots; neither mutates the
        cart, so an investigation can continue after an export.
        """
        resolved: list[ResolvedItem] = []
        for item in self._items:
            reason: str | None = None
            try:
                digest = digest_for(item)
            except Exception as exc:  # noqa: BLE001 — one bad ref must not lose the rest
                digest = None
                reason = f"{type(exc).__name__}: {exc}"
            if digest is None and reason is None:
                reason = "reference could not be resolved at the read point"
            holds: tuple[str, ...] = ()
            if holds_for is not None and digest is not None:
                try:
                    holds = tuple(holds_for(item))
                except Exception:  # noqa: BLE001 — an unknown hold status is not "no hold"
                    holds = ()
                    reason = "legal-hold status could not be established"
            resolved.append(
                ResolvedItem(
                    kind=item.kind.value,
                    ref=item.ref,
                    added_by=item.added_by,
                    added_at=item.added_at,
                    added_from=item.added_from,
                    note=item.note,
                    digest=digest,
                    legal_holds=holds,
                    unresolved_reason=reason,
                )
            )
        return ResolvedCart(
            resolved_at=resolved_at, resolved_by=resolved_by, items=tuple(resolved)
        )


def capsule_holds(capsule_dir: Path) -> tuple[str, ...]:
    """Active legal holds on a capsule directory (D8).

    Thin wrapper over the shipped `server.capsule_delete.active_hold_ids` rather
    than a second reader of `holds.jsonl`: two readers of one file drift, and the
    one that drifts here would under-report a hold.
    """
    from novafabric.server.capsule_delete import active_hold_ids

    return tuple(active_hold_ids(capsule_dir))


def items_from_run_ids(
    run_ids: Iterable[str], *, added_by: str, added_at: str, added_from: str | None = None
) -> list[CartItem]:
    """Convenience for the common case: a set of runs collected from one view."""
    return [
        CartItem(
            kind=CartItemKind.RUN,
            ref=run_id,
            added_by=added_by,
            added_at=added_at,
            added_from=added_from,
        )
        for run_id in run_ids
    ]


def with_note(item: CartItem, note: str) -> CartItem:
    """Attach an investigator's note without mutating the original reference."""
    return replace(item, note=note)
