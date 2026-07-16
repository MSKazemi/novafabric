"""ADR-0181 first slice: nova backup create/verify (local profile).

Covers: round-trip create → verify; tamper detection; the normative key-material
deny-filter; live-writer safety via the SQLite online-backup API; unsigned
honesty fields; DSSE signing when a local NovaSeal profile is configured; and
CLI smoke for both commands.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from typing import Callable

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.backup import (
    BackupCreateError,
    create_backup,
    verify_backup,
)
from novafabric.backup.create import is_denied
from novafabric.cli.main import app

runner = CliRunner()

SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the honest-unsigned path regardless of the dev machine's config."""
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A populated local NovaFabric home, including material that must be excluded."""
    home = tmp_path / "nova-home"
    (home / "capsules" / "run-001").mkdir(parents=True)
    (home / "runs" / "run-002").mkdir(parents=True)
    (home / "keys").mkdir()

    conn = sqlite3.connect(home / "registry.db")
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE assets (name TEXT);
        INSERT INTO assets VALUES ('asset-a');
        """
    )
    conn.commit()
    conn.close()

    (home / "capsules" / "run-001" / "capsule.yaml").write_text("run_id: run-001\n")
    (home / "capsules" / "run-001" / "trace.jsonl").write_text('{"event": 1}\n')
    (home / "runs" / "run-002" / "capsule.yaml").write_text("run_id: run-002\n")
    (home / "config.yaml").write_text(f"api_key: {SECRET}\nregion: eu\n")

    # Material the deny-filter must keep out of any set.
    (home / "keys" / "ed25519.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----FAKE")
    (home / ".serve-token").write_text("token-value")
    (home / "capsules" / "run-001" / "stray.key").write_bytes(b"stray key material")
    return home


@pytest.fixture()
def novaseal_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configured local NovaSeal profile (key + self-signed cert + yaml)."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal-material" / "seal.pem"
    key_path.parent.mkdir()
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Backup-Test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = key_path.parent / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"merkle_db: {tmp_path / 'merkle.db'}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(cfg))
    return cfg


def _tamper(archive: Path, mutate: Callable[[Path], None]) -> Path:
    """Unpack → mutate → repack, preserving member order/names."""
    workdir = archive.parent / f"{archive.stem}-tamper"
    names: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
        tar.extractall(workdir, filter="data")
    mutate(workdir)
    tampered = archive.with_name("tampered.tar.gz")
    with tarfile.open(tampered, "w:gz") as tar:
        for name in names:
            tar.add(workdir / name, arcname=name)
    return tampered


# ---------------------------------------------------------------------------
# Round-trip + manifest contents
# ---------------------------------------------------------------------------

def test_round_trip_create_then_verify_ok(home: Path, tmp_path: Path, _no_signing: None) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    assert result.archive_path.exists()

    manifest = result.manifest
    assert manifest.profile == "local-full"
    assert manifest.db_dump == "registry.db"
    assert manifest.redacted_config == "config.redacted.yaml"
    assert manifest.object_store_manifest is None
    assert manifest.schema_revision == "1"
    assert manifest.nova_version
    paths = {m.path for m in manifest.members}
    assert "registry.db" in paths
    assert "capsules/run-001/capsule.yaml" in paths
    assert "runs/run-002/capsule.yaml" in paths

    verdict = verify_backup(result.archive_path)
    assert verdict.ok is True
    assert verdict.set_id == manifest.set_id
    assert len(verdict.ok_members) == len(manifest.members)
    assert verdict.mismatched == [] and verdict.missing == []


def test_output_path_may_be_a_directory(home: Path, tmp_path: Path, _no_signing: None) -> None:
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    result = create_backup(out_dir, home=home)
    assert result.archive_path.parent == out_dir
    assert result.manifest.set_id in result.archive_path.name


def test_create_without_registry_fails(tmp_path: Path, _no_signing: None) -> None:
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    with pytest.raises(BackupCreateError, match="registry.db"):
        create_backup(tmp_path / "set.tar.gz", home=empty_home)


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------

