"""Tests for src/novafabric/cli/doctor.py — nova doctor command."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage._base import StorageInfo

runner = CliRunner()


# ---------------------------------------------------------------------------
# nova doctor (no flags)
# ---------------------------------------------------------------------------


def test_doctor_no_flags_prints_hint() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--check-storage")


# ---------------------------------------------------------------------------
# nova doctor --check-storage (SQLite path)
# ---------------------------------------------------------------------------


def test_doctor_check_storage_sqlite_initialised(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, asset_type TEXT NOT NULL,
            version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'development',
            spec_json TEXT NOT NULL, git_commit_sha TEXT, created_at TEXT NOT NULL,
            promoted_at TEXT, promoted_by TEXT,
            forced_promotion INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE eval_results (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, suite_name TEXT NOT NULL,
            passed INTEGER NOT NULL, score_json TEXT, run_at TEXT NOT NULL
        );
        CREATE TABLE lineage_nodes (
            node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
            first_seen_capsule_run_id TEXT, payload TEXT NOT NULL
        );
        CREATE TABLE lineage_edges (
            edge_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL,
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            capsule_run_id TEXT NOT NULL, confidence TEXT,
            created_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "sqlite" in result.output
    assert "Schema version" in result.output
    assert "assets" in result.output


def test_doctor_check_storage_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "nonexistent.db"
    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "sqlite" in result.output


def test_doctor_check_storage_shows_row_counts(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, asset_type TEXT NOT NULL,
            version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'development',
            spec_json TEXT NOT NULL, git_commit_sha TEXT, created_at TEXT NOT NULL,
            promoted_at TEXT, promoted_by TEXT,
            forced_promotion INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO assets
            VALUES ('a1','m','llm','1','development','{}',NULL,'2026-01-01',NULL,NULL,0);
        INSERT INTO assets
            VALUES ('a2','n','llm','1','development','{}',NULL,'2026-01-01',NULL,NULL,0);
        """
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "assets" in result.output
    assert "2" in result.output  # 2 asset rows


def test_doctor_check_storage_migration_pending(tmp_path: Path) -> None:
    # ADR-0211: an unstamped (init_schema-bootstrapped) DB is "pending" —
    # the comparator, not a table-existence heuristic, decides now.
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1);"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "pending" in result.output
    # ADR-0211 D5: the hint must name the track that fixes THIS database.
    assert "--track registry" in result.output.replace("\n", "")


def test_doctor_check_storage_migration_up_to_date(tmp_path: Path) -> None:
    # ADR-0211: "up to date" now means stamped AT the script head — a stale
    # stamp (e.g. s0001 with a newer head) correctly reports pending instead.
    from novafabric.migrations.registry_track import script_head

    head = script_head("sqlite")
    assert head is not None, "source checkout must resolve the sqlite head"

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1); "
        "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY); "
        f"INSERT INTO alembic_version VALUES ('{head}');"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_doctor_check_storage_migration_stale_stamp_is_pending(
    tmp_path: Path,
) -> None:
    # A DB stamped behind head must report pending (the pre-ADR-0211 code
    # reported "up to date" for ANY stamp — the bug this slice fixes).
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1); "
        "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY); "
        "INSERT INTO alembic_version VALUES ('s0001');"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "pending" in result.output


