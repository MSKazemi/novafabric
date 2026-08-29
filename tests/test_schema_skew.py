"""ADR-0211: schema-skew comparator + fail-closed startup guard (unit tier).

Covers: all five comparator states against tmp SQLite DBs (real checkout
script trees) and stub script dirs; the guard decision table x the
NOVAFABRIC_ALLOW_SCHEMA_SKEW values; the E-SKEW-BEHIND / E-SKEW-AHEAD message
contracts (fields + remediation strings); the readyz mapping; the packaged
registry-track resolution helpers; and the server-lifespan integration
(a behind DB refuses startup and mutates nothing; the override starts).

Postgres-side comparator behavior is exercised in the testcontainers tier
(tests/metadata_store/test_pg_restore_roundtrip.py).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from novafabric.migrations import registry_track
from novafabric.server.schema_skew import (
    ALLOW_SCHEMA_SKEW_ENV,
    SchemaSkewError,
    compare_revisions,
    enforce_schema_skew_guard,
)

SQLITE_HEAD = registry_track.script_head("sqlite")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamped_db(tmp_path: Path, revision: str | None) -> Path:
    """A tmp SQLite DB with the given alembic stamp (None = table absent)."""
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    if revision is not None:
        conn.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def head() -> str:
    assert SQLITE_HEAD is not None, "checkout must resolve the sqlite head"
    return SQLITE_HEAD


# ---------------------------------------------------------------------------
# Comparator: the five states
# ---------------------------------------------------------------------------


class TestCompareRevisions:
    def test_ok_at_head(self, tmp_path: Path, head: str) -> None:
        report = compare_revisions("sqlite", _stamped_db(tmp_path, head))
        assert report.status == "ok"
        assert report.db_revision == head
        assert report.head_revision == head

    def test_behind_on_ancestor_stamp(self, tmp_path: Path, head: str) -> None:
        report = compare_revisions("sqlite", _stamped_db(tmp_path, "s0001"))
        assert report.status == "behind"
        assert report.db_revision == "s0001"
        assert report.head_revision == head

    def test_ahead_or_foreign_on_unknown_stamp(self, tmp_path: Path) -> None:
        report = compare_revisions("sqlite", _stamped_db(tmp_path, "zzz9999"))
        assert report.status == "ahead_or_foreign"
        assert report.db_revision == "zzz9999"

    def test_unstamped_when_table_absent(self, tmp_path: Path) -> None:
        report = compare_revisions("sqlite", _stamped_db(tmp_path, None))
        assert report.status == "unstamped"
        assert report.db_revision is None

    def test_unstamped_when_table_empty(self, tmp_path: Path) -> None:
        db = _stamped_db(tmp_path, None)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
        conn.commit()
        conn.close()
        assert compare_revisions("sqlite", db).status == "unstamped"

    def test_unstamped_on_missing_file(self, tmp_path: Path) -> None:
        # Fresh install about to be bootstrapped — start-with-warning state.
        report = compare_revisions("sqlite", tmp_path / "nope.db")
        assert report.status == "unstamped"

    def test_unknown_on_unreadable_db(self, tmp_path: Path) -> None:
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"this is not a sqlite database at all\x00\x01")
        assert compare_revisions("sqlite", garbage).status == "unknown"

    def test_unknown_when_head_unresolvable(self, tmp_path: Path, head: str) -> None:
        # Stub script dir with no scripts: head is honestly unknown.
        stub = tmp_path / "stub-scripts"
        (stub / "sqlite" / "versions").mkdir(parents=True)
        (stub / "env.py").write_text("")
        (stub / "script.py.mako").write_text("")
        db = _stamped_db(tmp_path, head)
        report = compare_revisions("sqlite", db, script_dir=stub)
        assert report.status == "unknown"
        assert report.head_revision is None

    def test_unknown_on_missing_target(self) -> None:
        assert compare_revisions("sqlite", None).status == "unknown"
        assert compare_revisions("postgres", "").status == "unknown"

    def test_unknown_on_foreign_backend_or_track(self, tmp_path: Path) -> None:
        db = _stamped_db(tmp_path, None)
        assert compare_revisions("oracle", db).status == "unknown"
        assert compare_revisions("sqlite", db, track="metadata").status == "unknown"

    def test_postgres_unknown_when_unreachable(self) -> None:
        # No server behind this DSN; comparator must degrade, not raise/hang.
        report = compare_revisions(
            "postgres", "postgresql://nobody@127.0.0.1:1/nova"
        )
        assert report.status == "unknown"

    def test_report_never_contains_target(self, tmp_path: Path) -> None:
        db = _stamped_db(tmp_path, "s0001")
        report = compare_revisions("sqlite", db)
        for value in (report.detail, report.remediation):
            assert str(db) not in value


# ---------------------------------------------------------------------------
# Guard decision table (state x ALLOW)
# ---------------------------------------------------------------------------


class TestGuardDecisionTable:
    def test_ok_starts(self, tmp_path: Path, head: str) -> None:
        report = enforce_schema_skew_guard(
            backend="sqlite", target=_stamped_db(tmp_path, head)
        )
        assert report.status == "ok"

    def test_behind_refuses_by_default(self, tmp_path: Path, head: str) -> None:
        with pytest.raises(SchemaSkewError) as excinfo:
            enforce_schema_skew_guard(
                backend="sqlite", target=_stamped_db(tmp_path, "s0001")
            )
        message = str(excinfo.value)
        # E-SKEW-BEHIND contract: both revisions + the exact remediation.
        assert "s0001" in message
        assert head in message
        assert "Refusing to start" in message
        assert "nova db upgrade --track registry --backend sqlite" in message
        assert f"{ALLOW_SCHEMA_SKEW_ENV}=1" in message
        assert excinfo.value.report.status == "behind"

    def test_ahead_refuses_naming_package_upgrade(
        self, tmp_path: Path, head: str
    ) -> None:
        with pytest.raises(SchemaSkewError) as excinfo:
            enforce_schema_skew_guard(
                backend="sqlite", target=_stamped_db(tmp_path, "zzz9999")
            )
        message = str(excinfo.value)
        # E-SKEW-AHEAD contract: foreign revision, head, upgrade-the-package.
        assert "zzz9999" in message
        assert head in message
        assert "newer or different NovaFabric" in message
        assert "upgrade the novafabric package" in message
        assert "do NOT downgrade the schema" in message

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
    def test_allow_env_downgrades_behind_to_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        value: str,
    ) -> None:
        monkeypatch.setenv(ALLOW_SCHEMA_SKEW_ENV, value)
        with caplog.at_level(logging.WARNING, logger="novafabric.server.schema_skew"):
            report = enforce_schema_skew_guard(
                backend="sqlite", target=_stamped_db(tmp_path, "s0001")
            )
        assert report.status == "behind"
        records = [
            r for r in caplog.records
            if getattr(r, "event", "") == "schema_skew_overridden"
        ]
        assert len(records) == 1  # exactly one structured record per startup
        record = records[0]
        # W-SKEW-* carries byte-identical structured fields.
        assert record.status == "behind"  # type: ignore[attr-defined]
        assert record.backend == "sqlite"  # type: ignore[attr-defined]
        assert record.db_revision == "s0001"  # type: ignore[attr-defined]
        assert record.head_revision == SQLITE_HEAD  # type: ignore[attr-defined]
        assert "--track registry" in record.remediation  # type: ignore[attr-defined]

    def test_allow_env_downgrades_ahead_to_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ALLOW_SCHEMA_SKEW_ENV, "1")
        report = enforce_schema_skew_guard(
            backend="sqlite", target=_stamped_db(tmp_path, "zzz9999")
        )
        assert report.status == "ahead_or_foreign"

    @pytest.mark.parametrize("value", ["0", "", "no", "false", "2", "on"])
    def test_non_allow_values_still_refuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(ALLOW_SCHEMA_SKEW_ENV, value)
        with pytest.raises(SchemaSkewError):
            enforce_schema_skew_guard(
                backend="sqlite", target=_stamped_db(tmp_path, "s0001")
            )

    def test_unstamped_starts_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.server.schema_skew"):
            report = enforce_schema_skew_guard(
                backend="sqlite", target=_stamped_db(tmp_path, None)
            )
        assert report.status == "unstamped"
        assert any("nova doctor --check-storage" in r.getMessage() for r in caplog.records)

    def test_unknown_starts_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"not sqlite\x00")
        with caplog.at_level(logging.WARNING, logger="novafabric.server.schema_skew"):
            report = enforce_schema_skew_guard(backend="sqlite", target=garbage)
        assert report.status == "unknown"

    def test_messages_never_contain_target(self, tmp_path: Path) -> None:
        db = _stamped_db(tmp_path, "s0001")
        with pytest.raises(SchemaSkewError) as excinfo:
            enforce_schema_skew_guard(backend="sqlite", target=db)
        assert str(db) not in str(excinfo.value)
        assert str(tmp_path) not in str(excinfo.value)


# ---------------------------------------------------------------------------
# readyz mapping (observability refactor, ADR-0211 D1)
# ---------------------------------------------------------------------------


class TestReadyzMapping:
    def test_check_migrations_sqlite_states(self, tmp_path: Path, head: str) -> None:
        from novafabric.server.observability import check_migrations_sqlite

        assert check_migrations_sqlite(_stamped_db(tmp_path, head)) == "ok"

    def test_behind_is_fail(self, tmp_path: Path) -> None:
        from novafabric.server.observability import check_migrations_sqlite

        assert check_migrations_sqlite(_stamped_db(tmp_path, "s0001")) == "fail"

    def test_ahead_is_fail(self, tmp_path: Path) -> None:
        from novafabric.server.observability import check_migrations_sqlite

        assert check_migrations_sqlite(_stamped_db(tmp_path, "zzz")) == "fail"

    def test_unstamped_is_unknown(self, tmp_path: Path) -> None:
        from novafabric.server.observability import check_migrations_sqlite

        assert check_migrations_sqlite(_stamped_db(tmp_path, None)) == "unknown"

    def test_postgres_no_dsn_is_unknown(self) -> None:
        from novafabric.server.observability import check_migrations_postgres

        assert check_migrations_postgres(None) == "unknown"
        assert check_migrations_postgres("") == "unknown"

    def test_postgres_unreachable_is_unknown(self) -> None:
        from novafabric.server.observability import check_migrations_postgres

        assert (
            check_migrations_postgres("postgresql://nobody@127.0.0.1:1/nova")
            == "unknown"
        )


# ---------------------------------------------------------------------------
# Registry-track resolution helpers (ADR-0211 D2/D5)
# ---------------------------------------------------------------------------


class TestRegistryTrack:
    def test_checkout_resolution_finds_repo_tree(self) -> None:
        script_dir = registry_track.resolve_script_dir()
        assert script_dir is not None
        assert (script_dir / "env.py").is_file()
        assert (script_dir / "sqlite" / "versions").is_dir()
        assert (script_dir / "postgres" / "versions").is_dir()

    def test_heads_resolve_for_both_backends(self) -> None:
        assert registry_track.script_head("sqlite") is not None
        assert registry_track.script_head("postgres") is not None

    def test_unknown_backend_refused(self) -> None:
        with pytest.raises(registry_track.RegistryMigrationsUnavailableError):
            registry_track.registry_alembic_config("oracle")

    def test_upgrade_registry_to_head_sqlite(self, tmp_path: Path, head: str) -> None:
        # The programmatic engine behind `nova db upgrade --track registry`
        # and pg-restore step 3: an empty DB migrates to head and stamps.
        db = tmp_path / "fresh-registry.db"
        result_head = registry_track.upgrade_registry_to_head(
            "sqlite", f"sqlite:///{db}"
        )
        assert result_head == head
        conn = sqlite3.connect(db)
        try:
            stamp = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        finally:
            conn.close()
        assert stamp == head
        # And the comparator agrees the migrated DB is at head.
        assert compare_revisions("sqlite", db).status == "ok"

    def test_head_cache_cleared(self) -> None:
        registry_track.clear_head_cache()
        assert registry_track.script_head("sqlite") == SQLITE_HEAD


# ---------------------------------------------------------------------------
# Server lifespan integration (guard runs BEFORE init_schema)
# ---------------------------------------------------------------------------


class TestLifespanIntegration:
    def test_behind_db_refuses_startup_and_mutates_nothing(
        self, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        from novafabric.server.app import create_app
        from novafabric.server.config import ServerConfig

        db = _stamped_db(tmp_path, "s0001")
        before = db.read_bytes()
        app = create_app(ServerConfig(db_path=str(db), insecure_no_auth=True))
        with pytest.raises(SchemaSkewError):
            with TestClient(app):
                pass  # pragma: no cover - startup must fail
        assert db.read_bytes() == before  # refused server mutated nothing

    def test_behind_db_starts_with_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from novafabric.server.app import create_app
        from novafabric.server.config import ServerConfig

        monkeypatch.setenv(ALLOW_SCHEMA_SKEW_ENV, "1")
        db = _stamped_db(tmp_path, "s0001")
        app = create_app(ServerConfig(db_path=str(db), insecure_no_auth=True))
        with TestClient(app) as client:
            assert client.get("/livez").status_code == 200

    def test_fresh_unstamped_db_starts(self, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from novafabric.server.app import create_app
        from novafabric.server.config import ServerConfig

        app = create_app(
            ServerConfig(db_path=str(tmp_path / "new.db"), insecure_no_auth=True)
        )
        with TestClient(app) as client:
            assert client.get("/livez").status_code == 200


# ---------------------------------------------------------------------------
# Registry-track resolution edge branches
# ---------------------------------------------------------------------------


class TestRegistryTrackResolution:
    def test_no_script_dir_anywhere_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(registry_track, "resolve_script_dir", lambda: None)
        with pytest.raises(registry_track.RegistryMigrationsUnavailableError):
            registry_track.registry_alembic_config("sqlite")

    def test_packaged_dir_wins_over_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate an installed wheel: novafabric/migrations/registry/ exists
        # next to the module — it must be preferred (ADR-0211 D2).
        fake_pkg = tmp_path / "site-packages" / "novafabric" / "migrations"
        registry = fake_pkg / "registry"
        registry.mkdir(parents=True)
        (registry / "env.py").write_text("")
        monkeypatch.setattr(
            registry_track, "__file__", str(fake_pkg / "registry_track.py")
        )
        assert registry_track.packaged_script_dir() == registry
        assert registry_track.resolve_script_dir() == registry
        # And with no alembic.ini in any parent, the checkout fallback is None.
        assert registry_track.checkout_script_dir() is None

    def test_script_head_unresolvable_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            registry_track.script_head("sqlite", script_dir=tmp_path / "nope")
            is None
        )

    def test_head_ancestry_unresolvable_is_none(self, tmp_path: Path) -> None:
        assert (
            registry_track.head_ancestry("sqlite", script_dir=tmp_path / "nope")
            is None
        )

    def test_sqlalchemy_url_normalization(self) -> None:
        assert registry_track._sqlalchemy_url(
            "postgresql://u:p@h/db"
        ) == "postgresql+psycopg://u:p@h/db"
        assert registry_track._sqlalchemy_url(
            "postgres://u:p@h/db"
        ) == "postgresql+psycopg://u:p@h/db"
        assert registry_track._sqlalchemy_url(
            "postgresql+psycopg://u@h/db"
        ) == "postgresql+psycopg://u@h/db"
        assert registry_track._sqlalchemy_url("sqlite:///x.db") == "sqlite:///x.db"


# ---------------------------------------------------------------------------
# B14 (campaign 3): an empty Postgres backend must not start silently
# ---------------------------------------------------------------------------
#
# On a 314-VM fleet the server was started with `--backend postgres` against a
# database with zero tables (its migration had died — see B13). The guard
# reported `unstamped`, warned once, and started; the server then returned
# HTTP 201 for all 41,774 uploads, wrote no metadata rows, and logged no error.
# The capsules were durable and verify — the failure is that the server's own
# report of what it did was false for a whole plane.


class _EmptyBackendHarness:
    """Force `unstamped` and control the table count the guard sees."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, table_count: int | None):
        from novafabric.server import schema_skew as ss

        monkeypatch.setattr(
            ss,
            "compare_revisions",
            lambda *a, **kw: ss.SkewReport(
                status=ss.STATUS_UNSTAMPED,
                backend="postgres",
                db_revision=None,
                head_revision="abc123",
                detail="no alembic_version stamp",
                track="registry",
            ),
        )
        monkeypatch.setattr(
            ss, "postgres_application_tables", lambda _dsn: table_count
        )
        self.ss = ss


