"""Tests for PolicyStore."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from novafabric.promote.exceptions import PolicyNotFoundError
from novafabric.promote.policy_store import PolicyStore


def _make_bundle(tag: str) -> str:
    import json

    return json.dumps({
        "proposer_key_ids": ["proposer"],
        "approver_key_ids": ["approver"],
        "self_approval": False,
        "approval_threshold": 1,
        "bypass_valid_duration_hours": 24,
        "tag": tag,
    })


def test_put_and_get_by_version(local_policy_store: PolicyStore) -> None:
    bundle = _make_bundle("v1")
    version = local_policy_store.put(bundle)
    assert isinstance(version, int)
    assert version >= 1
    retrieved = local_policy_store.get_by_version(version)
    assert retrieved == bundle


def test_put_multiple_versions(local_policy_store: PolicyStore) -> None:
    v1 = local_policy_store.put(_make_bundle("v1"))
    v2 = local_policy_store.put(_make_bundle("v2"))
    assert v2 > v1
    assert local_policy_store.get_by_version(v1) == _make_bundle("v1")
    assert local_policy_store.get_by_version(v2) == _make_bundle("v2")


def test_get_by_version_not_found(local_policy_store: PolicyStore) -> None:
    with pytest.raises(PolicyNotFoundError):
        local_policy_store.get_by_version(9999)


def test_get_latest(local_policy_store: PolicyStore) -> None:
    local_policy_store.put(_make_bundle("v1"))
    time.sleep(0.01)
    local_policy_store.put(_make_bundle("v2"))
    result = local_policy_store.get_latest()
    assert "v2" in result


def test_get_latest_no_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = PolicyStore(tmp_path / "empty.db")
    with pytest.raises(PolicyNotFoundError):
        store.get_latest()


def test_get_active_at_boundary(local_policy_store: PolicyStore) -> None:
    """get_active_at should return highest version where created_at <= timestamp."""
    local_policy_store.put(_make_bundle("v1"))
    time.sleep(0.05)  # ensure distinct timestamps
    ts_between = datetime.now(timezone.utc)
    time.sleep(0.05)
    local_policy_store.put(_make_bundle("v2"))

    # At ts_between, only v1 should be active
    result = local_policy_store.get_active_at(ts_between)
    assert "v1" in result
    assert "v2" not in result


def test_get_active_at_no_policy_before_time(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = PolicyStore(tmp_path / "empty.db")
    with pytest.raises(PolicyNotFoundError):
        store.get_active_at(datetime(2000, 1, 1, tzinfo=timezone.utc))


def test_get_latest_version(local_policy_store: PolicyStore) -> None:
    local_policy_store.put(_make_bundle("v1"))
    v2 = local_policy_store.put(_make_bundle("v2"))
    assert local_policy_store.get_latest_version() == v2


def test_namespace_isolation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = PolicyStore(tmp_path / "ns.db")
    store.put(_make_bundle("ns-a"), namespace="ns-a")
    store.put(_make_bundle("ns-b"), namespace="ns-b")

    result_a = store.get_latest(namespace="ns-a")
    result_b = store.get_latest(namespace="ns-b")
    assert "ns-a" in result_a
    assert "ns-b" in result_b
