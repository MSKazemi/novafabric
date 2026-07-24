"""ADR-0217: automated pg-dump restore — unit tier (fake binaries, fake psycopg).

The real-Postgres cycle lives in tests/metadata_store/test_backup_pg_restore.py
(testcontainers). Here: pre-flight refusal semantics, safety dump, pg_restore
argv contract, DSN hygiene in failures, missing-binary error, and the honest
count-skip for sets that predate recorded counts.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from novafabric.backup.create import create_backup
from novafabric.backup.models import BackupManifest
from novafabric.backup.restore import (
    PgRestoreNotFoundError,
    RestoreError,
    _verify_pg_counts,
    restore_backup,
)

FAKE_DSN = "postgresql://nova:sup3r-secret-pw@db.example.internal:5432/novadb"


@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    from novafabric.registry.store import get_connection, init_schema

    home = tmp_path / "pg-home"
    home.mkdir()
    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.close()
    return home


@pytest.fixture()
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake pg_dump + pg_restore on PATH; pg_restore records its argv."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    dump = bindir / "pg_dump"
    dump.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "pg_dump (PostgreSQL) 16.3"; exit 0; fi\n'
        'out=""\nprev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--file" ]; then out="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'printf "PGDMP-fake-custom-format" > "$out"\n'
    )
    dump.chmod(0o755)
    restore = bindir / "pg_restore"
    restore.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "pg_restore (PostgreSQL) 16.3"; exit 0; fi\n'
        f'echo "$@" > {bindir}/pg_restore.argv\n'
    )
    restore.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return bindir


def _fake_psycopg(monkeypatch: pytest.MonkeyPatch, table_count: int) -> None:
    """Install a fake psycopg whose connect() reports *table_count* tables."""

    class FakeError(Exception):
        pass

    class FakeResult:
        def __init__(self, value: int) -> None:
            self._value = value

        def fetchone(self) -> tuple[int]:
            return (self._value,)

    class FakeConn:
        def execute(self, query: str, *args: object) -> FakeResult:
            return FakeResult(table_count)

        def __enter__(self) -> "FakeConn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn: FakeConn()  # type: ignore[attr-defined]
    fake.Error = FakeError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)


@pytest.fixture()
def pg_set(home: Path, tmp_path: Path, _no_signing: None, fake_bin: Path) -> Path:
    return create_backup(
        tmp_path / "pg-set.tar.gz", home=home, profile="pg", dsn=FAKE_DSN
    ).archive_path


def _stub_pg_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the live-DB verification steps (exercised in the container tier)."""
    from novafabric.backup.models import RestoreStepResult

    monkeypatch.setattr(
        "novafabric.backup.restore._run_pg_migrations",
        lambda dsn: RestoreStepResult(name="pg-migrations", ok=True, detail="stub"),
    )
    monkeypatch.setattr(
        "novafabric.backup.restore._verify_pg_counts",
        lambda dsn, manifest: RestoreStepResult(
            name="verify-db-counts", ok=True, detail="stub"
        ),
    )
    monkeypatch.setattr(
        "novafabric.backup.restore._verify_pg_rls",
        lambda dsn: RestoreStepResult(name="verify-rls", ok=True, detail="stub"),
    )


def test_pg_restore_flow_argv_and_dump_never_lands_in_home(
    pg_set: Path, fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_psycopg(monkeypatch, table_count=0)
    _stub_pg_verifies(monkeypatch)
    target = tmp_path / "target-home"
    out = restore_backup(pg_set, home=target, dsn=FAKE_DSN)
    assert out.ok is True, [s for s in out.steps if not s.ok]

    argv = (fake_bin / "pg_restore.argv").read_text()
    for flag in (
        "--clean",
        "--if-exists",
        "--single-transaction",
        "--no-owner",
        "--no-privileges",
        "--dbname",
    ):
        assert flag in argv
    assert not (target / "db.pgdump").exists()  # DB member never lands in home
    names = [s.name for s in out.steps]
    assert "check-target-db" in names and "pg-restore" in names
    assert names.index("pg-restore") < names.index("migrations")
    # The DSN appears nowhere in any step detail.
    assert all(FAKE_DSN not in s.detail for s in out.steps)


def test_refuse_non_empty_target_without_force(
    pg_set: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_psycopg(monkeypatch, table_count=7)
    target = tmp_path / "target-home"
    with pytest.raises(RestoreError, match="not empty"):
        restore_backup(pg_set, home=target, dsn=FAKE_DSN)
    assert not target.exists()  # nothing touched


def test_force_takes_safety_dump_first(
    pg_set: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_psycopg(monkeypatch, table_count=7)
    _stub_pg_verifies(monkeypatch)
    target = tmp_path / "target-home"
    out = restore_backup(pg_set, home=target, dsn=FAKE_DSN, force=True)
    assert out.ok is True, [s for s in out.steps if not s.ok]
    step = next(s for s in out.steps if s.name == "safety-dump")
    assert step.ok
    assert out.moved_aside is not None
    dump = Path(out.moved_aside) / "db.pre-restore.pgdump"
    assert dump.read_bytes() == b"PGDMP-fake-custom-format"


def test_pg_restore_failure_scrubs_dsn(
    pg_set: Path, fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_bin / "pg_restore").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "pg_restore (PostgreSQL) 16.3"; exit 0; fi\n'
        f'echo "connection to {FAKE_DSN} failed" >&2\n'
        "exit 1\n"
    )
    (fake_bin / "pg_restore").chmod(0o755)
    _fake_psycopg(monkeypatch, table_count=0)
    _stub_pg_verifies(monkeypatch)
    target = tmp_path / "target-home"
    out = restore_backup(pg_set, home=target, dsn=FAKE_DSN)
    assert out.ok is False
    step = next(s for s in out.steps if s.name == "pg-restore")
    assert step.ok is False
    assert FAKE_DSN not in step.detail
    assert "<dsn redacted>" in step.detail
    assert "rolled back" in step.detail  # single-transaction failure semantics


def test_missing_pg_restore_binary_is_a_named_error(
    pg_set: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_psycopg(monkeypatch, table_count=0)
    _stub_pg_verifies(monkeypatch)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(PgRestoreNotFoundError, match="postgresql-client"):
        restore_backup(pg_set, home=tmp_path / "target-home", dsn=FAKE_DSN)


def test_old_set_without_counts_skips_honestly() -> None:
    manifest = BackupManifest(
        set_id="01OLD",
        created_at="2026-07-16T00:00:00+00:00",
        profile="pg-dump",
        nova_version="0.63.0",
        members=[],
    )
    step = _verify_pg_counts(FAKE_DSN, manifest)
    assert step.ok is True
    assert "skipped" in step.detail