_DSN = "postgresql://u:secret@db.internal:5432/nova"


def test_empty_postgres_backend_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _EmptyBackendHarness(monkeypatch, table_count=0)
    monkeypatch.delenv(h.ss.ALLOW_SCHEMA_SKEW_ENV, raising=False)
    with pytest.raises(h.ss.SchemaSkewError) as exc:
        h.ss.enforce_schema_skew_guard(backend="postgres", target=_DSN)
    assert "no tables" in str(exc.value)
    assert "nova db upgrade" in str(exc.value)


def test_empty_backend_refusal_never_echoes_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _EmptyBackendHarness(monkeypatch, table_count=0)
    monkeypatch.delenv(h.ss.ALLOW_SCHEMA_SKEW_ENV, raising=False)
    with pytest.raises(h.ss.SchemaSkewError) as exc:
        h.ss.enforce_schema_skew_guard(backend="postgres", target=_DSN)
    message = str(exc.value)
    assert "secret" not in message
    assert "db.internal" not in message
    assert _DSN not in message


def test_populated_but_unstamped_postgres_still_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy init_schema()-bootstrapped case must keep working."""
    h = _EmptyBackendHarness(monkeypatch, table_count=17)
    monkeypatch.delenv(h.ss.ALLOW_SCHEMA_SKEW_ENV, raising=False)
    report = h.ss.enforce_schema_skew_guard(backend="postgres", target=_DSN)
    assert report.status == h.ss.STATUS_UNSTAMPED


def test_unreadable_backend_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """None means 'could not tell' — never refuse on ignorance (ADR-0182)."""
    h = _EmptyBackendHarness(monkeypatch, table_count=None)
    monkeypatch.delenv(h.ss.ALLOW_SCHEMA_SKEW_ENV, raising=False)
    report = h.ss.enforce_schema_skew_guard(backend="postgres", target=_DSN)
    assert report.status == h.ss.STATUS_UNSTAMPED


def test_empty_backend_refusal_is_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _EmptyBackendHarness(monkeypatch, table_count=0)
    monkeypatch.setenv(h.ss.ALLOW_SCHEMA_SKEW_ENV, "1")
    report = h.ss.enforce_schema_skew_guard(backend="postgres", target=_DSN)
    assert report.status == h.ss.STATUS_UNSTAMPED


def test_sqlite_local_mode_is_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local mode must never acquire a Postgres-shaped startup requirement."""
    from novafabric.server import schema_skew as ss

    monkeypatch.setattr(
        ss,
        "compare_revisions",
        lambda *a, **kw: ss.SkewReport(
            status=ss.STATUS_UNSTAMPED,
            backend="sqlite",
            db_revision=None,
            head_revision="abc123",
            detail="no alembic_version stamp",
            track="registry",
        ),
    )

    def _must_not_run(_dsn: str) -> int:
        raise AssertionError("the empty-backend probe must not run for sqlite")

    monkeypatch.setattr(ss, "postgres_application_tables", _must_not_run)
    monkeypatch.delenv(ss.ALLOW_SCHEMA_SKEW_ENV, raising=False)
    report = ss.enforce_schema_skew_guard(
        backend="sqlite", target=tmp_path / "registry.db"
    )
    assert report.status == ss.STATUS_UNSTAMPED