def test_tampered_member_fails_verify(home: Path, tmp_path: Path, _no_signing: None) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)

    def flip(workdir: Path) -> None:
        target = workdir / "capsules" / "run-001" / "capsule.yaml"
        target.write_text("run_id: EVIL\n")

    tampered = _tamper(result.archive_path, flip)
    verdict = verify_backup(tampered)
    assert verdict.ok is False
    assert [c.path for c in verdict.mismatched] == ["capsules/run-001/capsule.yaml"]


def test_missing_member_fails_verify(home: Path, tmp_path: Path, _no_signing: None) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    workdir = tmp_path / "unpack"
    with tarfile.open(result.archive_path, "r:gz") as tar:
        members = [m.name for m in tar.getmembers() if m.isfile()]
        tar.extractall(workdir, filter="data")
    members.remove("registry.db")
    stripped = tmp_path / "stripped.tar.gz"
    with tarfile.open(stripped, "w:gz") as tar:
        for name in members:
            tar.add(workdir / name, arcname=name)

    verdict = verify_backup(stripped)
    assert verdict.ok is False
    assert [c.path for c in verdict.missing] == ["registry.db"]


def test_unknown_manifest_key_rejected(home: Path, tmp_path: Path, _no_signing: None) -> None:
    from novafabric.backup.verify import BackupVerifyError

    result = create_backup(tmp_path / "set.tar.gz", home=home)

    def add_key(workdir: Path) -> None:
        manifest = json.loads((workdir / "manifest.json").read_text())
        manifest["smuggled"] = True
        (workdir / "manifest.json").write_text(json.dumps(manifest))

    tampered = _tamper(result.archive_path, add_key)
    with pytest.raises(BackupVerifyError, match="manifest schema"):
        verify_backup(tampered)


# ---------------------------------------------------------------------------
# Normative exclusions (ADR-0181 D3)
# ---------------------------------------------------------------------------

def test_key_material_never_lands_in_the_tarball(
    home: Path, tmp_path: Path, _no_signing: None
) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    with tarfile.open(result.archive_path, "r:gz") as tar:
        names = tar.getnames()
    for name in names:
        assert "keys/" not in name and not name.startswith("keys")
        assert not name.endswith((".pem", ".key"))
        assert ".serve-token" not in name and ".server-token" not in name
    # The stray key inside the capsule tree was also filtered.
    assert "capsules/run-001/stray.key" not in names
    assert not any(m.path.endswith((".pem", ".key")) for m in result.manifest.members)


def test_config_is_redacted_in_the_set(home: Path, tmp_path: Path, _no_signing: None) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    with tarfile.open(result.archive_path, "r:gz") as tar:
        fh = tar.extractfile("config.redacted.yaml")
        assert fh is not None
        redacted = fh.read().decode()
    assert SECRET not in redacted
    assert "region: eu" in redacted  # non-secret content survives


@pytest.mark.parametrize(
    "path",
    [
        "keys/ed25519.pem",
        "nested/keys/x.txt",
        "signing.key",
        "a/b/cert.pem",
        ".serve-token",
        ".server-token",
        ".env",
    ],
)
def test_deny_filter(path: str) -> None:
    assert is_denied(path) is True


def test_deny_filter_allows_normal_members() -> None:
    assert is_denied("capsules/run-001/capsule.yaml") is False
    assert is_denied("registry.db") is False


# ---------------------------------------------------------------------------
# Live-writer safety (ADR-0181 D5 — SQLite online-backup API)
# ---------------------------------------------------------------------------

def test_create_while_a_live_connection_holds_the_db(
    home: Path, tmp_path: Path, _no_signing: None
) -> None:
    live = sqlite3.connect(home / "registry.db")
    try:
        live.execute("INSERT INTO assets VALUES ('asset-live')")
        live.commit()
        live.execute("BEGIN")
        live.execute("INSERT INTO assets VALUES ('uncommitted')")

        result = create_backup(tmp_path / "set.tar.gz", home=home)
    finally:
        live.rollback()
        live.close()

    # Snapshot is consistent and contains only committed data.
    workdir = tmp_path / "unpack-live"
    with tarfile.open(result.archive_path, "r:gz") as tar:
        tar.extractall(workdir, filter="data")
    conn = sqlite3.connect(workdir / "registry.db")
    rows = {r[0] for r in conn.execute("SELECT name FROM assets")}
    conn.close()
    assert "asset-a" in rows and "asset-live" in rows
    assert "uncommitted" not in rows


