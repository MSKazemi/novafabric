# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for edge_typer — pure function role-pair → edge_type (cap-002, FR-07)."""

from __future__ import annotations

from novafabric.capsule.edge_typer import coerce_legacy_edge_type, resolve_edge_type
from novafabric.capsule.schema import CapsuleRole, DistributionRole, EdgeType


def test_parent_child_resolves_contains() -> None:
    et = resolve_edge_type(CapsuleRole.PARENT.value, CapsuleRole.CHILD.value)
    assert et == EdgeType.contains


def test_driver_worker_resolves_contains() -> None:
    et = resolve_edge_type(
        DistributionRole.DRIVER.value,
        DistributionRole.WORKER.value,
    )
    assert et == EdgeType.contains


def test_standalone_child_resolves_spawned() -> None:
    et = resolve_edge_type(CapsuleRole.STANDALONE.value, CapsuleRole.CHILD.value)
    assert et == EdgeType.spawned


def test_is_replay_flag_resolves_replayed_from() -> None:
    et = resolve_edge_type(is_replay=True)
    assert et == EdgeType.replayed_from


def test_is_delegation_flag_resolves_delegated_to() -> None:
    et = resolve_edge_type(is_delegation=True)
    assert et == EdgeType.delegated_to


def test_unknown_pair_defaults_to_contains() -> None:
    et = resolve_edge_type("unknown_role_a", "unknown_role_b")
    assert et == EdgeType.contains


def test_none_roles_default_to_contains() -> None:
    et = resolve_edge_type()
    assert et == EdgeType.contains


def test_replay_flag_overrides_delegation() -> None:
    """is_replay takes precedence over is_delegation."""
    et = resolve_edge_type(is_replay=True, is_delegation=True)
    assert et == EdgeType.replayed_from


# ── coerce_legacy_edge_type ───────────────────────────────────────────────


def test_coerce_none_returns_contains() -> None:
    """Pre-0.2.0 rows without edge_type → contains (FR-07)."""
    et = coerce_legacy_edge_type(None)
    assert et == EdgeType.contains


def test_coerce_legacy_consumed() -> None:
    et = coerce_legacy_edge_type("consumed")
    assert et == EdgeType.contains


def test_coerce_legacy_produced_by() -> None:
    et = coerce_legacy_edge_type("produced_by")
    assert et == EdgeType.contains


def test_coerce_legacy_derived_from() -> None:
    et = coerce_legacy_edge_type("derived_from")
    assert et == EdgeType.spawned


def test_coerce_legacy_replayed_from() -> None:
    et = coerce_legacy_edge_type("replayed_from")
    assert et == EdgeType.replayed_from


def test_coerce_already_typed_contains() -> None:
    et = coerce_legacy_edge_type("contains")
    assert et == EdgeType.contains


def test_coerce_already_typed_spawned() -> None:
    et = coerce_legacy_edge_type("spawned")
    assert et == EdgeType.spawned


def test_coerce_already_typed_delegated_to() -> None:
    et = coerce_legacy_edge_type("delegated_to")
    assert et == EdgeType.delegated_to


def test_coerce_unknown_string_defaults_to_contains() -> None:
    et = coerce_legacy_edge_type("some_future_type")
    assert et == EdgeType.contains
