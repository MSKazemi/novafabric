"""``GET /api/seal/log/verify`` reads the log read-only and matches capsule ids exactly.

Adversarial review, 2026-10-23 (attack A4):

* the route built ``MerkleLog(path)``, which creates the database (and its parent
  directory) — so asking a verifier's server to verify *created* a log there
  (verification must create nothing, ADR-0297 S2);
* ``capsule_included`` was ``entry_json LIKE '%"<capsule_id>"%'``: ``%``, ``_%`` or the
  signer's key id all answered ``true``;
* a Postgres DSN in ``NOVAFABRIC_SEAL_DB_PATH`` reached SQLite as a file path → 500.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app
from novafabric.trust.novaseal.merkle import MerkleLog, open_existing_merkle_log

TOKEN = "seal-log-verify-token"
HOST = {"host": "127.0.0.1:4321"}
AUTH = {"token": TOKEN}
CAPSULE_ID = hashlib.sha256(b"a sealed manifest").hexdigest()
KEYID = hashlib.sha256(b"a signer certificate").hexdigest()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    for name in ("NOVAFABRIC_SEAL_CONFIG", "NOVAFABRIC_SEAL_DB_PATH", "NOVAFABRIC_CAPSULE_DIR"):
        monkeypatch.delenv(name, raising=False)
    runs = tmp_path / "runs"
    runs.mkdir()
    return TestClient(
        create_app(token=TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db"),
        raise_server_exceptions=False,
    )


def _sealed_log(path: Path, *, root_version: int | None = None) -> None:
    log = MerkleLog(path, root_version=root_version)
    log.append({"capsule_id": CAPSULE_ID, "has_tsr": False, "keyid": KEYID})
    log.append({"capsule_id": "0" * 64, "has_tsr": True, "keyid": KEYID})
    log.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_missing_log_is_404_and_nothing_is_created(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = tmp_path / "never" / "created" / "merkle.db"
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(absent))
    r = client.get("/api/seal/log/verify", params=AUTH, headers=HOST)
    assert r.status_code == 404, r.text
    assert "no novaseal merkle log" in r.json()["detail"].lower()
    assert not absent.parent.exists(), "verification created the log's directory"


def test_the_default_log_is_not_created_either(client: TestClient, tmp_path: Path) -> None:
    home = tmp_path / "home"
    before = sorted(p.name for p in home.iterdir())
    r = client.get("/api/seal/log/verify", params=AUTH, headers=HOST)
    assert r.status_code == 404, r.text
    assert sorted(p.name for p in home.iterdir()) == before


def test_an_existing_log_verifies_without_being_written(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "merkle.db"
    _sealed_log(db)
    before = _digest(db)
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
    body = client.get("/api/seal/log/verify", params=AUTH, headers=HOST).json()
    assert body["consistent"] is True
    assert body["entry_count"] == 2
    assert _digest(db) == before


@pytest.mark.parametrize(
    ("capsule_id", "included"),
    [
        (CAPSULE_ID, True),
        ("%", False),
        ("_%", False),
        ("%%", False),
        (KEYID, False),
        (CAPSULE_ID[:32], False),
        ('", "', False),
        ("doesnotexist", False),
    ],
)
def test_capsule_included_is_an_exact_match(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsule_id: str,
    included: bool,
) -> None:
    db = tmp_path / "merkle.db"
    _sealed_log(db)
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
    body = client.get(
        "/api/seal/log/verify", params={**AUTH, "capsule_id": capsule_id}, headers=HOST
    ).json()
    assert body["capsule_included"] is included


def test_a_postgres_log_is_refused_clearly_without_leaking_the_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", "postgresql://nova:S3cretPw@127.0.0.1:1/seal")
    r = client.get("/api/seal/log/verify", params=AUTH, headers=HOST)
    assert r.status_code == 501, r.text
    assert "S3cretPw" not in r.text
    assert "nova seal log verify" in r.json()["detail"]


def test_a_file_that_is_not_a_log_is_422(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"not a database at all" * 100)
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(bogus))
    r = client.get("/api/seal/log/verify", params=AUTH, headers=HOST)
    assert r.status_code == 422, r.text


def test_a_legacy_log_is_verified_read_only_without_migration(tmp_path: Path) -> None:
    """A pre-ADR-0313 log (no root_version column, no log_meta row) is not migrated."""
    db = tmp_path / "legacy.db"
    _sealed_log(db, root_version=1)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tree_heads DROP COLUMN root_version")
    conn.execute("DELETE FROM log_meta")
    conn.commit()
    conn.close()
    before = _digest(db)
    log = open_existing_merkle_log(db, read_only=True)
    assert log is not None
    try:
        result = log.verify_consistency()
        assert result.consistent, result.errors
        assert result.root_version == 1
        assert log.has_entry_for_capsule_id(CAPSULE_ID)
    finally:
        log.close()
    assert _digest(db) == before


def test_a_read_only_log_refuses_writes_and_absent_files(tmp_path: Path) -> None:
    db = tmp_path / "merkle.db"
    _sealed_log(db)
    log = MerkleLog(db, read_only=True)
    try:
        with pytest.raises(Exception, match="readonly|read-only"):
            log.append({"capsule_id": "x"})
    finally:
        log.close()
    assert open_existing_merkle_log(tmp_path / "absent.db", read_only=True) is None
    assert not (tmp_path / "absent.db").exists()


def test_verify_capsule_reads_the_sealers_log_without_migrating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``verify_capsule`` opens a SQLite log ``mode=ro`` too: a legacy log stays as it is."""
    import datetime

    import yaml
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from novafabric.capture.finalize import evidence_digests, seal_capsule
    from novafabric.trust.novaseal.capsule_verify import verify_capsule
    from novafabric.trust.novaseal.config import load_signing_profile

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "read-only-verify")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    db = tmp_path / "legacy.db"
    MerkleLog(db, root_version=1).close()
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {db}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
    capsule = tmp_path / "runs" / "r1"
    capsule.mkdir(parents=True)
    (capsule / "env.lock").write_text("python=3.13\n")
    manifest = {"run_id": "r1", "status": "success", "evidence_digests": evidence_digests(capsule)}
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    assert seal_capsule(capsule, manifest) is None
    import gc

    gc.collect()  # close the sealer's connection so no stray checkpoint moves the bytes
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tree_heads DROP COLUMN root_version")
    conn.execute("DELETE FROM log_meta")
    conn.commit()
    conn.close()
    before = _digest(db)
    report = verify_capsule(capsule, load_signing_profile())
    assert report.valid, report.all_errors()
    assert report.result.log_integrity_ok
    assert _digest(db) == before, "verification migrated the sealer's log"
    # A write could also sit in the -wal sidecar (another connection may still be
    # open), so read the schema back through SQLite, which sees the WAL too.
    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tree_heads)")}
        meta_rows = conn.execute("SELECT COUNT(*) FROM log_meta").fetchone()[0]
    finally:
        conn.close()
    assert "root_version" not in columns, "verification migrated the sealer's log"
    assert meta_rows == 0, "verification wrote a root_version to the sealer's log"
