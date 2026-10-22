"""Pin what ``nova retention`` crypto-shred does TODAY, so the docs label stays honest.

The retention ``crypto-shred`` action keys DEK destruction on the capsule manifest's
``metadata.pii_subject_id`` (``retention/sweep.py:enumerate_capsules``,
``retention/actions.py:_apply_crypto_shred``). Verified 2026-10-22:

1. nothing in NovaFabric writes ``metadata.pii_subject_id`` — not ``nova capture
   --pii-subject`` (which encrypts PII under the subject's DEK but records only an HMAC
   ``subject_ref``), not the framework adapters;
2. the sweep enumerates ``<capsule-dir>/<run>/capsule.json`` only, while ``nova
   capture`` and the in-process writers produce ``capsule.yaml``.

So a crypto-shred binding destroys no key: a matched capsule records ``applied`` with
reason ``no_dek`` and the subject's DEK stays active. ``docs/cli-reference.md`` labels
the action **experimental** with that limitation; ``nova pii erase`` is the working
crypto-shred. When either fact changes, these tests fail on purpose — update the
docs label in the same change.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novafabric.audit import AuditLog
from novafabric.pii.dek import DEKStore
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.models import RetentionBinding, SweepOutcome
from novafabric.retention.sweep import HoldContext, enumerate_capsules, plan_sweep

SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)

_BINDING = RetentionBinding.model_validate(
    {"id": "gdpr", "match": {"tag": "contains-pii"}, "window": "P90D",
     "action": "crypto-shred"}
)


def test_no_writer_of_pii_subject_id_exists_outside_retention() -> None:
    """Fact 1. A hit here means the writer was built: relabel the docs, then update this pin.

    Matches the key as a quoted string literal (how any writer would set it); prose
    in help text and docstrings that merely names ``metadata.pii_subject_id`` is not
    a writer.
    """
    pattern = re.compile(r"""["']pii_subject_id["']""")
    writers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "retention" not in path.relative_to(SRC).parts
        and pattern.search(path.read_text(encoding="utf-8"))
    )
    assert writers == [], (
        "metadata.pii_subject_id is now referenced outside retention/: "
        f"{writers}. Retention crypto-shred may work end to end now - re-verify and "
        "update the 'experimental' limitation in docs/cli-reference.md."
    )


def test_sweep_does_not_enumerate_capsule_yaml_manifests(tmp_path: Path) -> None:
    """Fact 2. ``nova capture`` writes capsule.yaml; the sweep only reads capsule.json."""
    run = tmp_path / "capsules" / "01RUN"
    run.mkdir(parents=True)
    (run / "capsule.yaml").write_text(
        f"run_id: 01RUN\ncreated_at: '{OLD.isoformat()}'\n"
        "metadata:\n  tags: contains-pii\n  pii_subject_id: user-42\n"
    )
    assert enumerate_capsules(tmp_path / "capsules") == []


def test_crypto_shred_of_a_capture_shaped_manifest_destroys_no_key(tmp_path: Path) -> None:
    """A matched capsule whose manifest carries no subject: ``no_dek``, the DEK survives."""
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek("user-42")
    run = tmp_path / "capsules" / "01RUN"
    run.mkdir(parents=True)
    (run / "capsule.json").write_text(
        json.dumps({"run_id": "01RUN", "created_at": OLD.isoformat(),
                    "metadata": {"tags": "contains-pii"}})
    )
    items = enumerate_capsules(tmp_path / "capsules")
    assert [i.pii_subject_id for i in items] == [None]
    plan = plan_sweep(items, [_BINDING], HoldContext(), now=NOW)
    executor = SweepExecutor(
        registry="reg", principal="test", audit_log=AuditLog(tmp_path / "audit.jsonl"),
        dek_store=store, receipt_dir=tmp_path / "receipts", retention_months=0,
    )
    records = executor.execute(plan, dry_run=False)
    assert [(r.outcome, r.reason) for r in records] == [(SweepOutcome.APPLIED, "no_dek")]
    assert store.get_dek("user-42") is not None, "a key was destroyed"
    assert not (tmp_path / "receipts").exists()
