"""ADR-0216: full local-mode backup coverage, key policy, restore hardening.

Covers: every-store collection with signed coverage rows; 0.1.1 manifest
backward compat; the key-material dual opt-in (--include-keys /
--restore-keys); external-origin mapping and sensitive file modes on restore;
never-overwrite-live-audit semantics; the D4 interaction where a moved-aside
LIVE audit log (superset) must still drive shred replay; ratchet
epoch-regression advance; and the verify-state-dbs gate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest

from novafabric.backup.create import create_backup
from novafabric.backup.models import (
    MANIFEST_SCHEMA_VERSION,
    BackupManifest,
)
from novafabric.backup.restore import RestoreError, restore_backup
from novafabric.backup.verify import verify_backup
from novafabric.trust.novaseal.ratchet import init_ratchet, load_state, rotate


@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


def _sqlite_db(path: Path, ddl: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    conn.commit()
    conn.close()


def _dek_db(path: Path, subjects: list[str]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE data_subject_deks (
            subject_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            dek_hex TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    for subject in subjects:
        conn.execute(
            "INSERT INTO data_subject_deks (subject_id, dek_hex, created_at) "
            "VALUES (?, ?, '2026-07-24T00:00:00+00:00')",
            (subject, os.urandom(32).hex()),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def full_home(tmp_path: Path) -> Path:
    """A home populated with EVERY ADR-0216 component (except duckdb/keys)."""
    home = tmp_path / "full-home"
    (home / "capsules" / "run-001").mkdir(parents=True)
    (home / "spool").mkdir()

    # Real registry schema (restore re-runs init_schema; a fake one clashes).
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.close()

    # Real (empty) Merkle log — verify-seal-log runs the real verifier on it.
    from novafabric.trust.novaseal.merkle import open_merkle_log

    open_merkle_log(home / "novaseal-merkle.db")

    _sqlite_db(home / "incidents.db", "CREATE TABLE incidents (id TEXT);")
    _sqlite_db(home / "metadata.db", "CREATE TABLE runs (run_id TEXT);")
    _sqlite_db(home / "tsa_nonces.db", "CREATE TABLE nonces (nonce TEXT);")
    _dek_db(home / "dek.db", ["subject-1", "subject-2"])

    (home / "capsules" / "run-001" / "capsule.yaml").write_text("run_id: run-001\n")
    (home / "config.yaml").write_text("region: eu\n")
    (home / "dashboard-audit.jsonl").write_text('{"action": "x"}\n')
    (home / "spool" / "00000001.jsonl").write_text('{"event": 1}\n')
    init_ratchet("node-a", home / "seal" / "ratchet")
    return home


@pytest.fixture()
def audit_log(tmp_path: Path) -> Path:
    log = tmp_path / "audit-root" / "audit.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"event_type": "other"}\n')
    return log


# ---------------------------------------------------------------------------
# Create: coverage
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_signing")
def test_full_coverage_create(full_home: Path, audit_log: Path, tmp_path: Path) -> None:
    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )
    manifest = result.manifest
    assert manifest.manifest_schema_version == MANIFEST_SCHEMA_VERSION == "0.2.0"

    by_path = {m.path: m for m in manifest.members}
    for expected in (
        "registry.db",
        "incidents.db",
        "metadata.db",
        "dek.db",
        "tsa_nonces.db",
        "novaseal-merkle.db",
        "dashboard-audit.jsonl",
        "spool/00000001.jsonl",
        "seal/ratchet/node-a.json",
        "seal/ratchet/epoch-registry.jsonl",
        "external/audit/audit.jsonl",
        "config.redacted.yaml",
    ):
        assert expected in by_path, f"missing member {expected}"

    assert by_path["dek.db"].sensitive is True
    assert by_path["seal/ratchet/node-a.json"].sensitive is True
    assert by_path["seal/ratchet/epoch-registry.jsonl"].sensitive is False
    assert by_path["external/audit/audit.jsonl"].origin == "audit"
    assert by_path["incidents.db"].kind == "state_db"

    status = {c.component: c.status for c in manifest.coverage}
    assert status["dek"] == "included"
    assert status["audit-log"] == "included"
    assert status["dashboard"] == "absent"  # no dashboard.duckdb in this home
    assert status["keys"] == "excluded"  # default: never key material
    assert manifest.includes_keys is False

    assert verify_backup(result.archive_path).ok is True


@pytest.mark.usefixtures("_no_signing")
def test_manifest_011_still_validates() -> None:
    """A literal 0.1.1 manifest dict must validate with 0.2.0 defaults."""
    manifest = BackupManifest.model_validate(
        {
            "manifest_schema_version": "0.1.1",
            "set_id": "01OLD",
            "created_at": "2026-07-16T00:00:00+00:00",
            "profile": "local-full",
            "nova_version": "0.63.0",
            "schema_revision": "1",
            "members": [
                {
                    "path": "registry.db",
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                    "kind": "registry",
                }
            ],
            "db_dump": "registry.db",
            "object_store_manifest": None,
            "redacted_config": None,
            "signing_status": "unsigned",
            "signing_detail": "old set",
            "signature": None,
        }
    )
    member = manifest.members[0]
    assert member.role is None
    assert member.origin == "home"
    assert member.sensitive is False
    assert manifest.coverage == []
    assert manifest.includes_keys is False


# ---------------------------------------------------------------------------
# Key policy (ADR-0216 D4)
# ---------------------------------------------------------------------------

@pytest.fixture()
def keyring(tmp_path: Path) -> Path:
    keyring = tmp_path / "keyring"
    keyring.mkdir()
    (keyring / "mohsen_at_host.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----FAKE")
    return keyring


@pytest.mark.usefixtures("_no_signing")
def test_default_set_never_contains_key_material(
    full_home: Path, audit_log: Path, keyring: Path, tmp_path: Path
) -> None:
    (full_home / "novaseal.yaml").write_text("profile: local\n")
    result = create_backup(
        tmp_path / "set.tar.gz",
        home=full_home,
        audit_log_path=audit_log,
        keyring_dir=keyring,
    )
    assert not any(m.path.endswith((".pem", ".key")) for m in result.manifest.members)
    assert not any(m.kind == "key_material" for m in result.manifest.members)
    assert result.manifest.includes_keys is False


@pytest.mark.usefixtures("_no_signing")
def test_include_keys_dual_opt_in_round_trip(
    full_home: Path, audit_log: Path, keyring: Path, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "set.tar.gz",
        home=full_home,
        audit_log_path=audit_log,
        keyring_dir=keyring,
        include_keys=True,
    )
    manifest = result.manifest
    assert manifest.includes_keys is True
    key_members = [m for m in manifest.members if m.kind == "key_material"]
    assert key_members and all(m.path.startswith("external/") for m in key_members)
    assert all(m.sensitive for m in key_members)
    assert verify_backup(result.archive_path).ok is True

    # Restore WITHOUT --restore-keys: key members skipped.
    home_b = tmp_path / "restore-no-keys"
    audit_b = tmp_path / "audit-b" / "audit.jsonl"
    ring_b = tmp_path / "ring-b"
    out = restore_backup(
        result.archive_path,
        home=home_b,
        audit_log_path=audit_b,
        keyring_dir=ring_b,
    )
    assert out.ok is True
    assert not ring_b.exists() or not list(ring_b.iterdir())
    extract = next(s for s in out.steps if s.name == "extract")
    assert "SKIPPED" in extract.detail and "--restore-keys" in extract.detail

    # Restore WITH --restore-keys: keys land in the keyring root, mode 0600.
    home_c = tmp_path / "restore-keys"
    audit_c = tmp_path / "audit-c" / "audit.jsonl"
    ring_c = tmp_path / "ring-c"
    out = restore_backup(
        result.archive_path,
        home=home_c,
        audit_log_path=audit_c,
        keyring_dir=ring_c,
        restore_keys=True,
    )
    assert out.ok is True
    restored_pem = ring_c / "mohsen_at_host.pem"
    assert restored_pem.read_bytes() == b"-----BEGIN PRIVATE KEY-----FAKE"
    assert stat.S_IMODE(restored_pem.stat().st_mode) == 0o600


@pytest.mark.usefixtures("_no_signing")
def test_key_member_with_lying_manifest_fails_verify(
    full_home: Path, audit_log: Path, keyring: Path, tmp_path: Path
) -> None:
    """A set whose manifest says includes_keys=false but carries a key member
    is an invalid set — the D4 opt-in is enforced by verify, not convention."""
    result = create_backup(
        tmp_path / "set.tar.gz",
        home=full_home,
        audit_log_path=audit_log,
        keyring_dir=keyring,
        include_keys=True,
    )
    workdir = tmp_path / "tamper"
    workdir.mkdir()
    with tarfile.open(result.archive_path, "r:gz") as tar:
        tar.extractall(workdir, filter="data")
    manifest_data = json.loads((workdir / "manifest.json").read_text())
    manifest_data["includes_keys"] = False
    (workdir / "manifest.json").write_text(json.dumps(manifest_data, sort_keys=True))
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        for f in sorted(workdir.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(workdir)))
    verdict = verify_backup(tampered)
    assert verdict.ok is False
    assert any("excluded path" in e for e in verdict.errors)


# ---------------------------------------------------------------------------
# Restore: origin mapping, modes, live-audit protection
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_signing")
def test_restore_origin_mapping_and_sensitive_modes(
    full_home: Path, audit_log: Path, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )
    home_b = tmp_path / "home-b"
    audit_b = tmp_path / "audit-b" / "audit.jsonl"
    out = restore_backup(result.archive_path, home=home_b, audit_log_path=audit_b)
    assert out.ok is True, [s for s in out.steps if not s.ok]

    # External origin: audit log restored OUTSIDE the home, at its real root.
    assert audit_b.read_text() == audit_log.read_text()
    assert not (home_b / "external").exists()

    # Sensitive modes: dek.db 0600; ratchet state 0600; ratchet dir 0700.
    assert stat.S_IMODE((home_b / "dek.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((home_b / "seal/ratchet/node-a.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home_b / "seal" / "ratchet").stat().st_mode) == 0o700

    # Every state DB restored and provably openable.
    state_step = next(s for s in out.steps if s.name == "verify-state-dbs")
    assert state_step.ok and "open clean" in state_step.detail


@pytest.mark.usefixtures("_no_signing")
def test_restore_never_silently_overwrites_live_audit_log(
    full_home: Path, audit_log: Path, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )
    home_b = tmp_path / "home-b"
    live_audit = tmp_path / "live" / "audit.jsonl"
    live_audit.parent.mkdir(parents=True)
    live_audit.write_text('{"live": true}\n')

    with pytest.raises(RestoreError, match="refusing to overwrite live external"):
        restore_backup(result.archive_path, home=home_b, audit_log_path=live_audit)
    assert live_audit.read_text() == '{"live": true}\n'  # untouched

    out = restore_backup(
        result.archive_path, home=home_b, audit_log_path=live_audit, force=True
    )
    assert out.ok is True
    assert out.moved_aside is not None
    aside = Path(out.moved_aside) / "external" / "audit" / "audit.jsonl"
    assert aside.read_text() == '{"live": true}\n'  # preserved, never deleted
    assert live_audit.read_text() == audit_log.read_text()  # restored content


@pytest.mark.usefixtures("_no_signing")
def test_shred_in_live_log_survives_restore_of_older_backup(
    full_home: Path, audit_log: Path, tmp_path: Path
) -> None:
    """D4 sharp edge: the backup predates the shred; the shred record lives
    only in the LIVE audit log that --force moves aside. The moved-aside log
    must still drive replay — shredded stays shredded."""
    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )

    receipt = tmp_path / "receipt-s2.json"
    receipt.write_text(json.dumps({"subject_id": "subject-2"}))
    live_audit = tmp_path / "live" / "audit.jsonl"
    live_audit.parent.mkdir(parents=True)
    live_audit.write_text(
        json.dumps(
            {
                "event_type": "retention.action",
                "details": {
                    "action": "crypto-shred",
                    "outcome": "applied",
                    "erasure_receipt_ref": str(receipt),
                },
            }
        )
        + "\n"
    )

    home_b = tmp_path / "home-b"
    out = restore_backup(
        result.archive_path, home=home_b, audit_log_path=live_audit, force=True
    )
    assert out.ok is True
    conn = sqlite3.connect(home_b / "dek.db")
    subjects = {r[0] for r in conn.execute("SELECT subject_id FROM data_subject_deks")}
    conn.close()
    assert subjects == {"subject-1"}  # subject-2 re-destroyed from the live log
    replay = next(s for s in out.steps if s.name == "crypto-shred-replay")
    assert "re-destroyed" in replay.detail


# ---------------------------------------------------------------------------
# Ratchet epoch regression (ADR-0216 D5)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_signing")
def test_ratchet_regression_advances_past_registry_max(
    full_home: Path, audit_log: Path, tmp_path: Path
) -> None:
    state_dir = full_home / "seal" / "ratchet"
    stale_state = (state_dir / "node-a.json").read_text()  # epoch 0 snapshot
    rotate("node-a", state_dir)
    rotate("node-a", state_dir)  # registry now knows epochs 0..2
    # Simulate a backup that captured the epoch-0 state with the epoch-2
    # registry (the regression restore must burn 0..2).
    (state_dir / "node-a.json").write_text(stale_state)

    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )
    home_b = tmp_path / "home-b"
    out = restore_backup(
        result.archive_path,
        home=home_b,
        audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
    )
    assert out.ok is True
    step = next(s for s in out.steps if s.name == "ratchet-advance")
    assert "epoch regression" in step.detail
    assert load_state("node-a", home_b / "seal" / "ratchet").epoch == 3


@pytest.mark.usefixtures("_no_signing")
def test_ratchet_current_state_is_untouched(
    full_home: Path, audit_log: Path, tmp_path: Path
) -> None:
    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )
    home_b = tmp_path / "home-b"
    out = restore_backup(
        result.archive_path,
        home=home_b,
        audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
    )
    step = next(s for s in out.steps if s.name == "ratchet-advance")
    assert step.ok and "already current" in step.detail
    assert load_state("node-a", home_b / "seal" / "ratchet").epoch == 0


# ---------------------------------------------------------------------------
# verify-state-dbs gate
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_no_signing")
def test_corrupt_state_db_fails_restore(
    full_home: Path, audit_log: Path, tmp_path: Path
) -> None:
    """A set whose incidents.db is garbage (hash-consistent, so verify-set
    passes) must fail verify-state-dbs → failed restore."""
    import hashlib

    result = create_backup(
        tmp_path / "set.tar.gz", home=full_home, audit_log_path=audit_log
    )
    workdir = tmp_path / "tamper"
    workdir.mkdir()
    with tarfile.open(result.archive_path, "r:gz") as tar:
        tar.extractall(workdir, filter="data")
    garbage = b"this is not a sqlite database at all"
    (workdir / "incidents.db").write_bytes(garbage)
    manifest_data = json.loads((workdir / "manifest.json").read_text())
    for member in manifest_data["members"]:
        if member["path"] == "incidents.db":
            member["sha256"] = hashlib.sha256(garbage).hexdigest()
            member["size_bytes"] = len(garbage)
    (workdir / "manifest.json").write_text(json.dumps(manifest_data, sort_keys=True))
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        for f in sorted(workdir.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(workdir)))

    home_b = tmp_path / "home-b"
    out = restore_backup(
        tampered,
        home=home_b,
        audit_log_path=tmp_path / "audit-b" / "audit.jsonl",
    )
    assert out.ok is False
    step = next(s for s in out.steps if s.name == "verify-state-dbs")
    assert step.ok is False and "incidents.db" in step.detail
