"""Crypto-shred never destroys a subject's key while another capsule must keep its PII.

Review compliance-privacy-08: the DEK is per subject, so shredding because ``run-dev``
was due also erased the subject's PII in ``run-prod``, which the same plan reported
as WORM-held ("No action — not even crypto-shred — is applied to an item whose WORM
locked_until is in the future" was false in effect). A sibling that no binding
covers, or that is not yet due, was erased the same way.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from novafabric.audit import AuditLog
from novafabric.pii.dek import DEKStore
from novafabric.pii.envelope import subject_ref
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.models import RetentionBinding, SweepOutcome
from novafabric.retention.sweep import HoldContext, enumerate_capsules, plan_sweep

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
PEPPER = b"retention-test-pepper"
SUBJECT = "alice"


def capture_shaped(capsules: Path, run_id: str, refs: list[str], env: str) -> Path:
    """A capsule as ``nova capture --pii-subject`` leaves it (envelope bookkeeping)."""
    d = capsules / run_id
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text(yaml.dump({
        "run_id": run_id, "created_at": OLD.isoformat(), "deployment_environment": env,
    }))
    entries = [
        {"field_path": "outputs/stdout.txt", "detection_rule_id": "EMAIL",
         "legal_basis": "operator_declared_compliance", "subject_id_hmac": ref,
         "redacted_at_utc": OLD.isoformat(), "protection": "envelope"}
        for ref in refs
    ]
    (d / "redaction_manifest.json").write_text(json.dumps({"capsule_id": run_id, "entries": entries}))
    return d


def executor(tmp_path: Path, store: DEKStore, pepper: bytes | None) -> SweepExecutor:
    return SweepExecutor(
        registry="reg", principal="test", audit_log=AuditLog(tmp_path / "audit.jsonl"),
        dek_store=store, receipt_dir=tmp_path / "receipts", retention_months=0,
        hold_check=lambda _: None, pepper=pepper,
    )

_DEV_ONLY = RetentionBinding.model_validate({
    "id": "dev", "match": {"age": "P30D", "deployment_environment": "dev"},
    "window": "P90D", "action": "crypto-shred",
})
_ALL = RetentionBinding.model_validate(
    {"id": "all", "match": {"age": "P30D"}, "window": "P90D", "action": "crypto-shred"}
)


def _sweep(tmp_path: Path, binding: RetentionBinding, holds: HoldContext) -> dict[str, tuple]:
    items = enumerate_capsules(tmp_path / "capsules")
    plan = plan_sweep(items, [binding], holds, now=NOW)
    store = DEKStore(tmp_path / "dek.db")
    records = executor(tmp_path, store, PEPPER).execute(plan, dry_run=False, inventory=items)
    return {r.item_id: (r.outcome, r.reason) for r in records}


def _two_capsules(tmp_path: Path) -> DEKStore:
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek(SUBJECT)
    ref = subject_ref(PEPPER, SUBJECT)
    capture_shaped(tmp_path / "capsules", "run-dev", [ref], "dev")
    capture_shaped(tmp_path / "capsules", "run-prod", [ref], "production")
    return store


def test_a_worm_held_sibling_keeps_the_subject_key(tmp_path: Path) -> None:
    store = _two_capsules(tmp_path)
    holds = HoldContext(worm_locks={"run-prod": NOW + timedelta(days=3650)})
    records = _sweep(tmp_path, _ALL, holds)
    assert records["run-prod"][0] is SweepOutcome.SKIPPED  # held, as before
    outcome, reason = records["run-dev"]
    assert outcome is SweepOutcome.SKIPPED
    assert reason is not None and "run-prod" in reason
    assert store.get_dek(SUBJECT) is not None, "the WORM-held capsule's PII was erased"


def test_a_sibling_no_binding_covers_keeps_the_subject_key(tmp_path: Path) -> None:
    store = _two_capsules(tmp_path)
    records = _sweep(tmp_path, _DEV_ONLY, HoldContext())
    assert records["run-dev"][0] is SweepOutcome.SKIPPED
    assert store.get_dek(SUBJECT) is not None


def test_when_every_capsule_is_due_the_key_is_shredded(tmp_path: Path) -> None:
    store = _two_capsules(tmp_path)
    records = _sweep(tmp_path, _ALL, HoldContext())
    assert {o for o, _ in records.values()} == {SweepOutcome.APPLIED}
    assert store.get_dek(SUBJECT) is None
    assert OLD < NOW  # the fixtures are old enough to be due
