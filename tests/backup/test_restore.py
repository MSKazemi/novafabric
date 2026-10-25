"""ADR-0181 second slice: nova restore (local profile).

Covers: create → restore round-trip into an empty home with the verification
chain green; non-empty-home refusal without --force; --force moving existing
data aside (preserved, never deleted); tampered sets refused before the home
is touched; path-traversal member rejection; the NORMATIVE crypto-shred replay
(a resurrected DEK is re-destroyed); pg-dump sets honestly refused; and CLI
smoke for `nova restore`.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path
from typing import Callable

import pytest
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.audit import AuditEventType, AuditLog
from novafabric.backup import (
    RestoreError,
    create_backup,
    restore_backup,
)
from novafabric.backup.models import (
    PROFILE_PG_DUMP,
    BackupManifest,
    BackupMember,
)
from novafabric.cli.main import app
from novafabric.pii.dek import DEKStore

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


@pytest.fixture()
def source_home(tmp_path: Path) -> Path:
    """A populated local home (real registry schema) to back up."""
    from novafabric.registry.store import get_connection, init_schema

    home = tmp_path / "source-home"
    (home / "capsules" / "run-001").mkdir(parents=True)
    (home / "runs" / "run-002").mkdir(parents=True)

    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, name, asset_type, version, spec_json, created_at) "
        "VALUES ('a1', 'asset-a', 'prompt', '1.0.0', '{}', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    (home / "capsules" / "run-001" / "capsule.yaml").write_text("run_id: run-001\n")
    (home / "runs" / "run-002" / "capsule.yaml").write_text("run_id: run-002\n")
    (home / "config.yaml").write_text("region: eu\n")
    return home


@pytest.fixture()
def backup_set(source_home: Path, tmp_path: Path, _no_signing: None) -> Path:
    return create_backup(tmp_path / "set.tar.gz", home=source_home).archive_path


def _tamper(archive: Path, mutate: Callable[[Path], None]) -> Path:
    workdir = archive.parent / f"{archive.stem}-tamper"
    with tarfile.open(archive, "r:gz") as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
        tar.extractall(workdir, filter="data")
    mutate(workdir)
    tampered = archive.with_name("tampered.tar.gz")
    with tarfile.open(tampered, "w:gz") as tar:
        for name in names:
            tar.add(workdir / name, arcname=name)
    return tampered


def _make_raw_set(path: Path, workdir: Path, members: dict[str, bytes], **overrides: object) -> Path:
    """Build a syntactically valid set whose manifest lists *members* verbatim."""
    import hashlib

    manifest = BackupManifest(
        set_id="01HZZZZZZZZZZZZZZZZZZZZZZZ",
        created_at="2026-07-16T00:00:00+00:00",
        nova_version="0.0.0-test",
        members=[
            BackupMember(
                path=name,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                kind="blob",
            )
            for name, data in members.items()
        ],
        **overrides,  # type: ignore[arg-type]
    )
    workdir.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    for index, (name, data) in enumerate(members.items()):
        src = workdir / f"member-{index}"
        src.write_bytes(data)
        staged[name] = src
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json")))
    with tarfile.open(path, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        for name, src in staged.items():
            tar.add(src, arcname=name)
    return path


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_round_trip_restore_into_empty_home(backup_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "restored-home"
    result = restore_backup(backup_set, home=target)

    assert result.ok is True
    assert result.profile == "local-full"
    assert result.moved_aside is None
    assert [s.name for s in result.steps] == [
        "verify-set",
        "prepare-home",
        "extract",
        "migrations",
        "crypto-shred-replay",
        "ratchet-advance",
        "verify-storage",
        "verify-seal-log",
        "verify-state-dbs",
    ]
    assert all(s.ok for s in result.steps)

    # Restored content is real and readable.
    conn = sqlite3.connect(target / "registry.db")
    rows = {r[0] for r in conn.execute("SELECT name FROM assets")}
    conn.close()
    assert rows == {"asset-a"}
    assert (target / "capsules" / "run-001" / "capsule.yaml").read_text() == (
        "run_id: run-001\n"
    )
    assert (target / "runs" / "run-002" / "capsule.yaml").exists()
    assert (target / "config.redacted.yaml").exists()

    # Verification chain details are honest.
    by_name = {s.name: s for s in result.steps}
    assert "schema_version" in by_name["verify-storage"].detail
    assert "skipped" in by_name["verify-seal-log"].detail  # no Merkle log


def test_restore_runs_migrations_to_head(backup_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "restored-home"
    result = restore_backup(backup_set, home=target)
    step = next(s for s in result.steps if s.name == "migrations")
    assert step.ok is True and "schema_version" in step.detail

    # Bootstrap DDL brought the old snapshot to head: new tables exist.
    conn = sqlite3.connect(target / "registry.db")
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "promotion_proposals" in tables and "runs_cache" in tables


# ---------------------------------------------------------------------------
# Non-empty home / --force
# ---------------------------------------------------------------------------

def test_non_empty_home_refused_without_force(backup_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "occupied-home"
    target.mkdir()
    (target / "registry.db").write_bytes(b"precious existing data")

    with pytest.raises(RestoreError, match="not empty"):
        restore_backup(backup_set, home=target)

    # Untouched.
    assert (target / "registry.db").read_bytes() == b"precious existing data"
    assert list(target.iterdir()) == [target / "registry.db"]


def test_force_moves_existing_data_aside(backup_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "occupied-home"
    (target / "runs" / "old-run").mkdir(parents=True)
    (target / "registry.db").write_bytes(b"old registry bytes")
    (target / "runs" / "old-run" / "capsule.yaml").write_text("run_id: old\n")

    result = restore_backup(backup_set, home=target, force=True)

    assert result.ok is True
    assert result.moved_aside is not None
    pre_restore = Path(result.moved_aside)
    assert pre_restore.parent == target
    assert pre_restore.name.startswith(".pre-restore-")
    # Displaced data preserved byte-for-byte, never deleted.
    assert (pre_restore / "registry.db").read_bytes() == b"old registry bytes"
    assert (pre_restore / "runs" / "old-run" / "capsule.yaml").read_text() == (
        "run_id: old\n"
    )
    # Restored data in place.
    conn = sqlite3.connect(target / "registry.db")
    assert {r[0] for r in conn.execute("SELECT name FROM assets")} == {"asset-a"}
    conn.close()


# ---------------------------------------------------------------------------
# Tamper / traversal safety
# ---------------------------------------------------------------------------

def test_tampered_set_refused_before_touching_home(
    backup_set: Path, tmp_path: Path
) -> None:
    def flip(workdir: Path) -> None:
        (workdir / "registry.db").write_bytes(b"evil bytes")

    tampered = _tamper(backup_set, flip)
    target = tmp_path / "never-created-home"

    with pytest.raises(RestoreError, match="failed verification"):
        restore_backup(tampered, home=target)
    assert not target.exists()  # home never touched


@pytest.mark.parametrize("evil_path", ["../evil.txt", "a/../../evil.txt"])
def test_path_traversal_member_rejected(tmp_path: Path, evil_path: str) -> None:
    raw_set = _make_raw_set(
        tmp_path / "evil.tar.gz",
        tmp_path / "evil-stage",
        {evil_path: b"payload"},
    )
    target = tmp_path / "traversal-home"
    with pytest.raises(RestoreError, match="Unsafe member path"):
        restore_backup(raw_set, home=target)
    assert not target.exists()
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize(
    "evil_path", ["/tmp/evil.txt", "~/evil.txt", "C:\\evil.txt", "keys/seal.pem"]
)
def test_absolute_and_denied_member_paths_rejected(evil_path: str) -> None:
    from novafabric.backup.restore import _reject_unsafe_member

    with pytest.raises(RestoreError):
        _reject_unsafe_member(evil_path)


def test_pg_dump_set_without_dsn_refused_before_touching_anything(
    tmp_path: Path,
) -> None:
    """ADR-0217: pg restore is automated, but with no DSN the pre-flight
    refuses legibly — naming the flag and env vars — before any mutation."""
    raw_set = _make_raw_set(
        tmp_path / "pg.tar.gz",
        tmp_path / "pg-stage",
        {"db.pgdump": b"PGDMP-fake"},
        profile=PROFILE_PG_DUMP,
        db_dump="db.pgdump",
        db_target="db.example.internal/nova",
    )
    target = tmp_path / "pg-home"
    with pytest.raises(RestoreError, match="--dsn or set NOVA_DSN"):
        restore_backup(raw_set, home=target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# NORMATIVE crypto-shred replay (ADR-0181 D4)
# ---------------------------------------------------------------------------

def _seed_shred_record(
    log_path: Path, receipt_path: Path, subject_id: str
) -> None:
    """One applied CRYPTO_SHRED RetentionActionRecord + its erasure receipt."""
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(
            {
                "subject_id": subject_id,
                "erased_at": "2026-07-01T00:00:00+00:00",
                "capsule_ids_affected": ["run-001"],
                "method": "aes-256-gcm-dek-destruction",
                "legal_basis": "GDPR Art.17",
            }
        )
    )
    AuditLog(log_path).append(
        event_type=AuditEventType.RETENTION_ACTION,
        actor="retention-sweep",
        resource_id="run-001",
        details={
            "record_id": "01HZZZZZZZZZZZZZZZZZZZZZZY",
            "action": "crypto-shred",
            "outcome": "applied",
            "item_id": "run-001",
            "binding_id": "b-1",
            "erasure_receipt_ref": str(receipt_path),
        },
    )


def test_shred_replay_re_destroys_resurrected_dek(
    backup_set: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "audit.jsonl"
    receipt = tmp_path / "receipts" / "run-001.receipt.json"
    _seed_shred_record(log_path, receipt, subject_id="subj-1")

    # A resurrected DEK in the target home (e.g. dek.db recovered from
    # somewhere it should not have been). Not part of the set, so --force
    # leaves it in place for the replay to find.
    target = tmp_path / "shred-home"
    target.mkdir()
    store = DEKStore(target / "dek.db")
    store.get_or_create_dek("subj-1")
    store.get_or_create_dek("subj-untouched")
    store.close()

    result = restore_backup(
        backup_set, home=target, force=True, decision_log_path=log_path
    )

    assert result.ok is True
    step = next(s for s in result.steps if s.name == "crypto-shred-replay")
    assert step.ok is True
    assert "1 resurrected DEK(s) re-destroyed" in step.detail

    conn = sqlite3.connect(target / "dek.db")
    subjects = {
        r[0] for r in conn.execute("SELECT subject_id FROM data_subject_deks")
    }
    conn.close()
    assert "subj-1" not in subjects  # shredded stays shredded
    assert "subj-untouched" in subjects  # replay is surgical


def test_shred_replay_with_dek_already_absent(backup_set: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    receipt = tmp_path / "receipts" / "run-001.receipt.json"
    _seed_shred_record(log_path, receipt, subject_id="subj-1")

    target = tmp_path / "clean-home"
    target.mkdir()
    store = DEKStore(target / "dek.db")  # store exists, but subj-1 has no DEK
    store.get_or_create_dek("subj-other")
    store.close()

    result = restore_backup(
        backup_set, home=target, force=True, decision_log_path=log_path
    )
    step = next(s for s in result.steps if s.name == "crypto-shred-replay")
    assert step.ok is True and "1 already absent" in step.detail
    assert result.ok is True


def test_shred_replay_without_dek_store_is_checked_none(
    backup_set: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "audit.jsonl"
    receipt = tmp_path / "receipts" / "run-001.receipt.json"
    _seed_shred_record(log_path, receipt, subject_id="subj-1")

    target = tmp_path / "no-dek-home"
    result = restore_backup(backup_set, home=target, decision_log_path=log_path)
    step = next(s for s in result.steps if s.name == "crypto-shred-replay")
    assert step.ok is True and "checked: none" in step.detail


def test_shred_replay_unresolvable_record_fails_restore(
    backup_set: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "audit.jsonl"
    # Applied shred whose receipt is gone: preservation cannot be proven.
    AuditLog(log_path).append(
        event_type=AuditEventType.RETENTION_ACTION,
        actor="retention-sweep",
        resource_id="run-009",
        details={
            "action": "crypto-shred",
            "outcome": "applied",
            "item_id": "run-009",
            "erasure_receipt_ref": str(tmp_path / "receipts" / "missing.json"),
        },
    )
    target = tmp_path / "unresolved-home"
    target.mkdir()
    DEKStore(target / "dek.db").close()  # a DEK store exists

    result = restore_backup(
        backup_set, home=target, force=True, decision_log_path=log_path
    )
    step = next(s for s in result.steps if s.name == "crypto-shred-replay")
    assert step.ok is False and "cannot be proven" in step.detail
    assert result.ok is False  # verification chain incomplete = failed restore


def test_no_decision_log_is_an_honest_noop(backup_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "no-log-home"
    result = restore_backup(
        backup_set, home=target, decision_log_path=tmp_path / "absent.jsonl"
    )
    step = next(s for s in result.steps if s.name == "crypto-shred-replay")
    assert step.ok is True and "no retention decision log" in step.detail
    assert result.ok is True


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_restore_help() -> None:
    result = runner.invoke(app, ["restore", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--home")
    assert_flag_in_help(result, "--force")


def test_cli_restore_round_trip(backup_set: Path, tmp_path: Path) -> None:
    target = tmp_path / "cli-home"
    result = runner.invoke(app, ["restore", str(backup_set), "--home", str(target)])
    assert result.exit_code == 0, result.output
    assert "Restore complete" in result.output
    assert (target / "registry.db").exists()


def test_cli_restore_refuses_non_empty_home_exit_1(
    backup_set: Path, tmp_path: Path
) -> None:
    target = tmp_path / "cli-occupied"
    target.mkdir()
    (target / "registry.db").write_bytes(b"x")
    result = runner.invoke(app, ["restore", str(backup_set), "--home", str(target)])
    assert result.exit_code == 1
    assert "not empty" in result.output


# ---------------------------------------------------------------------------
# Shred replay hardening (adversarial review 2026-10-22): holds, chain, receipts
# ---------------------------------------------------------------------------


def _resurrected(target: Path, *subjects: str) -> None:
    target.mkdir(exist_ok=True)
    store = DEKStore(target / "dek.db")
    for subject in subjects:
        store.get_or_create_dek(subject)
    store.close()


def _subjects(target: Path) -> set[str]:
    conn = sqlite3.connect(target / "dek.db")
    try:
        return {r[0] for r in conn.execute("SELECT subject_id FROM data_subject_deks")}
    finally:
        conn.close()


def _replay_step(result: object) -> object:
    return next(s for s in result.steps if s.name == "crypto-shred-replay")  # type: ignore[attr-defined]


def test_shred_replay_never_destroys_a_key_under_an_active_legal_hold(
    backup_set: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Destroying a DEK is irreversible; a hold says nothing may be destroyed. The hold
    wins, the DEK stays, and the restore is NOT declared complete (D4 is unmet)."""
    from novafabric.retention.holds import place_hold

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "deployment-home"))
    hold = place_hold("default", "litigation", actor="legal")
    log_path = tmp_path / "audit.jsonl"
    _seed_shred_record(log_path, tmp_path / "receipts" / "run-001.receipt.json", "subj-1")
    target = tmp_path / "held-home"
    _resurrected(target, "subj-1")

    result = restore_backup(backup_set, home=target, force=True, decision_log_path=log_path)

    step = _replay_step(result)
    assert step.ok is False  # type: ignore[attr-defined]
    assert hold.hold_id in step.detail  # type: ignore[attr-defined]
    assert "legal hold" in step.detail  # type: ignore[attr-defined]
    assert result.ok is False
    assert "subj-1" in _subjects(target)


