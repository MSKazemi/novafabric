"""ADR-0239 — the evidence cart: references, resolved once, disclosed as curated.

Two properties carry the design, and each is a way an exported exhibit could
quietly mislead:

* **D2 — resolve once.** A cart of copies attests to a state that never existed
  at any single instant: twelve items captured at twelve moments, silently mixing
  states if anything changed between the first click and the last.
* **D5 — say it is curated.** `evidence/completeness.py` covers exhaustive
  exports; a cart is the opposite, and a curated bundle that does not say so
  invites being read as complete. In an adversarial setting that is the
  difference between evidence and a misleading exhibit.
"""

from __future__ import annotations

import pytest

from novafabric.evidence.cart import (
    CURATION_DISCLOSURE,
    CartItem,
    CartItemKind,
    EvidenceCart,
    items_from_run_ids,
    with_note,
)

AT = "2026-09-06T00:00:00+00:00"


def _item(ref: str, kind: CartItemKind = CartItemKind.RUN, **kw: object) -> CartItem:
    return CartItem(kind=kind, ref=ref, added_by="alice", added_at=AT, **kw)  # type: ignore[arg-type]


def _resolve(cart: EvidenceCart, **kw: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "resolved_at": "2026-09-06T12:00:00+00:00",
        "resolved_by": "alice",
        "digest_for": lambda item: f"sha256:{item.ref}",
    }
    defaults.update(kw)
    return cart.resolve(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# D1 — a cart of references
# ---------------------------------------------------------------------------


def test_items_are_references_not_copies() -> None:
    cart = EvidenceCart(owner="alice")
    cart.add(_item("run-1"))
    (stored,) = cart.items
    assert stored.ref == "run-1"
    assert not hasattr(stored, "content"), "a cart entry must not carry a copy"


def test_adding_the_same_item_twice_is_a_no_op() -> None:
    """Clicking "add" twice during an investigation is one item."""
    cart = EvidenceCart(owner="alice")
    assert cart.add(_item("run-1")) is True
    assert cart.add(_item("run-1")) is False
    assert len(cart) == 1


def test_identity_ignores_who_added_it_and_when() -> None:
    cart = EvidenceCart(owner="alice")
    cart.add(CartItem(kind=CartItemKind.RUN, ref="run-1", added_by="alice", added_at=AT))
    added = cart.add(
        CartItem(kind=CartItemKind.RUN, ref="run-1", added_by="bob", added_at="later")
    )
    assert added is False, "the same run added by two people is still one item"


def test_the_same_ref_under_a_different_kind_is_a_different_item() -> None:
    """Non-vacuity for the dedup rule — otherwise it could be matching on ref alone."""
    cart = EvidenceCart(owner="alice")
    assert cart.add(_item("x", CartItemKind.RUN)) is True
    assert cart.add(_item("x", CartItemKind.DIFF)) is True
    assert len(cart) == 2


def test_add_order_is_preserved() -> None:
    """The sequence in which an investigator collected things is itself evidence."""
    cart = EvidenceCart(owner="alice")
    for ref in ("c", "a", "b"):
        cart.add(_item(ref))
    assert [i.ref for i in cart.items] == ["c", "a", "b"]


def test_remove_and_clear() -> None:
    cart = EvidenceCart(owner="alice")
    cart.add(_item("run-1"))
    cart.add(_item("run-2"))
    assert cart.remove(CartItemKind.RUN, "run-1") is True
    assert cart.remove(CartItemKind.RUN, "run-1") is False
    assert len(cart) == 1
    cart.clear()
    assert len(cart) == 0


def test_a_note_does_not_mutate_the_original_reference() -> None:
    original = _item("run-1")
    annotated = with_note(original, "the failing shard")
    assert original.note is None
    assert annotated.note == "the failing shard"


def test_items_from_run_ids_records_the_view() -> None:
    """Chain of custody: who added what, when, and from which view."""
    items = items_from_run_ids(
        ["r1", "r2"], added_by="alice", added_at=AT, added_from="/runs?status=error"
    )
    assert [i.added_from for i in items] == ["/runs?status=error"] * 2


# ---------------------------------------------------------------------------
# D2 — resolve once, at one read point
# ---------------------------------------------------------------------------


def test_every_item_shares_one_read_point() -> None:
    """The property that makes the manifest attestable at all."""
    cart = EvidenceCart(owner="alice")
    for ref in ("r1", "r2", "r3"):
        cart.add(_item(ref))
    resolved = _resolve(cart)
    assert resolved.resolved_at == "2026-09-06T12:00:00+00:00"
    assert len(resolved.items) == 3


def test_resolution_reads_each_reference_exactly_once() -> None:
    calls: list[str] = []

    cart = EvidenceCart(owner="alice")
    for ref in ("r1", "r2"):
        cart.add(_item(ref))

    def digest_for(item: CartItem) -> str:
        calls.append(item.ref)
        return f"sha256:{item.ref}"

    _resolve(cart, digest_for=digest_for)
    assert calls == ["r1", "r2"], calls


def test_resolving_twice_produces_independent_snapshots() -> None:
    """An investigation continues after an export; neither snapshot mutates."""
    cart = EvidenceCart(owner="alice")
    cart.add(_item("r1"))
    first = _resolve(cart, resolved_at="T1")
    cart.add(_item("r2"))
    second = _resolve(cart, resolved_at="T2")
    assert len(first.items) == 1
    assert len(second.items) == 2
    assert first.resolved_at == "T1" and second.resolved_at == "T2"


# ---------------------------------------------------------------------------
# An unresolvable reference is reported, never dropped
# ---------------------------------------------------------------------------


def test_an_unresolvable_reference_is_carried_and_marked() -> None:
    """Eleven items delivered for twelve added, with no indication which
    vanished, is a quietly wrong exhibit."""
    cart = EvidenceCart(owner="alice")
    cart.add(_item("good"))
    cart.add(_item("gone"))
    resolved = _resolve(cart, digest_for=lambda i: None if i.ref == "gone" else "sha256:x")

    assert len(resolved.items) == 2, "the missing item must still appear"
    assert len(resolved.unresolved) == 1
    assert resolved.unresolved[0].ref == "gone"
    assert resolved.unresolved[0].unresolved_reason
    assert resolved.complete is False


def test_a_raising_resolver_does_not_lose_the_other_items() -> None:
    cart = EvidenceCart(owner="alice")
    cart.add(_item("boom"))
    cart.add(_item("fine"))

    def digest_for(item: CartItem) -> str:
        if item.ref == "boom":
            raise OSError("disk gone")
        return "sha256:ok"

    resolved = _resolve(cart, digest_for=digest_for)
    assert len(resolved.items) == 2
    assert "OSError" in (resolved.unresolved[0].unresolved_reason or "")
    assert resolved.items[1].digest == "sha256:ok"


def test_all_resolved_reports_complete() -> None:
    """The converse — otherwise `complete` carries no information."""
    cart = EvidenceCart(owner="alice")
    cart.add(_item("r1"))
    assert _resolve(cart).complete is True


# ---------------------------------------------------------------------------
# D5 — the manifest discloses that it is curated
# ---------------------------------------------------------------------------


def test_the_manifest_states_it_is_operator_assembled_and_not_exhaustive() -> None:
    cart = EvidenceCart(owner="alice")
    cart.add(_item("r1"))
    payload = _resolve(cart).as_dict()
    assert payload["operator_assembled"] is True
    assert payload["exhaustive"] is False
    assert payload["disclosure"] == CURATION_DISCLOSURE


def test_the_disclosure_is_machine_and_human_readable() -> None:
    """A flag alone gets ignored by a person; a sentence alone by a tool."""
    payload = _resolve(EvidenceCart(owner="alice")).as_dict()
    assert isinstance(payload["exhaustive"], bool)
    assert "curated subset" in payload["disclosure"]
    assert "not an exhaustive set" in payload["disclosure"]


def test_complete_is_not_a_claim_of_exhaustiveness() -> None:
    """The two claims are different and the manifest must not blur them.

    `all_references_resolved` says every reference was readable.
    `exhaustive: false` says the selection was curated. A fully-resolved cart is
    still not an exhaustive one.
    """
    cart = EvidenceCart(owner="alice")
    cart.add(_item("r1"))
    payload = _resolve(cart).as_dict()
    assert payload["all_references_resolved"] is True
    assert payload["exhaustive"] is False


# ---------------------------------------------------------------------------
# D8 — a legal hold travels with the item
# ---------------------------------------------------------------------------


def test_a_held_item_carries_its_holds_into_the_manifest() -> None:
    """An exported exhibit whose hold status is invisible invites being treated
    as unencumbered."""
    cart = EvidenceCart(owner="alice")
    cart.add(_item("held"))
    resolved = _resolve(cart, holds_for=lambda i: ["hold-7"])
    assert resolved.items[0].legal_holds == ("hold-7",)
    assert resolved.as_dict()["legal_holds_present"] == ["hold-7"]


def test_an_unheld_item_carries_no_holds() -> None:
    cart = EvidenceCart(owner="alice")
    cart.add(_item("free"))
    resolved = _resolve(cart, holds_for=lambda i: [])
    assert resolved.items[0].legal_holds == ()
    assert resolved.as_dict()["legal_holds_present"] == []


def test_an_unknown_hold_status_is_not_reported_as_no_hold() -> None:
    """"We could not establish a hold" and "there is no hold" are different."""
    cart = EvidenceCart(owner="alice")
    cart.add(_item("mystery"))

    def holds_for(item: CartItem) -> list[str]:
        raise RuntimeError("hold store unreachable")

    resolved = _resolve(cart, holds_for=holds_for)
    assert resolved.items[0].legal_holds == ()
    assert "legal-hold status could not be established" in (
        resolved.items[0].unresolved_reason or ""
    )


def test_holds_are_not_queried_for_an_unresolvable_item() -> None:
    """Asking a store about a reference that does not resolve invents a question."""
    queried: list[str] = []
    cart = EvidenceCart(owner="alice")
    cart.add(_item("gone"))
    _resolve(
        cart,
        digest_for=lambda i: None,
        holds_for=lambda i: queried.append(i.ref) or [],  # type: ignore[func-returns-value]
    )
    assert queried == []


# ---------------------------------------------------------------------------
# D7 — ephemeral and local
# ---------------------------------------------------------------------------


def test_the_cart_holds_no_path_to_a_store() -> None:
    """The dashboard's "no persistent state of its own" property.

    A durable cart is a saved investigation — a different feature with different
    lifecycle and access-control questions.
    """
    from dataclasses import fields

    names = {f.name for f in fields(EvidenceCart)}
    assert not any("path" in n or "db" in n or "store" in n for n in names), names


def test_the_item_kind_set_is_closed() -> None:
    """An unresolvable kind must not reach a signed manifest."""
    with pytest.raises(ValueError):
        CartItemKind("something-new")
