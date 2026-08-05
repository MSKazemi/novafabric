"""Degradation contract for the dependencies ADR-0222 moved out of core.

ADR-0222's promise is not just "the import still works". It is a behavioural
contract, one clause per core-reachable call site that touches a moved
dependency:

    Either fall back to a stdlib-equivalent path producing **identical**
    results, or raise an ``ImportError`` naming the **exact extra** to
    install. Never a silent wrong answer, and never a bare
    ``ModuleNotFoundError`` with no remedy.

Each test here pins one clause of that contract. They matter more than the
import-surface guard: an import that succeeds but then quietly returns partial
data is worse than one that fails.

The blocker is a ``sys.meta_path`` finder rather than an uninstall, so these
run in the ordinary dev venv (which has every extra installed).
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

BLOCKED_IMPORT_NAMES = frozenset(
    {"duckdb", "pyarrow", "numpy", "community", "clickhouse_connect"}
)


class _BlockingFinder:
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname.split(".")[0] in BLOCKED_IMPORT_NAMES:
            raise ImportError(f"No module named {fullname!r} (lean-install blocker)")
        return None


@pytest.fixture
def lean_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ADR-0222 extras look uninstalled for the duration of a test.

    Evicting the already-imported modules is not optional: ``sys.modules`` is
    consulted *before* ``sys.meta_path``, so a blocker alone would be silently
    bypassed for anything the test session had already imported (which, in this
    dev venv, is all of them). ``monkeypatch.delitem`` restores them on
    teardown, so other tests are unaffected.
    """
    for name in [
        m for m in sys.modules if m.split(".")[0] in BLOCKED_IMPORT_NAMES
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockingFinder(), *sys.meta_path])


# ---------------------------------------------------------------------------
# Clause 1 — `nova query`: fall back to sqlite with IDENTICAL results
# ---------------------------------------------------------------------------


def _write_capsule(
    root: Path,
    run_id: str,
    *,
    created_at: str,
    status: str = "success",
    model: str = "claude-3-7-sonnet",
    cost: float = 0.01,
    tokens_in: int = 100,
    tokens_out: int = 50,
    duration_ms: int = 800,
) -> None:
    capsule = root / run_id
    capsule.mkdir(parents=True)
    (capsule / "capsule.json").write_text(
        json.dumps(
            {
                "schema_version": "0.1.0",
                "run_id": run_id,
                "created_at": created_at,
                "status": status,
            }
        )
    )
    (capsule / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "gen_ai.request.model": model,
                "gen_ai.response.model": model,
                "duration_ms": duration_ms,
                "gen_ai.usage.input_tokens": tokens_in,
                "gen_ai.usage.output_tokens": tokens_out,
                "nova.cost": {"currency": "USD", "amount": cost},
            }
        )
        + "\n"
    )


