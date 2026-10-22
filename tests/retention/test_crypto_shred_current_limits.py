"""Pin what ``nova retention`` crypto-shred does TODAY, so the docs label stays honest.

The retention ``crypto-shred`` action destroys a data subject's DEK. Verified
2026-10-22 on branch ``r4g4-lineage-retention``:

1. nothing in NovaFabric writes ``metadata.pii_subject_id``. ``nova capture
   --pii-subject`` encrypts PII under the subject's DEK and records only the subject's
   HMAC (``sha256:…``) in ``redaction_manifest.json`` (ADR-0314 D4/D5); the raw id
   lives only in ``dek.db``;
2. the sweep enumerates ``capsule.yaml`` (what ``nova capture`` writes) as well as
   ``capsule.json``, and reads those envelope subject HMACs; ``apply`` acts on
   ``capsule.yaml`` capsules only with ``include_capture_capsules: true``;
3. at apply time the HMAC is resolved to a live DEK with ``NOVA_PII_PEPPER``, and that
   DEK is destroyed: crypto-shred is **real** for capsules captured with
   ``--pii-subject``. Without the pepper, or when no live DEK matches, it destroys
   nothing and says so (``skipped``), never ``applied``;
4. a capsule with no subject at all (captured without ``--pii-subject``) still records
   ``applied``/``no_dek``: there is nothing to shred.

The DEK is per subject, not per capsule: shredding because one capsule is due makes the
subject's PII unreadable in *every* capsule captured with that ``--pii-subject``.
``docs/cli-reference.md`` states this. When any fact changes, these tests fail on
purpose — update the docs in the same change.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.audit import AuditLog
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.pii.capture_stage import SubjectEnvelopeStage
from novafabric.pii.dek import DEKStore
from novafabric.pii.envelope import ERASED, EnvelopeKeyring, read_capsule_file, subject_ref
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.models import RetentionBinding, SweepOutcome
from novafabric.retention.sweep import HoldContext, enumerate_capsules, plan_sweep

SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
PEPPER = b"retention-test-pepper"
SUBJECT = "user-4242"

_BINDING = RetentionBinding.model_validate(
    {"id": "gdpr", "match": {"age": "P30D"}, "window": "P90D", "action": "crypto-shred"}
)
runner = CliRunner()


def _capture_shaped(capsules: Path, run_id: str, refs: list[str]) -> Path:
    """A capsule as ``nova capture --pii-subject`` leaves it: yaml + envelope bookkeeping."""
    d = capsules / run_id
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text(yaml.dump({"run_id": run_id, "created_at": OLD.isoformat()}))
    entries = [
        {"field_path": "outputs/stdout.txt", "detection_rule_id": "EMAIL",
         "legal_basis": "operator_declared_compliance", "subject_id_hmac": ref,
         "redacted_at_utc": OLD.isoformat(), "protection": "envelope"}
        for ref in refs
    ]
    (d / "redaction_manifest.json").write_text(
        json.dumps({"capsule_id": run_id, "entries": entries})
    )
    return d


def _executor(tmp_path: Path, store: DEKStore, pepper: bytes | None) -> SweepExecutor:
    return SweepExecutor(
        registry="reg", principal="test", audit_log=AuditLog(tmp_path / "audit.jsonl"),
        dek_store=store, receipt_dir=tmp_path / "receipts", retention_months=0,
        hold_check=lambda _: None, pepper=pepper,
    )


def _shred(tmp_path: Path, store: DEKStore, pepper: bytes | None) -> list[tuple[object, ...]]:
    items = enumerate_capsules(tmp_path / "capsules")
    plan = plan_sweep(items, [_BINDING], HoldContext(), now=NOW)
    records = _executor(tmp_path, store, pepper).execute(plan, dry_run=False)
    return [(r.outcome, r.reason) for r in records]


def test_no_writer_of_pii_subject_id_exists_outside_retention() -> None:
    """Fact 1. A hit means a writer of the raw subject id was built: re-verify the docs.

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
        f"{writers}. Re-verify retention crypto-shred and docs/cli-reference.md."
    )


def test_sweep_enumerates_capsule_yaml_and_reads_envelope_subject_refs(tmp_path: Path) -> None:
    """Fact 2."""
    ref = subject_ref(PEPPER, SUBJECT)
    _capture_shaped(tmp_path / "capsules", "01RUN", [ref])
    [item] = enumerate_capsules(tmp_path / "capsules")
    assert (item.manifest, item.pii_subject_id, item.pii_subject_refs) == (
        "capsule.yaml", None, [ref]
    )


