"""Every store that wants WAL must survive a concurrent cold start (T31).

``PRAGMA journal_mode=WAL`` is a write that needs a brief exclusive lock, and
SQLite does not honour ``busy_timeout`` for it. Issuing it unconditionally on
each connect makes concurrent opens of one file raise ``database is locked``.

The window is the cold start, and that is measured rather than assumed. With
the unconditional pragma, eight threads opening one database, 500 trials per
arm: **25/500 trials failed against a database not yet in WAL, 0/500 against
one already in WAL.** Journal mode is a persistent property of the file, so
after the first connection establishes WAL the rest write nothing.

These tests race the real stores rather than a stand-in, because the defect
lives in how each store opens its own database. The race tests are necessarily
statistical; the deterministic tests at the bottom cover ``ensure_wal``'s retry
branches, which a healthy race never reaches.

Being statistical is not the same as being flaky. The anti-vacuity arm below
asserts that the race *does* reproduce, and at a ~5.7% per-trial rate a 60-trial
budget failed ~3% of runs — observed once here, failing and then passing on the
identical tree. That arm now stops at the first reproduction and may spend up to
``ANTI_VACUITY_TRIALS``, which makes a false failure ~7e-11 while making the
typical run faster. See the constants for the arithmetic.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from novafabric import _sqlite_util
from novafabric._sqlite_util import WAL_ATTEMPTS, ensure_wal

THREADS = 8

#: Trials the anti-vacuity arm may spend looking for one reproduction.
#:
#: The cold-start race reproduces on about **5.7% of trials** (measured on this
#: tree 2026-09-02: 17/300; the module docstring's original figure was 25/500 =
#: 5.0%). ``assert seen > 0`` over 60 trials therefore fails on ``0.943**60`` ≈
#: **3% of runs** — flaky by arithmetic, not by environment, and it was observed
#: failing once and passing on a re-run of the identical tree.
#:
#: At 400 trials that becomes ``0.943**400`` ≈ 7e-11. The budget costs nothing in
#: the normal case because the loop **stops at the first reproduction**: expected
#: trials to first hit is ~18, so a typical run is *faster* than the old
#: unconditional 60 (~0.6 s against ~1.9 s at 31 ms/trial). The full budget is
#: only ever spent when the race genuinely will not reproduce, which is the one
#: case worth spending time on.
ANTI_VACUITY_TRIALS = 400

#: Trials each "the fix holds" arm runs. Every trial must pass, so there is no
#: early exit and the cost is paid in full. At a 5.7% per-trial exposure, 60
#: trials hit the risky window ~3.4 times on average and would catch a fully
#: reverted fix with probability 1 - 0.943**60 ≈ 97%.
HOLDS_TRIALS = 60


def _open(db: Path, setup) -> sqlite3.Connection:
    """Open *db*, run *setup*, and close the connection if setup raises.

    Without this the failing arms hand a half-configured connection to the GC,
    which reports it as an unclosed-database ResourceWarning.
    """
    conn = sqlite3.connect(str(db), timeout=30.0)
    try:
        setup(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _race(make_conn) -> list[str]:
    """Open one database from THREADS threads at once; return the failures."""
    barrier = threading.Barrier(THREADS)
    failures: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=15)
            conn = make_conn()
            conn.close()
        except sqlite3.OperationalError as exc:
            with lock:
                failures.append(str(exc))
        except threading.BrokenBarrierError:  # pragma: no cover - timing only
            pass

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return failures


def test_the_unconditional_form_is_what_breaks(tmp_path: Path) -> None:
    """Anti-vacuity: the race harness must actually catch the old pattern.

    Without this, a green suite would prove only that the harness is too weak
    to fail. This is the pre-fix code, so it must break.
    """
    db = tmp_path / "unconditional.db"

    def old_style() -> sqlite3.Connection:
        def setup(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA busy_timeout=30000")  # does not cover journal_mode
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()

        return _open(db, setup)

    trials_used = 0
    for attempt in range(1, ANTI_VACUITY_TRIALS + 1):
        db.unlink(missing_ok=True)
        trials_used = attempt
        if _race(old_style):
            break
    else:  # pragma: no cover - ~7e-11 with the measured reproduction rate
        pytest.fail(
            f"the harness never reproduced the cold-start failure in "
            f"{ANTI_VACUITY_TRIALS} trials, so a pass in the tests below would "
            "prove nothing. At the measured ~5.7% per-trial rate this should "
            "happen about once in 10^10 runs, so treat it as the harness having "
            "stopped racing — not as luck."
        )
    assert trials_used <= ANTI_VACUITY_TRIALS


def test_ensure_wal_survives_a_concurrent_cold_start(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"

    def new_style() -> sqlite3.Connection:
        return _open(db, ensure_wal)

    for _ in range(HOLDS_TRIALS):
        db.unlink(missing_ok=True)
        assert _race(new_style) == []


@pytest.mark.parametrize("store_name", ["jobs", "lease", "nonce", "registry"])
def test_each_adopting_store_survives_a_concurrent_cold_start(
    store_name: str, tmp_path: Path
) -> None:
    """The four stores routed through ``ensure_wal`` open concurrently.

    The database is created first and then forced **back** to the default
    journal mode, so each race is against a not-yet-WAL file — the exact window
    T31 is about — without also racing schema creation.

    An earlier version of this test opened the database once and left it in WAL.
    Every race was then a warm one, and the test passed against the
    unconditional pre-fix code, proving nothing.
    """
    db = tmp_path / f"{store_name}.db"

    def build():
        """Create the store single-threaded and return its connect callable."""
        if store_name == "jobs":
            from novafabric.jobs.store import JobStore

            return JobStore(db_path=db)._connect
        if store_name == "lease":
            from novafabric.ha.lease import SqliteLeaseStore

            return SqliteLeaseStore(db)._connect
        if store_name == "nonce":
            from novafabric.trust.novaseal.nonce_store import NonceStore

            return NonceStore(db)._connect
        from novafabric.registry.store import get_connection

        get_connection(db).close()
        return lambda: get_connection(db)

    def reset_cold():
        """Fresh file, schema built single-threaded, journal mode back to DELETE.

        Rebuilding per trial rather than flipping the mode on the previous
        file: leftover ``-wal``/``-shm`` companions make the WAL->DELETE switch
        itself fail with ``database is locked``, which would be the test's own
        artefact rather than the product's behaviour.
        """
        for suffix in ("", "-wal", "-shm"):
            db.with_name(db.name + suffix).unlink(missing_ok=True)
        make = build()
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA journal_mode=DELETE")
        c.commit()
        c.close()
        mode = sqlite3.connect(str(db)).execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() != "wal", "the arm under test must start NOT in WAL"
        return make

    for _ in range(40):
        make = reset_cold()
        assert _race(make) == [], f"{store_name} failed a concurrent cold open"


def test_lease_store_survives_concurrent_construction(tmp_path: Path) -> None:
    """Constructing the store concurrently is safe too, not only connecting.

    ``SqliteLeaseStore.__init__`` runs ``executescript`` and an
    ``INSERT OR IGNORE`` in one transaction, which is a heavier cold path than
    ``JobStore.__init__`` (schema only). Eight threads constructing the store on
    one cold database: 0 failures in 200 trials when measured.
    """
    from novafabric.ha.lease import SqliteLeaseStore

    db = tmp_path / "lease-init.db"
    for _ in range(40):
        for suffix in ("", "-wal", "-shm"):
            db.with_name(db.name + suffix).unlink(missing_ok=True)
        assert _race(lambda: SqliteLeaseStore(db)._connect()) == []


# --------------------------------------------------------------------------
# Deterministic cover for the retry branches.
#
# A healthy race never reaches them: instrumented over 360 eight-thread cold
# races, no connection needed a second attempt. Driving them with a stub is the
# only way to test them without depending on contention that may not occur.
# --------------------------------------------------------------------------


class _FakeConn:
    """Minimal stand-in scripted with the journal modes to report."""

    def __init__(self, modes: list[str | Exception]) -> None:
        self._modes = list(modes)
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []

    def execute(self, sql: str):
        self.statements.append(sql)
        nxt = self._modes.pop(0) if self._modes else "delete"
        if isinstance(nxt, Exception):
            raise nxt

        class _Cur:
            def fetchone(self_inner):
                return (nxt,)

        return _Cur()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff schedule instead of waiting it out."""
    slept: list[float] = []
    monkeypatch.setattr(_sqlite_util.time, "sleep", slept.append)
    return slept


