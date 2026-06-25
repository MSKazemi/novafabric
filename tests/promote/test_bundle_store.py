"""Tests for PromoteBundleStore."""

from __future__ import annotations

import pytest

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.exceptions import BundleNotFoundError

CAPSULE_ID = "cap-test-001"
FAKE_ENVELOPE = b'{"payload":"dGVzdA","payloadType":"proposal","signatures":[]}'


def test_put_and_get_proposal(local_bundle_store: PromoteBundleStore) -> None:
    uuid = local_bundle_store.put_proposal(CAPSULE_ID, FAKE_ENVELOPE)
    assert uuid  # non-empty
    recovered = local_bundle_store.get_proposal(CAPSULE_ID, uuid)
    assert recovered == FAKE_ENVELOPE


def test_list_proposals_empty(local_bundle_store: PromoteBundleStore) -> None:
    result = local_bundle_store.list_proposals("nonexistent-cap")
    assert result == []


def test_list_proposals_returns_uuids(local_bundle_store: PromoteBundleStore) -> None:
    uuid1 = local_bundle_store.put_proposal(CAPSULE_ID, FAKE_ENVELOPE)
    uuids = local_bundle_store.list_proposals(CAPSULE_ID)
    assert uuid1 in uuids


def test_get_proposal_not_found(local_bundle_store: PromoteBundleStore) -> None:
    with pytest.raises(BundleNotFoundError):
        local_bundle_store.get_proposal(CAPSULE_ID, "00000000-0000-0000-0000-000000000000")


def test_put_and_get_approval(local_bundle_store: PromoteBundleStore) -> None:
    proposal_uuid = local_bundle_store.put_proposal(CAPSULE_ID, FAKE_ENVELOPE)
    approval_env = b'{"payload":"YXBwcm92YWw","payloadType":"approval","signatures":[]}'
    approval_uuid = local_bundle_store.put_approval(CAPSULE_ID, approval_env, proposal_uuid)
    assert approval_uuid  # non-empty
    recovered = local_bundle_store.get_approval(CAPSULE_ID, proposal_uuid)
    assert recovered == approval_env


def test_get_approval_not_found(local_bundle_store: PromoteBundleStore) -> None:
    with pytest.raises(BundleNotFoundError):
        local_bundle_store.get_approval(CAPSULE_ID, "no-such-proposal-uuid")


def test_multiple_capsules_isolated(local_bundle_store: PromoteBundleStore) -> None:
    uuid_a = local_bundle_store.put_proposal("cap-a", FAKE_ENVELOPE)
    uuid_b = local_bundle_store.put_proposal("cap-b", FAKE_ENVELOPE)
    assert uuid_a not in local_bundle_store.list_proposals("cap-b")
    assert uuid_b not in local_bundle_store.list_proposals("cap-a")