def test_shred_replay_honours_a_hold_that_came_with_the_restored_home(
    backup_set: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "audit.jsonl"
    _seed_shred_record(log_path, tmp_path / "receipts" / "run-001.receipt.json", "subj-1")
    target = tmp_path / "held-home-2"
    _resurrected(target, "subj-1")
    ledger = target / "registries" / "default" / "holds.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {"hold_id": "hold-restored", "registry": "default", "reason": "audit",
             "duration_days": None, "created_at": "2026-07-01T00:00:00+00:00",
             "released_at": None}
        )
        + "\n"
    )

    result = restore_backup(backup_set, home=target, force=True, decision_log_path=log_path)

    step = _replay_step(result)
    assert step.ok is False and "hold-restored" in step.detail  # type: ignore[attr-defined]
    assert "subj-1" in _subjects(target)


def test_shred_replay_refuses_a_log_whose_chain_does_not_verify(
    backup_set: Path, tmp_path: Path
) -> None:
    """A line appended outside the hash chain must not be able to destroy a key."""
    log_path = tmp_path / "audit.jsonl"
    _seed_shred_record(log_path, tmp_path / "receipts" / "run-001.receipt.json", "subj-1")
    victim = tmp_path / "receipts" / "run-002.receipt.json"
    victim.write_text(json.dumps({"subject_id": "victim", "capsule_ids_affected": ["run-002"]}))
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"event_type": "retention.action",
                 "details": {"action": "crypto-shred", "outcome": "applied",
                             "item_id": "run-002", "erasure_receipt_ref": str(victim)}}
            )
            + "\n"
        )
    target = tmp_path / "forged-home"
    _resurrected(target, "subj-1", "victim")

    result = restore_backup(backup_set, home=target, force=True, decision_log_path=log_path)

    step = _replay_step(result)
    assert step.ok is False  # type: ignore[attr-defined]
    assert "chain" in step.detail  # type: ignore[attr-defined]
    assert result.ok is False
    assert {"subj-1", "victim"} <= _subjects(target)  # nothing destroyed from that log