def test_ensure_wal_returns_immediately_when_already_in_wal(no_sleep) -> None:
    conn = _FakeConn(["wal"])
    ensure_wal(conn)  # type: ignore[arg-type]
    assert conn.statements == ["PRAGMA journal_mode"]
    assert conn.commits == 0, "reading the mode must not write"
    assert no_sleep == []


def test_a_loser_is_satisfied_by_the_winners_switch(no_sleep) -> None:
    """The exit condition is the invariant, not "my statement succeeded"."""
    conn = _FakeConn(["delete", "wal"])  # read says no, our write reports WAL
    ensure_wal(conn)  # type: ignore[arg-type]
    assert conn.statements == ["PRAGMA journal_mode", "PRAGMA journal_mode=WAL"]
    assert no_sleep == []


def test_ensure_wal_retries_with_bounded_backoff_then_succeeds(no_sleep) -> None:
    busy = sqlite3.OperationalError("database is locked")
    conn = _FakeConn([busy, busy, "wal"])
    ensure_wal(conn)  # type: ignore[arg-type]
    assert len(no_sleep) == 2, "one backoff per failed attempt"
    assert no_sleep == sorted(no_sleep), "backoff must not shrink"
    assert max(no_sleep) <= _sqlite_util.WAL_BACKOFF_MAX_S
    assert conn.rollbacks == 2, "each backoff drops this connection's lock"


def test_ensure_wal_reraises_the_real_error_when_the_budget_runs_out(no_sleep) -> None:
    busy = sqlite3.OperationalError("database is locked")
    conn = _FakeConn([busy] * (WAL_ATTEMPTS * 2))
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        ensure_wal(conn)  # type: ignore[arg-type]
    assert excinfo.value is busy, "the caller must see SQLite's own error"
    assert len(no_sleep) == WAL_ATTEMPTS - 1, "no sleep after the last attempt"


def test_ensure_wal_fails_loudly_when_the_mode_never_switches(no_sleep) -> None:
    """No exception, but the mode stays DELETE: a silent fallback is not allowed."""
    conn = _FakeConn(["delete"] * (WAL_ATTEMPTS * 2))
    with pytest.raises(sqlite3.OperationalError, match="lease database"):
        ensure_wal(conn, what="lease database")  # type: ignore[arg-type]
