"""Tests for the portable agent-passport projection (ADR-0149 / NF-179)."""
from __future__ import annotations

import pytest

from novafabric.interop.passport import (
    PASSPORT_COMPONENTS,
    ComponentState,
    PassportStatus,
    build_passport,
)


def test_all_components_present_is_green() -> None:
    doc = build_passport(
        agent_ref="agent@v1",
        present={name: f"sha256:{name}" for name in PASSPORT_COMPONENTS},
    )
    assert doc.status is PassportStatus.green
    assert all(c.state is ComponentState.present for c in doc.components)
    # Every present component carries a ref (its digest), never a body.
    assert all(c.ref for c in doc.components)


def test_missing_identity_is_red() -> None:
    # identity is the anchor: without it there is no basis for a passport.
    present = {n: f"sha256:{n}" for n in PASSPORT_COMPONENTS if n != "identity"}
    doc = build_passport(agent_ref="agent@v1", present=present)
    assert doc.status is PassportStatus.red
    identity = next(c for c in doc.components if c.name == "identity")
    assert identity.state is ComponentState.absent


def test_missing_non_identity_component_is_amber() -> None:
    present = {n: f"sha256:{n}" for n in PASSPORT_COMPONENTS if n != "package"}
    doc = build_passport(agent_ref="agent@v1", present=present)
    assert doc.status is PassportStatus.amber
    package = next(c for c in doc.components if c.name == "package")
    assert package.state is ComponentState.absent


def test_opaque_ancestor_is_amber_not_green() -> None:
    # Honest amber: a component that exists but whose source cannot be attested
    # (e.g. an opaque lineage ancestor) must NOT be reported green.
    present = {n: f"sha256:{n}" for n in PASSPORT_COMPONENTS if n != "lineage"}
    doc = build_passport(agent_ref="agent@v1", present=present, opaque=("lineage",))
    assert doc.status is PassportStatus.amber
    lineage = next(c for c in doc.components if c.name == "lineage")
    assert lineage.state is ComponentState.opaque
    # An opaque component carries no fabricated ref.
    assert lineage.ref is None


def test_opaque_identity_is_amber_not_red() -> None:
    # identity present-but-opaque still anchors the passport (amber, not red).
    present = {n: f"sha256:{n}" for n in PASSPORT_COMPONENTS if n != "identity"}
    doc = build_passport(agent_ref="agent@v1", present=present, opaque=("identity",))
    assert doc.status is PassportStatus.amber


def test_projection_is_not_signed_first_slice() -> None:
    doc = build_passport(agent_ref="agent@v1", present={"identity": "sha256:x"})
    # This first slice is an unsigned projection; seal-path signing is a follow-on.
    assert doc.signed is False


def test_no_verdict_field_beyond_status() -> None:
    doc = build_passport(agent_ref="agent@v1", present={"identity": "sha256:x"})
    forbidden = {"valid", "trusted", "certified", "verdict", "compliant", "pass", "passed"}
    assert forbidden.isdisjoint(doc.model_dump().keys())


def test_unknown_component_rejected() -> None:
    with pytest.raises(ValueError):
        build_passport(agent_ref="agent@v1", present={"not_a_component": "sha256:x"})


def test_component_cannot_be_both_present_and_opaque() -> None:
    with pytest.raises(ValueError):
        build_passport(
            agent_ref="agent@v1",
            present={"identity": "sha256:x"},
            opaque=("identity",),
        )