def test_shred_replay_refuses_an_edited_record(backup_set: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    _seed_shred_record(log_path, tmp_path / "receipts" / "run-001.receipt.json", "subj-1")
    victim = tmp_path / "receipts" / "victim.receipt.json"
    victim.write_text(json.dumps({"subject_id": "victim", "capsule_ids_affected": ["run-001"]}))
    entry = json.loads(log_path.read_text().splitlines()[0])
    entry["details"]["erasure_receipt_ref"] = str(victim)
    log_path.write_text(json.dumps(entry) + "\n")
    target = tmp_path / "edited-home"
    _resurrected(target, "victim")

    result = restore_backup(backup_set, home=target, force=True, decision_log_path=log_path)

    assert _replay_step(result).ok is False  # type: ignore[attr-defined]
    assert "victim" in _subjects(target)


def test_a_record_cannot_borrow_another_erasures_receipt(
    backup_set: Path, tmp_path: Path
) -> None:
    """The record names its receipt by path; a receipt for another item does not count."""
    log_path = tmp_path / "audit.jsonl"
    other = tmp_path / "receipts" / "run-999.receipt.json"
    _seed_shred_record(log_path, other, "victim")  # chained record for run-001 -> run-999
    target = tmp_path / "mismatch-home"
    _resurrected(target, "victim")

    result = restore_backup(backup_set, home=target, force=True, decision_log_path=log_path)

    step = _replay_step(result)
    assert step.ok is False and "cannot be proven" in step.detail  # type: ignore[attr-defined]
    assert "victim" in _subjects(target)


def test_a_symlinked_receipt_is_unresolvable(backup_set: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    receipt = tmp_path / "receipts" / "run-001.receipt.json"
    _seed_shred_record(log_path, receipt, "subj-1")
    real = tmp_path / "elsewhere.json"
    real.write_text(receipt.read_text())
    receipt.unlink()
    receipt.symlink_to(real)
    target = tmp_path / "link-home"
    _resurrected(target, "subj-1")

    result = restore_backup(backup_set, home=target, force=True, decision_log_path=log_path)

    assert _replay_step(result).ok is False  # type: ignore[attr-defined]
    assert "subj-1" in _subjects(target)


# --- review storage-backup-16: secret bytes are never written into a readable file ----


@pytest.mark.parametrize("preexisting", [False, True], ids=["new", "truncated-0644"])
def test_a_sensitive_member_is_private_before_its_bytes_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preexisting: bool
) -> None:
    """Key material was created 0644 (or truncated in place, keeping 0644) and filled
    before the follow-up chmod, so a local user could open it in that window."""
    import io
    import os
    import stat

    from novafabric.backup import restore

    dst = tmp_path / "key.pem"
    if preexisting:
        dst.write_bytes(b"old")
        dst.chmod(0o644)
    modes: list[int] = []
    real_copy = restore.shutil.copyfileobj

    def spy(src: object, out: object, *a: object, **k: object) -> None:
        modes.append(stat.S_IMODE(os.fstat(out.fileno()).st_mode))  # type: ignore[attr-defined]
        real_copy(src, out, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(restore.shutil, "copyfileobj", spy)
    # The header is split so a secret scanner does not read a bare marker as a key.
    restore._write_member_bytes(io.BytesIO(b"-----BEGIN PRIVATE" + b" KEY-----"), dst, mode=0o600)
    assert modes == [0o600]
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