# ---------------------------------------------------------------------------
# Signing honesty (worm-conformance A5 pattern) + DSSE
# ---------------------------------------------------------------------------

def test_unsigned_honesty_fields(home: Path, tmp_path: Path, _no_signing: None) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    manifest = result.manifest
    assert manifest.signing_status == "unsigned"
    assert manifest.signature is None
    assert manifest.signing_detail is not None and "NOT cryptographically signed" in (
        manifest.signing_detail
    )
    with tarfile.open(result.archive_path, "r:gz") as tar:
        assert "manifest.dsse.json" not in tar.getnames()

    verdict = verify_backup(result.archive_path)
    assert verdict.ok is True
    assert verdict.signing_status == "unsigned"
    assert verdict.signature_verified is None


def test_signed_manifest_when_novaseal_local_configured(
    home: Path, tmp_path: Path, novaseal_local: Path
) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    manifest = result.manifest
    assert manifest.signing_status == "signed"
    assert manifest.signature is not None
    assert manifest.signature.algorithm == "ecdsa-p256-sha256"
    assert manifest.signature.key_id and manifest.signature.sig
    with tarfile.open(result.archive_path, "r:gz") as tar:
        assert "manifest.dsse.json" in tar.getnames()

    verdict = verify_backup(result.archive_path)
    assert verdict.ok is True
    assert verdict.signing_status == "signed"
    assert verdict.signature_verified is True


def test_signed_manifest_tamper_breaks_dsse(
    home: Path, tmp_path: Path, novaseal_local: Path
) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)

    def edit_manifest(workdir: Path) -> None:
        manifest = json.loads((workdir / "manifest.json").read_text())
        manifest["created_at"] = "1999-01-01T00:00:00+00:00"
        (workdir / "manifest.json").write_text(json.dumps(manifest))

    tampered = _tamper(result.archive_path, edit_manifest)
    verdict = verify_backup(tampered)
    assert verdict.ok is False
    assert verdict.signature_verified is False
    assert any("signature" in e.lower() for e in verdict.errors)


# ---------------------------------------------------------------------------
# Postgres profile (ADR-0181 second slice) — pg_dump member, DSN hygiene
# ---------------------------------------------------------------------------

FAKE_DSN = "postgresql://nova:sup3r-secret-pw@db.example.internal:5432/novadb"