def test_doctor_check_storage_sqlite_uninitialised_schema(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    # DB exists but has no tables at all
    conn = sqlite3.connect(str(db))
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "not initialised" in result.output


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_doctor_check_storage_backend_error(tmp_path: Path) -> None:
    with patch(
        "novafabric.cli.doctor.get_backend",
        side_effect=ValueError("bad config"),
    ):
        result = runner.invoke(app, ["doctor", "--check-storage"])
    assert result.exit_code == 1
    assert "bad config" in result.output


# ---------------------------------------------------------------------------
# _print_storage_report — branch coverage
# ---------------------------------------------------------------------------


def test_print_storage_report_postgres_no_db_path() -> None:
    from novafabric.cli.doctor import _print_storage_report

    info = StorageInfo(
        backend="postgres",
        db_path=None,
        schema_version=2,
        row_counts={"assets": 10},
        migration_pending=None,
    )
    # Just assert it doesn't raise; output goes to the module-level console
    _print_storage_report(info)


def test_print_storage_report_empty_rows_with_path(tmp_path: Path) -> None:
    from novafabric.cli.doctor import _print_storage_report

    info = StorageInfo(
        backend="sqlite",
        db_path=str(tmp_path / "x.db"),
        schema_version=None,
        row_counts={},
        migration_pending=None,
    )
    _print_storage_report(info)


# ---------------------------------------------------------------------------
# nova doctor --check-scheduler (OQ-06, PAR-ADR-003)
# ---------------------------------------------------------------------------

_SCHEDULER_ENV_VARS = [
    "SLURM_JOB_ID",
    "SLURM_JOBID",
    "SLURM_EXPORT_ENV",
    "TORCHELASTIC_RUN_ID",
    "OMPI_COMM_WORLD_RANK",
    "RAY_WORLD_SIZE",
    "KUBERNETES_SERVICE_HOST",
    "NOVAFABRIC_GLOBAL_RUN_ID",
]


@pytest.fixture(autouse=True)
def _clean_scheduler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SCHEDULER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_doctor_check_scheduler_no_scheduler_is_ok() -> None:
    result = runner.invoke(app, ["doctor", "--check-scheduler"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_doctor_check_scheduler_slurm_export_none_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("SLURM_EXPORT_ENV", "NONE")

    result = runner.invoke(app, ["doctor", "--check-scheduler"])

    assert result.exit_code == 1
    assert "Mismatch detected" in result.output
    assert "SLURM_EXPORT_ENV" in result.output


def test_doctor_check_scheduler_with_contract_vars_present_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", "01ARZ3NDEKTSV4RRFFQ69G5FAV")

    result = runner.invoke(app, ["doctor", "--check-scheduler"])

    assert result.exit_code == 0
    assert "OK" in result.output


def test_doctor_no_flags_mentions_check_scheduler() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--check-scheduler")


# ---------------------------------------------------------------------------
# nova doctor --check-extras
# ---------------------------------------------------------------------------


def test_doctor_check_extras_lists_extras_and_stays_zero() -> None:
    """Diagnostic, not a gate.

    Most installs deliberately omit most of the 32 extras, so a non-zero exit
    would make `nova doctor` red for almost every user.
    """
    result = runner.invoke(app, ["doctor", "--check-extras"])

    assert result.exit_code == 0
    assert "Optional extras" in result.stdout
    assert "serve" in result.stdout


def test_doctor_check_extras_names_the_install_command_with_the_extra_intact() -> None:
    """The bug this exists to prevent: Rich stripping ``[serve]`` from the hint.

    A rendered ``pip install 'novafabric'`` is not a fix — it reinstalls the base
    package and changes nothing.
    """
    with patch("novafabric.cli.doctor.missing_requirements", return_value=["fastapi"]):
        result = runner.invoke(app, ["doctor", "--check-extras"])

    assert result.exit_code == 0
    assert "incomplete" in result.stdout
    assert "novafabric[" in result.stdout, "the extra name was stripped from the hint"
    assert "pip install 'novafabric'\n" not in result.stdout


def test_doctor_check_extras_reports_all_complete_when_nothing_is_missing() -> None:
    with patch("novafabric.cli.doctor.missing_requirements", return_value=[]):
        result = runner.invoke(app, ["doctor", "--check-extras"])

    assert result.exit_code == 0
    assert "All declared extras are fully installed" in result.stdout


def test_doctor_check_extras_explains_an_uninstalled_source_checkout() -> None:
    """No metadata is a situation to report, not a crash."""
    with patch("novafabric.cli.doctor.declared_extras", return_value=[]):
        result = runner.invoke(app, ["doctor", "--check-extras"])

    assert result.exit_code == 0
    assert "source checkout" in result.stdout


def test_doctor_no_flags_hint_mentions_check_extras() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert_flag_in_help(result, "--check-extras")


class TestLegacyCleartextTokenReport:
    """`nova doctor` must surface pre-ADR-0252 tokens whose secret is on disk.

    ADR-0252 stopped writing the secret itself, but it could not rewrite records
    already written. ``token_store.legacy_plaintext_count()`` was added to count
    them and its docstring named ``nova doctor`` as the consumer — and nothing
    called it, so the number existed and no operator could ever see it. A
    migration that stays silent about what it could not migrate is not finished.
    """

    @staticmethod
    def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: list) -> Path:
        store = tmp_path / "tokens.jsonl"
        store.write_text("".join(json.dumps(r) + "\n" for r in records))
        monkeypatch.setattr("novafabric.serve.token_store.tokens_path", lambda: store)
        return store

    def test_cleartext_record_is_reported_and_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(
            tmp_path,
            monkeypatch,
            [{"label": "old", "token": "s3cret-in-the-clear", "fingerprint": "aa"}],
        )
        result = runner.invoke(app, ["doctor", "--check-tokens"])
        assert result.exit_code == 1, result.output
        assert "cleartext" in result.output
        assert "1 token record" in result.output
        assert "s3cret-in-the-clear" not in result.output, (
            "the report must count the secret, never print it"
        )

    def test_fingerprint_only_records_are_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(tmp_path, monkeypatch, [{"label": "new", "fingerprint": "bb"}])
        result = runner.invoke(app, ["doctor", "--check-tokens"])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_other_checks_warn_without_changing_their_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning surfaces unasked; it must not retroactively fail a run."""
        self._seed(
            tmp_path, monkeypatch, [{"label": "old", "token": "x", "fingerprint": "aa"}]
        )
        result = runner.invoke(app, ["doctor", "--check-extras"])
        assert "cleartext" in result.output
        assert result.exit_code == 0, result.output

    def test_flag_is_documented_in_help(self) -> None:
        result = runner.invoke(app, ["doctor", "--help"])
        assert_flag_in_help(result, "--check-tokens")


def test_every_nova_command_doctor_prints_actually_exists() -> None:
    """A remediation hint naming a command that does not exist is worse than none.

    `nova doctor` is what an operator runs when something is already wrong, and
    its remediation lines are copied and pasted verbatim. A hint like
    ``nova serve token list`` for a subcommand that was never built sends that
    operator to a "No such command" error while the real problem stands.

    This guard exists because exactly that shipped: the cleartext-token
    remediation invented ``nova serve token list``/``token revoke``, which the
    CLI has never had — token issue and revoke live on the dashboard's
    ``/api/admin/tokens`` routes instead. Checking the string against the real
    command tree is the only thing that catches it, because a hint is plain text
    that no import, type check or route test ever exercises.
    """
    import ast as _ast
    import re as _re
    from pathlib import Path as _Path

    from novafabric.cli.introspect import command_paths

    source = _Path("src/novafabric/cli/doctor.py").read_text()
    literals = [
        node.value
        for node in _ast.walk(_ast.parse(source))
        if isinstance(node, _ast.Constant) and isinstance(node.value, str)
    ]
    # `nova …` inside backticks — how every hint in this file spells a command.
    # Anchored on the CLOSING backtick. A lazy quantifier ending at whitespace
    # captures only "nova serve" out of `nova serve token list`, which silently
    # reduces this guard to "is the first word a command" — the first two
    # versions of this test passed the bad string for exactly that reason.
    pattern = _re.compile(r"`(nova [a-z0-9 <>\[\]_.-]+?)`")
    valid = command_paths(include_hidden=True)

    bad: list[str] = []
    for text in literals:
        for raw in pattern.findall(text):
            words = raw.split()
            # Longest prefix that is a real command path. Trimming until
            # *something* matches would pass anything starting with a valid
            # command — "nova serve token list" would reduce to "nova serve" and
            # sail through, which is how the bug this guards against survived a
            # first version of this very test.
            best = 0
            for i in range(len(words), 0, -1):
                if " ".join(words[:i]) in valid:
                    best = i
                    break
            leftover = words[best:]
            # Options and <placeholders> after a real command are fine; a bare
            # word is a subcommand being claimed, and it does not exist.
            if best == 0 or any(
                not w.startswith(("-", "<", "[")) for w in leftover
            ):
                bad.append(" ".join(words))

    assert not bad, (
        "nova doctor prints these commands, and the CLI has no such command: "
        + "; ".join(sorted(set(bad)))
    )
