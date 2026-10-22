"""A hold placed between ``plan`` and ``apply`` still wins (ADR-0134 D4).

Suspected by the adversarial review and confirmed by reading: ``nova retention
apply`` read the holds once, while building the plan, then showed a confirmation
prompt (which can wait indefinitely) and executed the plan without looking again.
A legal hold placed while the operator sat at the prompt -- or while a long sweep
was running -- did not stop the purge.

Pinned here: the executor re-reads holds immediately before every destructive
action, a ``crypto-shred`` the DEK store refuses under a hold is recorded
``skipped``, never ``applied`` (a restore would otherwise *replay* it as an
evidenced shred), and a failing re-check fails closed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.pii.dek import DEKStore
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.holds import place_hold
from novafabric.retention.models import (
    Decision,
    PlannedDecision,
    RetentionAction,
    SweepItem,
    SweepOutcome,
)

runner = CliRunner()
REGISTRY = "prod"
OLD = datetime.now(tz=timezone.utc) - timedelta(days=400)
_POLICY = """
version: 1
registry: prod
retention_days: 30
deletion_mode: defensible
bindings:
  - id: purge-old
    match:
      tag: trade
    window: P90D
    action: purge
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    return h


def _capsule(home: Path, run_id: str = "cap-old") -> Path:
    d = home / "capsules" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capsule.json").write_text(
        json.dumps({"run_id": run_id, "created_at": OLD.isoformat(), "metadata": {"tags": "trade"}})
    )
    return d


def _due(item: SweepItem, action: RetentionAction) -> PlannedDecision:
    return PlannedDecision(
        item=item,
        binding_id="b",
        matched_binding_ids=["b"],
        action=action,
        due_at=OLD,
        decision=Decision.DUE,
    )


def test_executor_rechecks_holds_before_a_purge(home: Path) -> None:
    cap = _capsule(home)
    plan = [_due(SweepItem(item_id="cap-old", created_at=OLD, path=cap), RetentionAction.PURGE)]
    place_hold(REGISTRY, "litigation", actor="legal")  # after planning
    records = SweepExecutor(registry=REGISTRY, principal="t", audit_log=None).execute(
        plan, dry_run=False
    )
    assert records[0].outcome is SweepOutcome.SKIPPED
    assert records[0].reason is not None and records[0].reason.startswith("legal_hold_active")
    assert cap.is_dir()


def test_a_failing_recheck_fails_closed(home: Path) -> None:
    cap = _capsule(home)
    plan = [_due(SweepItem(item_id="cap-old", created_at=OLD, path=cap), RetentionAction.PURGE)]

    def broken(_: PlannedDecision) -> str | None:
        raise OSError("hold store unreadable")

    records = SweepExecutor(
        registry=REGISTRY, principal="t", audit_log=None, hold_check=broken
    ).execute(plan, dry_run=False)
    assert records[0].outcome is SweepOutcome.SKIPPED
    assert "hold_check_failed" in str(records[0].reason)
    assert cap.is_dir()


def test_a_held_crypto_shred_is_skipped_not_applied(home: Path) -> None:
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek("user-42")
    item = SweepItem(item_id="cap-old", created_at=OLD, pii_subject_id="user-42")
    executor = SweepExecutor(
        registry=REGISTRY,
        principal="t",
        audit_log=None,
        dek_store=store,
        retention_months=0,
        hold_check=lambda _: None,  # isolate the DEK store's own hold check
    )
    place_hold("another-registry", "litigation", actor="legal")
    try:
        records = executor.execute([_due(item, RetentionAction.CRYPTO_SHRED)], dry_run=False)
        assert store.get_dek("user-42") is not None
    finally:
        store.close()
    assert records[0].outcome is SweepOutcome.SKIPPED
    assert records[0].reason == "legal_hold_active"


def test_cli_apply_honours_a_hold_placed_at_the_prompt(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _capsule(home)
    policy = tmp_path / "cwd" / ".novafabric" / "registries" / REGISTRY / "retention-policy.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text(_POLICY)

    def confirm_while_a_hold_lands(*_: object, **__: object) -> bool:
        place_hold(REGISTRY, "subpoena arrived", actor="legal")
        return True

    monkeypatch.setattr("typer.confirm", confirm_while_a_hold_lands)
    res = runner.invoke(app, ["retention", "apply", "--registry", REGISTRY, "--json"])
    assert isinstance(res.exception, SystemExit) or res.exception is None, res.exception
    assert cap.is_dir(), res.output
    assert '"skipped"' in res.output