@pytest.fixture()
def fake_pg_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake pg_dump on PATH that writes a deterministic custom-format stub."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    script = bindir / "pg_dump"
    script.write_text(
        "#!/bin/sh\n"
        'out=""\nprev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--file" ]; then out="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'printf "PGDMP-fake-custom-format" > "$out"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return bindir


def test_pg_profile_produces_dump_member_and_manifest(
    home: Path, tmp_path: Path, _no_signing: None, fake_pg_dump: Path
) -> None:
    result = create_backup(
        tmp_path / "pg-set.tar.gz", home=home, profile="pg", dsn=FAKE_DSN
    )
    manifest = result.manifest
    assert manifest.profile == "pg-dump"
    assert manifest.db_dump == "db.pgdump"
    assert manifest.db_target == "db.example.internal/novadb"  # redacted, no creds
    kinds = {m.path: m.kind for m in manifest.members}
    assert kinds["db.pgdump"] == "db_dump"
    assert kinds["registry.db"] == "registry"  # local registry still included

    verdict = verify_backup(result.archive_path)
    assert verdict.ok is True
    assert verdict.profile == "pg-dump"


def test_pg_profile_dsn_never_in_manifest_or_tarball_bytes(
    home: Path, tmp_path: Path, _no_signing: None, fake_pg_dump: Path
) -> None:
    result = create_backup(
        tmp_path / "pg-set.tar.gz", home=home, profile="pg", dsn=FAKE_DSN
    )
    manifest_json = json.dumps(result.manifest.model_dump(mode="json"))
    assert FAKE_DSN not in manifest_json
    assert "sup3r-secret-pw" not in manifest_json
    assert "nova:" not in manifest_json

    # Scan every decompressed byte of the set: the DSN travels nowhere.
    blob = b""
    with tarfile.open(result.archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            fh = tar.extractfile(member)
            if fh is not None:
                blob += fh.read()
    assert FAKE_DSN.encode() not in blob
    assert b"sup3r-secret-pw" not in blob


def test_pg_profile_reads_dsn_from_nova_dsn_env(
    home: Path,
    tmp_path: Path,
    _no_signing: None,
    fake_pg_dump: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_DSN", FAKE_DSN)
    result = create_backup(tmp_path / "pg-set.tar.gz", home=home, profile="pg")
    assert result.manifest.db_target == "db.example.internal/novadb"


def test_pg_profile_without_dsn_fails_clearly(
    home: Path,
    tmp_path: Path,
    _no_signing: None,
    fake_pg_dump: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOVA_DSN", raising=False)
    monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)
    with pytest.raises(BackupCreateError, match="NOVA_DSN"):
        create_backup(tmp_path / "pg-set.tar.gz", home=home, profile="pg")


def test_pg_dump_binary_absent_named_error_with_install_hint(
    home: Path, tmp_path: Path, _no_signing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.backup.create import PgDumpNotFoundError

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(PgDumpNotFoundError, match="postgresql-client"):
        create_backup(tmp_path / "pg-set.tar.gz", home=home, profile="pg", dsn=FAKE_DSN)


def test_pg_dump_failure_scrubs_dsn_from_error(
    home: Path, tmp_path: Path, _no_signing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "fail-bin"
    bindir.mkdir()
    script = bindir / "pg_dump"
    script.write_text(
        '#!/bin/sh\necho "pg_dump: error: connection to \\"$5\\" failed" >&2\nexit 1\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    with pytest.raises(BackupCreateError) as excinfo:
        create_backup(tmp_path / "pg-set.tar.gz", home=home, profile="pg", dsn=FAKE_DSN)
    assert FAKE_DSN not in str(excinfo.value)
    assert "sup3r-secret-pw" not in str(excinfo.value)


def test_unknown_profile_rejected(home: Path, tmp_path: Path, _no_signing: None) -> None:
    with pytest.raises(BackupCreateError, match="Unknown backup profile"):
        create_backup(tmp_path / "set.tar.gz", home=home, profile="cloud")


@pytest.mark.skip(
    reason=(
        "Live-Postgres verification of the pg profile is infra-gated: pg_dump and "
        "a reachable Postgres are not available on this machine (ADR-0181 second "
        "slice ships with a fake-pg_dump contract test; run this against a real "
        "DSN when server infra exists)."
    )
)
def test_pg_profile_against_live_postgres() -> None:  # pragma: no cover
    raise NotImplementedError


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_backup_help() -> None:
    result = runner.invoke(app, ["backup", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output and "verify" in result.output


def test_cli_create_and_verify_round_trip(
    home: Path, tmp_path: Path, _no_signing: None
) -> None:
    out = tmp_path / "cli-set.tar.gz"
    created = runner.invoke(app, ["backup", "create", "-o", str(out), "--home", str(home)])
    assert created.exit_code == 0, created.output
    assert "Backup set written" in created.output
    assert "unsigned" in created.output  # honest CLI output

    verified = runner.invoke(app, ["backup", "verify", str(out)])
    assert verified.exit_code == 0, verified.output
    assert "verified" in verified.output


def test_cli_verify_exits_1_on_mismatch(
    home: Path, tmp_path: Path, _no_signing: None
) -> None:
    result = create_backup(tmp_path / "set.tar.gz", home=home)

    def flip(workdir: Path) -> None:
        (workdir / "registry.db").write_bytes(b"not a database")

    tampered = _tamper(result.archive_path, flip)
    verified = runner.invoke(app, ["backup", "verify", str(tampered)])
    assert verified.exit_code == 1
    assert "FAILED" in verified.output


def test_cli_verify_missing_set_exits_1(tmp_path: Path) -> None:
    verified = runner.invoke(app, ["backup", "verify", str(tmp_path / "nope.tar.gz")])
    assert verified.exit_code == 1