@pytest.fixture
def query_capsules(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    for i, (model, cost, tin, tout, dur) in enumerate(
        [
            ("claude-3-7-sonnet", 0.01, 100, 50, 800),
            ("claude-3-7-sonnet", 0.03, 300, 90, 1500),
            ("gpt-4o", 0.02, 200, 70, 1100),
            ("gpt-4o", 0.05, 500, 250, 2400),
            ("gemini-2-flash", 0.005, 40, 15, 300),
        ]
    ):
        _write_capsule(
            root,
            f"run-{i}",
            created_at=f"2026-07-1{i}T12:00:00Z",
            model=model,
            cost=cost,
            tokens_in=tin,
            tokens_out=tout,
            duration_ms=dur,
        )
    return root


def test_query_engine_detection_falls_back_to_sqlite(lean_install: None) -> None:
    """`_detect_engine()` must pick sqlite, not raise, when duckdb is absent."""
    from novafabric.query.engine import _detect_engine

    assert _detect_engine() == "sqlite"


_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

# Clause-shaped query plans exercised against both engines.
_PARITY_PLANS: dict[str, dict[str, Any]] = {
    "count-by-model": {"select": "count()", "group_by": "model", "since": "30d"},
    "cost-aggregates": {
        "select": ["sum(cost)", "avg(cost)"],
        "group_by": "model",
        "since": "30d",
        "order_by": "sum(cost)",
    },
    "latency-percentiles": {
        "select": ["p95(latency)", "max(latency)", "min(latency)"],
        "group_by": "model",
        "since": "30d",
    },
    "ungrouped-token-sums": {
        "select": ["count()", "sum(prompt_tokens)", "sum(completion_tokens)"],
        "since": "30d",
    },
    # Edge: a filter that matches nothing must agree on emptiness too.
    "empty-result": {
        "select": "count()",
        "group_by": "model",
        "where": "model = no-such-model",
        "since": "30d",
    },
}


def test_query_reports_sqlite_engine_under_lean_install(
    lean_install: None, query_capsules: Path
) -> None:
    """The result must *say* which engine ran — provenance, not a silent swap."""
    from novafabric.query import build_plan, run_query

    plan = build_plan(
        select=["count()", "sum(cost)", "avg(latency)"],
        group_by="model",
        since="30d",
    )
    result = run_query(plan, query_capsules, now=_NOW)
    assert result["index"]["engine"] == "sqlite"
    assert result["rows"], "sqlite fallback returned no rows"


@pytest.mark.parametrize("plan_name", sorted(_PARITY_PLANS))
def test_sqlite_and_duckdb_engines_return_identical_rows(
    query_capsules: Path, plan_name: str
) -> None:
    """The core correctness claim behind demoting duckdb to an accelerator.

    If these two ever disagree, `nova query` silently returns different answers
    depending on which extras happen to be installed — which would make the
    `query` extra a correctness dependency, not a performance one, and would
    invalidate ADR-0222's decision to move duckdb out of core.
    """
    pytest.importorskip("duckdb")
    from novafabric.query import build_plan, run_query

    plan = build_plan(**_PARITY_PLANS[plan_name])
    duck = run_query(plan, query_capsules, engine="duckdb", now=_NOW)
    lite = run_query(plan, query_capsules, engine="sqlite", now=_NOW)

    assert duck["index"]["engine"] == "duckdb"
    assert lite["index"]["engine"] == "sqlite"
    assert lite["rows"] == duck["rows"], f"engine divergence for plan: {plan_name}"
    assert lite["index"]["capsule_count"] == duck["index"]["capsule_count"]


# ---------------------------------------------------------------------------
# Clause 2 — backup: skip the derived duckdb snapshot, still succeed
# ---------------------------------------------------------------------------


def test_duckdb_snapshot_returns_skip_reason_instead_of_raising(
    lean_install: None, tmp_path: Path
) -> None:
    """A backup must not fail because a *derived, rebuildable* cache was skipped.

    The dashboard duckdb store is a topology cache `nova serve` rebuilds, so
    skipping it loses no evidence — but the skip has to be reported, not
    swallowed, or the backup would silently claim coverage it does not have.
    """
    from novafabric.backup.coverage import duckdb_snapshot

    src = tmp_path / "dashboard.duckdb"
    src.write_bytes(b"not-really-a-duckdb-file")
    reason = duckdb_snapshot(src, tmp_path / "out.duckdb")

    assert reason is not None, "skip must be reported, not silently succeed"
    assert "duckdb" in reason.lower()
    assert "not installed" in reason.lower()


def test_duckdb_snapshot_succeeds_when_duckdb_is_available(tmp_path: Path) -> None:
    """Success case: with the extra installed the snapshot returns None (no skip)."""
    duckdb = pytest.importorskip("duckdb")

    src = tmp_path / "dashboard.duckdb"
    conn = duckdb.connect(str(src))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("INSERT INTO t VALUES (1), (2)")
    conn.close()

    from novafabric.backup.coverage import duckdb_snapshot

    assert duckdb_snapshot(src, tmp_path / "out.duckdb") is None


# ---------------------------------------------------------------------------
# Clause 3 — restore: fail LOUDLY, but only when a .duckdb member exists
# ---------------------------------------------------------------------------


def _member(path: str, **kw: Any) -> Any:
    from novafabric.backup.models import BackupMember

    defaults: dict[str, Any] = {
        "path": path,
        "sha256": "0" * 64,
        "size_bytes": 1,
        "kind": "state_db",
        "origin": "home",
    }
    defaults.update(kw)
    return BackupMember(**defaults)


def test_restore_fails_loudly_when_duckdb_member_cannot_be_verified(
    lean_install: None, tmp_path: Path
) -> None:
    """Unlike backup, restore must NOT degrade: unverified is not verified.

    A restore that reports success for a store it never opened would be a
    silent wrong answer — exactly what the degradation contract forbids.
    """
    from novafabric.backup.restore import _verify_state_dbs

    (tmp_path / "dashboard.duckdb").write_bytes(b"stub")
    result = _verify_state_dbs(tmp_path, [_member("dashboard.duckdb")])

    assert result.ok is False
    assert "dashboard.duckdb" in result.detail
    assert "duckdb module unavailable" in result.detail
    assert "cannot verify" in result.detail


def test_restore_succeeds_under_lean_install_when_no_duckdb_member_present(
    lean_install: None, tmp_path: Path
) -> None:
    """Edge case that keeps the failure above from being over-broad.

    The overwhelmingly common backup set has no `.duckdb` member at all (the
    dashboard cache only exists once `nova serve` has run). Those restores must
    still pass on a lean install — otherwise ADR-0222 would have broken restore
    for every default user.
    """
    from novafabric.backup.restore import _verify_state_dbs

    db_path = tmp_path / "registry.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()
    conn.close()

    result = _verify_state_dbs(tmp_path, [_member("registry.db")])

    assert result.ok is True, result.detail
    assert "1 state store(s) open clean" in result.detail


# ---------------------------------------------------------------------------
# Clause 4 — lineage insights: degrade with a stated reason
# ---------------------------------------------------------------------------


def test_insights_duckdb_cost_source_degrades_with_named_reason(
    lean_install: None, tmp_path: Path
) -> None:
    """An optional cost source must announce that it was ignored.

    `nova insights` still produces a report; the report just has to be honest
    that one input was unavailable rather than implying zero cost.
    """
    from novafabric.lineage.analytics.insights import _cost_from_duckdb

    cost_db = tmp_path / "accumulator.duckdb"
    cost_db.write_bytes(b"stub")
    hotspots, note = _cost_from_duckdb(cost_db)

    assert hotspots is None
    assert "duckdb" in note.lower()
    assert str(cost_db) in note


def test_insights_report_builds_under_lean_install(
    lean_install: None, tmp_path: Path
) -> None:
    """End-to-end: the default `nova insights` path works without the extras.

    This is the command that forced networkx to stay in core, so it is the one
    that most needs to keep working on a plain install.
    """
    from novafabric.lineage._store import LineageStore
    from novafabric.lineage.analytics.insights import build_insights_report

    store = LineageStore(tmp_path / "lineage.db")
    report = build_insights_report(store)

    assert report is not None
    assert report.to_markdown()


# ---------------------------------------------------------------------------
# Clause 5 — ClickHouse: ImportError naming a REAL extra
# ---------------------------------------------------------------------------


def _declared_extras() -> set[str]:
    import tomllib

    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with path.open("rb") as handle:
        return set(tomllib.load(handle)["project"]["optional-dependencies"])


def test_clickhouse_accumulator_raises_importerror_naming_a_real_extra(
    lean_install: None,
) -> None:
    """The hint has to resolve. `pip install novafabric[clickhouse]` must exist."""
    import importlib

    for name in [m for m in sys.modules if m.startswith("novafabric.evidence_fabric")]:
        del sys.modules[name]
    module = importlib.import_module(
        "novafabric.evidence_fabric.clickhouse_accumulator"
    )

    with pytest.raises(ImportError) as excinfo:
        module.ClickHouseAccumulator()

    message = str(excinfo.value)
    assert "pip install novafabric[clickhouse]" in message
    assert "clickhouse" in _declared_extras()


def test_clickhouse_cost_store_raises_importerror_naming_a_real_extra(
    lean_install: None,
) -> None:
    """Regression: this used to surface a bare "No module named" with no remedy.

    ``cost/clickhouse_store._get_client()`` imported clickhouse_connect
    directly. That was invisible while clickhouse-connect was an unconditional
    core dependency; once ADR-0222 moved it to an extra it became a
    dead-end error message, so a guard was added.
    """
    from novafabric.cost.clickhouse_store import require_clickhouse_connect

    with pytest.raises(ImportError) as excinfo:
        require_clickhouse_connect()

    message = str(excinfo.value)
    assert "pip install novafabric[clickhouse]" in message
    assert "clickhouse" in _declared_extras()


def test_clickhouse_guard_is_a_noop_when_the_extra_is_installed() -> None:
    """Success case: the guard must not fire in a working install."""
    pytest.importorskip("clickhouse_connect")
    from novafabric.cost.clickhouse_store import require_clickhouse_connect

    require_clickhouse_connect()


# ---------------------------------------------------------------------------
# Clause 6 — evidence_fabric lazy re-export
# ---------------------------------------------------------------------------


def test_dependency_free_evidence_fabric_symbol_imports_under_lean_install(
    lean_install: None,
) -> None:
    """`EventQueueConsumer` needs nothing optional and must import as such.

    Before ADR-0222 the package's eager ``__init__`` made every symbol here —
    including this one — require duckdb and pyarrow at import time.
    """
    import importlib

    for name in [m for m in sys.modules if m.startswith("novafabric.evidence_fabric")]:
        del sys.modules[name]
    package = importlib.import_module("novafabric.evidence_fabric")

    assert package.EventQueueConsumer is not None


def test_importing_evidence_fabric_does_not_load_duckdb_or_pyarrow() -> None:
    """The re-export must be genuinely lazy, not merely tolerant of failure.

    Checked in a subprocess: this test session has already imported duckdb and
    pyarrow many times over, so an in-process `sys.modules` check would pass
    regardless of whether the laziness works.
    """
    import subprocess

    script = (
        "import sys\n"
        "import novafabric.evidence_fabric as ef\n"
        "assert 'duckdb' not in sys.modules, 'package import pulled in duckdb'\n"
        "assert 'pyarrow' not in sys.modules, 'package import pulled in pyarrow'\n"
        "ef.EventQueueConsumer\n"
        "assert 'duckdb' not in sys.modules, 'EventQueueConsumer pulled in duckdb'\n"
        "ef.DuckDBAccumulator\n"
        "assert 'duckdb' in sys.modules, 'DuckDBAccumulator did not load duckdb'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_scale_tier_symbol_raises_importerror_naming_its_extra(
    lean_install: None,
) -> None:
    """Failure case: a duckdb-backed symbol must name the extra that fixes it."""
    import importlib

    for name in [m for m in sys.modules if m.startswith("novafabric.evidence_fabric")]:
        del sys.modules[name]
    package = importlib.import_module("novafabric.evidence_fabric")

    with pytest.raises(ImportError) as excinfo:
        _ = package.DuckDBAccumulator

    message = str(excinfo.value)
    assert "pip install novafabric[scale]" in message
    assert "scale" in _declared_extras()


@pytest.mark.parametrize("name", ["DuckDBAccumulator", "LocalPIITable", "PIIEvent"])
def test_scale_gated_names_match_the_documented_contract(
    lean_install: None, name: str
) -> None:
    """Every name the module docstring files under ``[scale]`` must behave that way.

    ``PIIEvent`` is the trap: it is a plain Pydantic model needing nothing
    optional, so it reads as dependency-free — but it is *defined in*
    ``pii_table``, which imports pyarrow at module level, so importing it from
    this package does need the extra. An earlier draft of the docstring listed
    it as dependency-free, which was wrong. This pins the doc to the behaviour.
    """
    import importlib

    for mod in [m for m in sys.modules if m.startswith("novafabric.evidence_fabric")]:
        del sys.modules[mod]
    package = importlib.import_module("novafabric.evidence_fabric")

    with pytest.raises(ImportError) as excinfo:
        getattr(package, name)
    assert "pip install novafabric[scale]" in str(excinfo.value)


def test_event_queue_consumer_is_the_only_dependency_free_export(
    lean_install: None,
) -> None:
    """Pins the docstring's "only export that needs nothing optional" claim."""
    import importlib

    for mod in [m for m in sys.modules if m.startswith("novafabric.evidence_fabric")]:
        del sys.modules[mod]
    package = importlib.import_module("novafabric.evidence_fabric")

    resolvable = set()
    for name in package.__all__:
        try:
            getattr(package, name)
        except ImportError:
            continue
        resolvable.add(name)

    # The three internally-guarded scale-tier names still *resolve* under a lean
    # install (they raise on instantiation instead), so they resolve here too.
    assert "EventQueueConsumer" in resolvable
    assert resolvable == {
        "EventQueueConsumer",
        "AvroSerializer",
        "ClickHouseAccumulator",
        "NATSJetStreamConsumer",
    }


def test_unknown_evidence_fabric_attribute_still_raises_attributeerror() -> None:
    """A PEP 562 `__getattr__` must not turn typos into ImportErrors."""
    import novafabric.evidence_fabric as package

    with pytest.raises(AttributeError):
        _ = package.NoSuchBackend


def test_evidence_fabric_dir_lists_every_public_export() -> None:
    """`dir()` must stay useful once attribute access is lazy."""
    import novafabric.evidence_fabric as package

    assert set(dir(package)) == set(package.__all__)


# ---------------------------------------------------------------------------
# Clause 6 — ADR-0222 OQ-2: PyJWT / python-multipart left the core install
# ---------------------------------------------------------------------------
#
# Both used to be pinned in core *and* in the `server` extra. They are declared
# once now, in `server`, because that is the only tier that uses them. Moving a
# dependency out of core is only safe under the same contract as the rest of
# ADR-0222: nothing on a core-reachable path may import it, and anything that
# does need it must fail with a message naming the exact extra.


def _core_dependency_names() -> set[str]:
    import tomllib

    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return {
        re.split(r"[<>=!\[;\s]", dep, maxsplit=1)[0].strip().lower()
        for dep in data["project"]["dependencies"]
    }


def _server_extra_names() -> set[str]:
    import tomllib

    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return {
        re.split(r"[<>=!\[;\s]", dep, maxsplit=1)[0].strip().lower()
        for dep in data["project"]["optional-dependencies"]["server"]
    }


@pytest.mark.parametrize("dist", ["pyjwt", "python-multipart"])
def test_server_only_dependency_is_declared_once_in_the_server_extra(
    dist: str,
) -> None:
    assert dist not in _core_dependency_names(), (
        f"{dist} is server-only (`import jwt` appears only under "
        "src/novafabric/server/; python-multipart is FastAPI's form-parsing "
        "runtime requirement and is never imported directly). It must not be "
        "in the default install."
    )
    assert dist in _server_extra_names(), (
        f"{dist} must stay declared in the `server` extra — dropping it from "
        "both tiers would make server mode silently uninstallable."
    )


def test_jwt_is_not_imported_on_any_core_cli_path() -> None:
    """The check that justified the move — run, not assumed.

    Exactly the mistake ADR-0222 avoided for `networkx`, which *is* imported at
    CLI start-up and therefore stayed in core.
    """
    import subprocess
    import sys as _sys

    probe = (
        "import sys, novafabric.cli.main;"
        "print(int('jwt' in sys.modules));"
        "print(int(any(m.split('.')[0] == 'multipart' for m in sys.modules)))"
    )
    out = subprocess.run(
        [_sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["0", "0"], (
        "importing the CLI now pulls in jwt/multipart, so they are core-reachable "
        "and must go back into the core dependency list"
    )


def test_offline_tokens_import_error_names_the_server_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing PyJWT must say which extra fixes it, not 'No module named jwt'."""
    for name in [m for m in sys.modules if m.split(".")[0] in {"jwt"}]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(
        sys.modules, "novafabric.server.offline_tokens", raising=False
    )

    class _NoJwt:
        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
            if fullname.split(".")[0] == "jwt":
                raise ImportError("No module named 'jwt' (test blocker)")
            return None

    monkeypatch.setattr(sys, "meta_path", [_NoJwt(), *sys.meta_path])

    with pytest.raises(ImportError) as excinfo:
        importlib.import_module("novafabric.server.offline_tokens")

    message = str(excinfo.value)
    assert "novafabric[server]" in message, message
    assert "PyJWT" in message, message


# ---------------------------------------------------------------------------
# Clause 7 — ADR-0222 OQ-3: the `query` extra must not be a *de*celerator
# ---------------------------------------------------------------------------
#
# `_detect_engine()` used to return "duckdb" whenever duckdb was importable.
# Measured (bench/query/bench_engine_crossover.py, 2026-08-01) that is ~20-25x
# SLOWER than sqlite at every size from 10 to 20,000 capsules, with a flat
# ratio — a per-row cost in the index build, not a start-up cost that
# amortises. So installing novafabric[query] / [scale] / [all] silently made
# `nova query` an order of magnitude slower, with no way to opt out.


def test_query_engine_defaults_to_sqlite_even_when_duckdb_is_installed() -> None:
    pytest.importorskip("duckdb")
    from novafabric.query.engine import _detect_engine

    assert _detect_engine() == "sqlite", (
        "duckdb is ~20x slower than sqlite for the query index at every "
        "measured size; it must not be picked implicitly just because the "
        "extra happens to be installed"
    )


def test_duckdb_remains_reachable_by_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing a default must not remove the capability."""
    pytest.importorskip("duckdb")
    from novafabric.query.engine import _ENGINE_ENV_VAR, _detect_engine

    monkeypatch.setenv(_ENGINE_ENV_VAR, "duckdb")
    assert _detect_engine() == "duckdb"


def test_explicit_duckdb_request_without_the_extra_warns_and_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unhonourable request is logged, never silently downgraded — and never
    fatal, because `nova query` is a read-only command."""
    import logging as _logging

    from novafabric.query.engine import _ENGINE_ENV_VAR, _detect_engine

    monkeypatch.setenv(_ENGINE_ENV_VAR, "duckdb")
    for name in [m for m in sys.modules if m.split(".")[0] == "duckdb"]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _NoDuckDB:
        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
            if fullname.split(".")[0] == "duckdb":
                raise ImportError("No module named 'duckdb' (test blocker)")
            return None

    monkeypatch.setattr(sys, "meta_path", [_NoDuckDB(), *sys.meta_path])

    with caplog.at_level(_logging.WARNING):
        assert _detect_engine() == "sqlite"
    assert "novafabric[query]" in caplog.text, caplog.text
