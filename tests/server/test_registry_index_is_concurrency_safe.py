"""The registry index is written by every worker, so it opens via the helper.

Defect **B8**. `nova server start --workers N` refuses unless `--backend
postgres`, and said so like this:

    --workers N requires --backend postgres; the SQLite backend cannot be
    shared safely across worker processes.

which reads as *postgres ⇒ no shared SQLite*. That is false.
`server/capsule_index.py::open_index` writes `registry.db` from **every** worker
on **every** upload, whatever `--backend` says. Choosing postgres does not remove
the shared SQLite writer — it unlocks the multi-worker mode that makes it shared.

**What the fix is, honestly.** `open_index` used a bare `sqlite3.connect()`,
routing around this project's `_sqlite_util`. Measured, that is *observably
identical* today: Python's `sqlite3.connect` already applies a 5 s busy timeout,
so both report `busy_timeout=5000, journal_mode=delete`. Campaign-2 drove 1,984
uploads at 64-way concurrency against 4 workers with zero losses and zero lock
errors for exactly that reason.

So routing through the helper changes no behaviour now. It is here for
**inheritance** — a bare connect would not pick up future hardening of
`_sqlite_util`, which is the gap B8 named. The substantive half of this fix is
the guard message, which asserted something untrue.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from novafabric._sqlite_util import DEFAULT_BUSY_TIMEOUT_MS
from novafabric.cli.main import app
from novafabric.server.capsule_index import open_index

runner = CliRunner()


class TestOpenIndexGoesThroughTheHelper:
    def test_it_calls_connect_sqlite(self, tmp_path: Path) -> None:
        """The property that actually matters, asserted directly.

        Checking `PRAGMA busy_timeout` would NOT discriminate — a bare connect
        reports the same 5000. Assert the call, not a pragma both paths share.
        """
        import novafabric.server.capsule_index as mod

        with patch.object(
            mod, "connect_sqlite", wraps=mod.connect_sqlite
        ) as spy:
            conn = open_index(tmp_path / "registry.db")
            conn.close()
        assert spy.called, (
            "B8: open_index routed around _sqlite_util, so it cannot inherit "
            "any hardening applied there"
        )

    def test_journal_mode_is_deliberately_untouched(self, tmp_path: Path) -> None:
        """Do NOT 'improve' this into `ensure_wal`.

        `PRAGMA journal_mode=WAL` is itself a write needing a brief exclusive
        lock, and it ignores `busy_timeout` — so setting it during a concurrent
        open is the very race this connection must survive. The helper splits
        the two for that reason; this asserts the split holds here.
        """
        conn = open_index(tmp_path / "registry.db")
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() != "wal"

    def test_the_busy_timeout_is_present_whatever_supplies_it(
        self, tmp_path: Path
    ) -> None:
        """Recorded because it is load-bearing for the measured result, even
        though it does not discriminate between the two open paths."""
        conn = open_index(tmp_path / "registry.db")
        try:
            assert (
                conn.execute("PRAGMA busy_timeout").fetchone()[0]
                == DEFAULT_BUSY_TIMEOUT_MS
            )
        finally:
            conn.close()

    def test_the_index_still_works(self, tmp_path: Path) -> None:
        conn = open_index(tmp_path / "registry.db")
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "runs_cache" in names


class TestTheWorkersGuardDoesNotClaimSomethingFalse:
    def test_the_emitted_message_names_the_metadata_store(self) -> None:
        """Assert the message a user actually sees.

        An earlier version of this test grepped the module source, and matched
        the *comment* that quotes the old wording — passing/failing on prose
        rather than behaviour. Invoke the command instead.
        """
        result = runner.invoke(
            app, ["server", "start", "--workers", "4", "--backend", "sqlite"]
        )
        assert result.exit_code == 2, result.output
        assert "metadata store cannot be shared" in result.output, (
            f"the guard must name what it refuses: {result.output}"
        )
        assert "the SQLite backend cannot be shared safely" not in result.output, (
            "B8: that wording says postgres ⇒ no shared SQLite, but registry.db "
            "is written by every worker under either backend"
        )
        assert "registry.db" in result.output, (
            "the operator should be told the registry index is shared regardless"
        )
