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
"""Sweep executor tests: dispatch, evidence, idempotency, fail-safety (ADR-0134 D5/D6)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novafabric.audit import AuditEventType, AuditLog
from novafabric.pii.dek import DEKStore
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.models import RetentionBinding, SweepItem, SweepOutcome
from novafabric.retention.sweep import EXPIRED_MARKER, HoldContext, plan_sweep

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)


def _binding(**overrides: object) -> RetentionBinding:
    data: dict[str, object] = {
        "id": "b1",
        "match": {"tag": "trade"},
        "window": "P90D",
        "action": "purge",
    }
    data.update(overrides)
    return RetentionBinding.model_validate(data)


def _capsule(tmp_path: Path, item_id: str, subject: str | None = None) -> SweepItem:
    d = tmp_path / "capsules" / item_id
    d.mkdir(parents=True)
    (d / "capsule.json").write_text(json.dumps({"run_id": item_id, "created_at": OLD.isoformat()}))
    return SweepItem(
        item_id=item_id,
        created_at=OLD,
        path=d,
        tags={"trade"},
        pii_subject_id=subject,
    )


def _executor(
    tmp_path: Path,
    *,
    dek_store: DEKStore | None = None,
    retention_months: int = 0,
) -> tuple[SweepExecutor, AuditLog]:
    audit = AuditLog(tmp_path / "audit.jsonl")
    return (
        SweepExecutor(
            registry="reg",
            principal="cron://nightly-retention",
            audit_log=audit,
            dek_store=dek_store,
            receipt_dir=tmp_path / "evidence" / "erasure",
            retention_months=retention_months,
        ),
        audit,
    )


# ---------------------------------------------------------------------------
# Apply performs exactly the plan
# ---------------------------------------------------------------------------


def test_purge_deletes_exactly_the_planned_items(tmp_path: Path) -> None:
    due = _capsule(tmp_path, "cap-due")
    fresh = _capsule(tmp_path, "cap-fresh")
    fresh = fresh.model_copy(update={"created_at": NOW - timedelta(days=1)})
    plan = plan_sweep([due, fresh], [_binding()], HoldContext(), now=NOW)
    executor, audit = _executor(tmp_path)

    records = executor.execute(plan, dry_run=False)

    assert [r.item_id for r in records] == ["cap-due"]
    assert records[0].outcome is SweepOutcome.APPLIED
    assert due.path is not None and not due.path.exists()  # purged
    assert fresh.path is not None and fresh.path.exists()  # untouched
    assert audit.verify() == []


def test_expire_metadata_writes_marker_and_is_idempotent(tmp_path: Path) -> None:
    item = _capsule(tmp_path, "cap-1")
    plan = plan_sweep([item], [_binding(action="expire-metadata")], HoldContext(), now=NOW)
    executor, _ = _executor(tmp_path)

    records = executor.execute(plan, dry_run=False)
    assert records[0].outcome is SweepOutcome.APPLIED
    assert item.path is not None
    marker = item.path / EXPIRED_MARKER
    assert marker.exists()
    assert json.loads(marker.read_text())["binding_id"] == "b1"
    assert (item.path / "capsule.json").exists()  # object untouched

    # Re-run: ground truth recomputed; already-expired item is a terminal no-op.
    item2 = item.model_copy(update={"expired": True})
    plan2 = plan_sweep([item2], [_binding(action="expire-metadata")], HoldContext(), now=NOW)
    assert plan2 == []


def test_worm_held_item_is_skipped_and_never_touched(tmp_path: Path) -> None:
    """The load-bearing D4 test: a WORM-retained capsule survives an apply."""
    item = _capsule(tmp_path, "cap-worm")
    holds = HoldContext(worm_locks={"cap-worm": NOW + timedelta(days=365)})
    plan = plan_sweep([item], [_binding()], holds, now=NOW)
    executor, audit = _executor(tmp_path)

    records = executor.execute(plan, dry_run=False)

    assert item.path is not None and item.path.exists()  # NEVER purged under lock
    assert records[0].outcome is SweepOutcome.SKIPPED
    assert records[0].reason == "worm_hold"
    assert records[0].worm_locked_until == NOW + timedelta(days=365)
    # the skip is recorded as evidence, never silently dropped
    entries = audit.query(resource_id="cap-worm")
    assert len(entries) == 1
    assert entries[0].details["outcome"] == "skipped"
    assert entries[0].details["reason"] == "worm_hold"


# ---------------------------------------------------------------------------
# Crypto-shred reuses the ADR-0069 DEKStore
# ---------------------------------------------------------------------------


def test_crypto_shred_destroys_dek_and_writes_receipt(tmp_path: Path) -> None:
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek("user@example.com")
    item = _capsule(tmp_path, "cap-pii", subject="user@example.com")
    plan = plan_sweep([item], [_binding(action="crypto-shred")], HoldContext(), now=NOW)
    executor, audit = _executor(tmp_path, dek_store=store, retention_months=0)

    records = executor.execute(plan, dry_run=False)

    assert records[0].outcome is SweepOutcome.APPLIED
    assert records[0].erasure_receipt_ref is not None
    receipt = json.loads(Path(records[0].erasure_receipt_ref).read_text())
    assert receipt["subject_id"] == "user@example.com"
    assert receipt["capsule_ids_affected"] == ["cap-pii"]
    assert store.get_dek("user@example.com") is None  # DEK destroyed
    assert item.path is not None and item.path.exists()  # ciphertext object intact
    assert audit.verify() == []
    store.close()


def test_crypto_shred_within_art17_window_is_deferred(tmp_path: Path) -> None:
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek("user@example.com")
    item = _capsule(tmp_path, "cap-pii", subject="user@example.com")
    plan = plan_sweep([item], [_binding(action="crypto-shred")], HoldContext(), now=NOW)
    executor, _ = _executor(tmp_path, dek_store=store, retention_months=120)

    records = executor.execute(plan, dry_run=False)

    assert records[0].outcome is SweepOutcome.DEFERRED
    assert records[0].reason == "art17_retention_window"
    assert records[0].erasure_receipt_ref is not None
    assert "deferred" in records[0].erasure_receipt_ref
    assert store.get_dek("user@example.com") is not None  # DEK intact
    store.close()


def test_crypto_shred_with_no_dek_is_applied_noop(tmp_path: Path) -> None:
    store = DEKStore(tmp_path / "dek.db")
    for subject in (None, "never-registered@example.com"):
        item = _capsule(tmp_path, f"cap-{subject or 'none'}", subject=subject)
        plan = plan_sweep([item], [_binding(action="crypto-shred")], HoldContext(), now=NOW)
        executor, _ = _executor(tmp_path, dek_store=store)
        records = executor.execute(plan, dry_run=False)
        assert records[0].outcome is SweepOutcome.APPLIED
        assert records[0].reason == "no_dek"
        assert records[0].erasure_receipt_ref is None
    store.close()


# ---------------------------------------------------------------------------
# Dry-run touches nothing (D3)
# ---------------------------------------------------------------------------


def test_dry_run_touches_nothing_and_writes_no_audit(tmp_path: Path) -> None:
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek("user@example.com")
    purge_item = _capsule(tmp_path, "cap-purge")
    shred_item = _capsule(tmp_path, "cap-shred", subject="user@example.com")
    bindings = [
        _binding(id="b-purge", match={"tag": "trade"}, action="purge"),
    ]
    plan = plan_sweep([purge_item, shred_item], bindings, HoldContext(), now=NOW)
    executor, audit = _executor(tmp_path, dek_store=store, retention_months=0)

    records = executor.execute(plan, dry_run=True)

    assert {r.outcome for r in records} == {SweepOutcome.DRY_RUN}
    assert purge_item.path is not None and purge_item.path.exists()
    assert shred_item.path is not None and shred_item.path.exists()
    assert store.get_dek("user@example.com") is not None
    assert audit.query() == []  # no audit entries in dry-run
    assert not (tmp_path / "evidence").exists()  # no receipts
    store.close()


# ---------------------------------------------------------------------------
# Evidence, fail-safety, bounds
# ---------------------------------------------------------------------------


def test_every_decision_appends_exactly_one_chained_audit_record(tmp_path: Path) -> None:
    due = _capsule(tmp_path, "cap-due")
    held = _capsule(tmp_path, "cap-held")
    holds = HoldContext(worm_locks={"cap-held": NOW + timedelta(days=10)})
    plan = plan_sweep([due, held], [_binding()], holds, now=NOW)
    executor, audit = _executor(tmp_path)

    records = executor.execute(plan, dry_run=False)

    assert len(records) == 2
    entries = audit.query()
    assert len(entries) == 2
    assert all(e.event_type is AuditEventType.RETENTION_ACTION for e in entries)
    assert all(e.actor == "cron://nightly-retention" for e in entries)
    assert audit.verify() == []
    # each audit entry carries a schema-shaped RetentionActionRecord
    for e in entries:
        assert e.details["schema_version"] == "0.1.0"
        assert e.details["binding_id"] == "b1"
        assert e.details["registry"] == "reg"


def test_per_item_failure_is_recorded_and_sweep_continues(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    a = _capsule(tmp_path, "cap-a")
    b = _capsule(tmp_path, "cap-b")
    plan = plan_sweep([a, b], [_binding()], HoldContext(), now=NOW)
    executor, audit = _executor(tmp_path)

    import novafabric.retention.actions as actions_mod

    real_rmtree = actions_mod.shutil.rmtree

    def flaky_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if "cap-a" in str(path):
            raise OSError("disk on fire")
        real_rmtree(path)  # type: ignore[arg-type]

    monkeypatch.setattr(actions_mod.shutil, "rmtree", flaky_rmtree)

    records = executor.execute(plan, dry_run=False)

    by_id = {r.item_id: r for r in records}
    assert by_id["cap-a"].outcome is SweepOutcome.ERROR
    assert by_id["cap-a"].reason is not None and "backend_error" in by_id["cap-a"].reason
    assert by_id["cap-b"].outcome is SweepOutcome.APPLIED  # sweep continued
    assert b.path is not None and not b.path.exists()
    assert len(audit.query()) == 2


def test_ambiguous_binding_conflict_is_error_and_item_untouched(tmp_path: Path) -> None:
    item = _capsule(tmp_path, "cap-conflict")
    bindings = [
        _binding(id="b-purge", action="purge"),
        _binding(id="b-shred", action="crypto-shred"),
    ]
    plan = plan_sweep([item], bindings, HoldContext(), now=NOW)
    executor, _ = _executor(tmp_path)
    records = executor.execute(plan, dry_run=False)
    assert records[0].outcome is SweepOutcome.ERROR
    assert records[0].reason == "ambiguous_binding"
    assert item.path is not None and item.path.exists()


def test_limit_bounds_applied_actions_and_rest_carries_over(tmp_path: Path) -> None:
    items = [_capsule(tmp_path, f"cap-{i}") for i in range(5)]
    plan = plan_sweep(items, [_binding()], HoldContext(), now=NOW)
    executor, _ = _executor(tmp_path)

    records = executor.execute(plan, dry_run=False, limit=2)

    assert len(records) == 2
    remaining = [i for i in items if i.path is not None and i.path.exists()]
    assert len(remaining) == 3  # carried to the next pass


def test_rerun_is_idempotent_noop(tmp_path: Path) -> None:
    item = _capsule(tmp_path, "cap-1")
    plan = plan_sweep([item], [_binding()], HoldContext(), now=NOW)
    executor, audit = _executor(tmp_path)
    executor.execute(plan, dry_run=False)
    # Next pass recomputes ground truth: the purged capsule no longer enumerates.
    from novafabric.retention.sweep import enumerate_capsules

    plan2 = plan_sweep(
        enumerate_capsules(tmp_path / "capsules"), [_binding()], HoldContext(), now=NOW
    )
    assert plan2 == []
    assert len(audit.query()) == 1
