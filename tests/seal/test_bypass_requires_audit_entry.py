"""A bypass counts only when the audit log records it (review r8, hole 2b).

``_active_bypass`` checked signature, trust root, approver, capsule and expiry, but
never that the bypass went through ``put_bypass``: a trusted approver with write
access to the bundle store could drop a signed bypass file next to the others and
skip the ``seal.bypass.create`` entry and the notification — the two compensating
controls ADR-0315's amendment makes a bypass fail closed on. A bypass now counts only
when the hash-chained log holds a self-consistent ``seal.bypass.create`` entry for
the same capsule, ``bypass_uuid`` and envelope SHA-256, and no ``seal.bypass.abort``
for that ``bypass_uuid``. ``NOVAFABRIC_PROMOTE_BYPASS_AUDIT_OPTIONAL=1`` waives the
check for verifying a copied store without its audit log.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.predicates import (
    BYPASS_PAYLOAD_TYPE,
    PROMOTE_CA_BUNDLE_ENV,
    PROMOTE_INSECURE_ENV,
    build_bypass_predicate,
)
from novafabric.promote.verifier import PROMOTE_BYPASS_AUDIT_OPTIONAL_ENV, verify_sod

from .test_promote_trust_root import CAPSULE, REASON, Pki, _policy, _trust


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROMOTE_CA_BUNDLE_ENV, raising=False)
    monkeypatch.delenv(PROMOTE_INSECURE_ENV, raising=False)
    monkeypatch.delenv(PROMOTE_BYPASS_AUDIT_OPTIONAL_ENV, raising=False)


@pytest.fixture()
def pki(tmp_path: Path) -> Pki:
    return Pki(tmp_path)


def _envelope(pki: Pki, cn: str = "alice") -> tuple[bytes, str]:
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    predicate = build_bypass_predicate(
        CAPSULE, "ab" * 32, "production", REASON, cn, until, []
    )
    return pki.sign(pki.identity(cn), predicate, BYPASS_PAYLOAD_TYPE), until


def _plant(store: PromoteBundleStore, envelope: bytes, until: str) -> str:
    """Write a bypass file the way the store lays it out, but without ``put_bypass``."""
    bypass_uuid = str(uuid.uuid4())
    path = store.bypass_path(CAPSULE, bypass_uuid)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"envelope": envelope.decode(), "bypass_uuid": bypass_uuid, "valid_until": until}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return bypass_uuid


def _audit(event: AuditEventType, bypass_uuid: str, envelope: bytes, **over: str) -> None:
    details = {
        "bypass_uuid": bypass_uuid,
        "envelope_sha256": hashlib.sha256(envelope).hexdigest(),
        **{k: v for k, v in over.items() if k != "resource_id"},
    }
    AuditLog(resolve_audit_log_path()).append(
        event, "tester", over.get("resource_id", CAPSULE), details
    )


def _verify(pki: Pki, store: PromoteBundleStore) -> object:
    return verify_sod(CAPSULE, _policy(pki, ["alice"]), store, trust=_trust(pki))


def test_a_bypass_stored_through_put_bypass_counts(pki: Pki) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    store.put_bypass(CAPSULE, envelope, until)

    result = _verify(pki, store)
    assert result.bypass_used and result.passed  # type: ignore[attr-defined]


def test_a_planted_bypass_with_no_audit_entry_is_ignored(pki: Pki) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    _plant(store, envelope, until)

    result = _verify(pki, store)
    assert not result.bypass_used and not result.passed  # type: ignore[attr-defined]


def test_a_bypass_withdrawn_by_an_abort_entry_is_ignored(pki: Pki) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    bypass_uuid = _plant(store, envelope, until)
    _audit(AuditEventType.SEAL_BYPASS_CREATE, bypass_uuid, envelope)
    _audit(AuditEventType.SEAL_BYPASS_ABORT, bypass_uuid, envelope)

    assert not _verify(pki, store).bypass_used  # type: ignore[attr-defined]


def test_a_swapped_envelope_does_not_match_its_audit_entry(pki: Pki) -> None:
    audited, until = _envelope(pki)
    swapped, _ = _envelope(pki)  # another valid bypass by the same approver
    store = PromoteBundleStore(pki.tmp / "bundles")
    bypass_uuid = _plant(store, swapped, until)
    _audit(AuditEventType.SEAL_BYPASS_CREATE, bypass_uuid, audited)

    assert not _verify(pki, store).bypass_used  # type: ignore[attr-defined]


def test_an_entry_for_another_capsule_does_not_count(pki: Pki) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    bypass_uuid = _plant(store, envelope, until)
    _audit(AuditEventType.SEAL_BYPASS_CREATE, bypass_uuid, envelope, resource_id="other")

    assert not _verify(pki, store).bypass_used  # type: ignore[attr-defined]


def test_a_hand_edited_entry_whose_hash_does_not_recompute_does_not_count(pki: Pki) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    bypass_uuid = _plant(store, envelope, until)
    line = json.dumps(
        {
            "entry_id": "x",
            "timestamp": "2026-10-23T00:00:00Z",
            "event_type": "seal.bypass.create",
            "actor": "mallory",
            "resource_id": CAPSULE,
            "details": {
                "bypass_uuid": bypass_uuid,
                "envelope_sha256": hashlib.sha256(envelope).hexdigest(),
            },
            "prev_hash": None,
            "entry_hash": "0" * 64,
        }
    )
    log = resolve_audit_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    assert not _verify(pki, store).bypass_used  # type: ignore[attr-defined]


def test_the_explicit_opt_out_accepts_a_store_copied_without_its_audit_log(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    _plant(store, envelope, until)
    monkeypatch.setenv(PROMOTE_BYPASS_AUDIT_OPTIONAL_ENV, "1")

    result = _verify(pki, store)
    assert result.bypass_used  # type: ignore[attr-defined]
    assert "audit entry not checked" in result.message  # type: ignore[attr-defined]


def test_the_keyword_overrides_the_environment(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, until = _envelope(pki)
    store = PromoteBundleStore(pki.tmp / "bundles")
    _plant(store, envelope, until)
    monkeypatch.setenv(PROMOTE_BYPASS_AUDIT_OPTIONAL_ENV, "1")

    result = verify_sod(
        CAPSULE, _policy(pki, ["alice"]), store, trust=_trust(pki), require_bypass_audit=True
    )
    assert not result.bypass_used