def test_masked_spans_are_not_shred_targets(tmp_path: Path) -> None:
    """A ``protection: mask`` span is already irrecoverable; it names no DEK to destroy."""
    d = _capture_shaped(tmp_path / "capsules", "01RUN", [])
    (d / "redaction_manifest.json").write_text(json.dumps({"capsule_id": "01RUN", "entries": [
        {"field_path": "x", "detection_rule_id": "EMAIL", "legal_basis": "b",
         "subject_id_hmac": subject_ref(PEPPER, SUBJECT), "redacted_at_utc": "t",
         "protection": "mask"}]}))
    assert enumerate_capsules(tmp_path / "capsules")[0].pii_subject_refs == []


def test_crypto_shred_destroys_the_subject_dek_of_a_capture_capsule(tmp_path: Path) -> None:
    """Fact 3: the DEK named by the capsule's envelope HMAC is destroyed, with a receipt."""
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek(SUBJECT)
    store.get_or_create_dek("someone-else")
    _capture_shaped(tmp_path / "capsules", "01RUN", [subject_ref(PEPPER, SUBJECT)])
    assert _shred(tmp_path, store, PEPPER) == [(SweepOutcome.APPLIED, None)]
    assert store.get_dek(SUBJECT) is None, "the subject's DEK survived"
    assert store.get_dek("someone-else") is not None, "another subject's DEK was destroyed"
    receipt = json.loads((tmp_path / "receipts" / "01RUN.receipt.json").read_text())
    assert receipt["subject_id"] == SUBJECT
    # Re-running is an idempotent no-op that still reads as done (ADR-0134 D2).
    assert _shred(tmp_path, store, PEPPER) == [(SweepOutcome.APPLIED, "already_shredded")]


def test_without_the_pepper_nothing_is_destroyed_and_it_is_not_applied(tmp_path: Path) -> None:
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek(SUBJECT)
    _capture_shaped(tmp_path / "capsules", "01RUN", [subject_ref(PEPPER, SUBJECT)])
    assert _shred(tmp_path, store, None) == [(SweepOutcome.SKIPPED, "pii_pepper_unavailable")]
    assert store.get_dek(SUBJECT) is not None


def test_a_wrong_pepper_resolves_nothing_and_is_not_applied(tmp_path: Path) -> None:
    """Erased and wrong-pepper look the same from the store; neither may read as applied."""
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek(SUBJECT)
    _capture_shaped(tmp_path / "capsules", "01RUN", [subject_ref(PEPPER, SUBJECT)])
    assert _shred(tmp_path, store, b"wrong") == [(SweepOutcome.SKIPPED, "pii_subject_unresolved")]
    assert store.get_dek(SUBJECT) is not None


def test_a_capsule_captured_without_a_subject_destroys_no_key(tmp_path: Path) -> None:
    """Fact 4: nothing to shred is recorded ``no_dek``; no DEK is touched."""
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek(SUBJECT)
    run = tmp_path / "capsules" / "01RUN"
    run.mkdir(parents=True)
    (run / "capsule.json").write_text(
        json.dumps({"run_id": "01RUN", "created_at": OLD.isoformat()})
    )
    assert _shred(tmp_path, store, PEPPER) == [(SweepOutcome.APPLIED, "no_dek")]
    assert store.get_dek(SUBJECT) is not None, "a key was destroyed"
    assert not (tmp_path / "receipts").exists()


def test_end_to_end_nova_capture_then_retention_apply_shreds_the_pii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``--pii-subject`` capture, then ``nova retention apply`` with the opt-in."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVA_PII_PEPPER", PEPPER.decode())
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    email = "alice.smith@example.com"
    stage = SubjectEnvelopeStage(
        email, pepper=PEPPER, dek_store_factory=lambda: DEKStore(home / "dek.db")
    )
    capture = CaptureOrchestrator(base_dir=home / "capsules", masking_pipeline=stage).run(
        command=[sys.executable, "-c", f"print('contact {email}')"]
    )
    assert capture.exit_code == 0
    policy = cwd / ".novafabric" / "registries" / "reg" / "retention-policy.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        "version: 1\nregistry: reg\nretention_days: 0\ninclude_capture_capsules: true\n"
        "bindings:\n  - id: shred\n    match: {age: PT0S}\n    window: PT0S\n"
        "    action: crypto-shred\n"
    )
    result = runner.invoke(
        app, ["retention", "apply", "-r", "reg", "--yes", "--json", "--retention-months", "0"]
    )
    assert isinstance(result.exception, SystemExit) or result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    [record] = json.loads(result.stdout)["due"]
    assert (record["item_id"], record["outcome"]) == (capture.run_id, "applied")
    keyring = EnvelopeKeyring.from_home(home, PEPPER)
    try:
        stdout = read_capsule_file(
            capture.capsule_dir, "outputs/stdout.txt", keyring=keyring, run_id=capture.run_id
        )
    finally:
        keyring.close()
    assert email not in str(stdout)
    assert ERASED in str(stdout)
