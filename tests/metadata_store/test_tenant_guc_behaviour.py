"""FR-07 / FR-12, behaviourally: every tenant-GUC write the store EXECUTES is
transaction-scoped.

``test_grep_constraints.py`` reads the source; a static scan can be evaded by a
spelling it does not know. This guard does not read the source at all. It drives
the real :class:`PostgresMetadataStore` against a recording connection, collects
every statement the store actually sends (rendered exactly as psycopg would —
``sql.Composed`` included — with the parameters bound) and every
``psycopg.connect`` call it makes, and checks each one:

* a statement that writes the tenant GUC must be exactly ``SET LOCAL
  app.current_tenant_id = '<uuid>'`` or ``set_config('app.current_tenant_id',
  '<uuid>', true)`` — default-deny, so an unknown spelling is a failure, not a
  pass;
* it must run **inside** ``conn.transaction()`` (a ``SET LOCAL`` outside a
  transaction is a no-op in Postgres, so the tenant context would be missing);
* no session-level ``SET`` of any ``app.*`` GUC is ever sent;
* no connection is opened with a libpq ``options=-c …`` or a DSN ``options=``
  that would set a GUC for the whole session.

The checker is pinned against Postgres itself by
:func:`test_checker_agrees_with_postgres` (``container``): for every executable
bypass form, the checker flags it exactly when the GUC really outlives the
transaction on a real server.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from novafabric.metadata_store.postgres import PostgresMetadataStore

TENANT = UUID("11111111-2222-4333-8444-555555555555")

# ── the runtime checker ─────────────────────────────────────────────────────

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SAFE_WRITES = (
    re.compile(rf"set local app\.current_tenant_id (?:=|to) '{_UUID}'"),
    re.compile(rf"select set_config\('app\.current_tenant_id', '{_UUID}', true\)"),
)
_SAFE_READ = re.compile(r"current_setting\('app\.current_tenant_id'(?:::text)?\)")
_TENANT_WRITE = re.compile(r"current_tenant_id|set_config")
_SESSION_SET = re.compile(r"^set (?:session )?app\.")


@dataclass(frozen=True)
class Executed:
    """One statement as the server would receive it."""

    text: str  # normalised: parameters bound, lower case, whitespace collapsed
    in_transaction: bool


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render(query: Any, params: Any = None) -> str:
    """The statement text with *params* bound, normalised for matching.

    Double quotes are dropped so a quoted identifier (``"app.current_tenant_id"``)
    reads the same as a bare one: Postgres accepts both.
    """
    if isinstance(query, sql.Composable):
        text = query.as_string(None)
    elif isinstance(query, bytes):
        text = query.decode()
    else:
        text = str(query)
    if params:
        values = list(params.values()) if isinstance(params, dict) else list(params)
        for value in values:
            text = text.replace("%s", _literal(value), 1)
    return " ".join(text.replace('"', "").split()).lower()


def violation(stmt: Executed) -> str | None:
    """Why *stmt* is not a transaction-scoped tenant-GUC write, or None if fine."""
    text = stmt.text
    if any(form.fullmatch(text) for form in _SAFE_WRITES):
        return None if stmt.in_transaction else f"outside a transaction: {text}"
    if _SESSION_SET.search(text):
        return f"session-scoped SET: {text}"
    if _TENANT_WRITE.search(_SAFE_READ.sub("", text)):
        return f"tenant GUC written in an unrecognised form: {text}"
    return None


def connect_violation(
    args: tuple[Any, ...], kwargs: dict[str, Any], env: dict[str, str] | None = None
) -> str | None:
    """A connection that sets any GUC for the whole session is a violation.

    Default-deny on the three libpq routes: an ``options=`` keyword carrying
    ``-c``, an ``options`` parameter inside the DSN, or ``PGOPTIONS`` in the
    environment libpq reads.
    """
    options = str(kwargs.get("options") or "")
    dsn = " ".join(str(a) for a in args) + " " + str(kwargs.get("conninfo") or "")
    if "-c" in options or re.search(r"options\s*=", dsn, re.I):
        return f"connection sets a session GUC: dsn={dsn.strip()!r} options={options!r}"
    if (env if env is not None else os.environ).get("PGOPTIONS"):
        return "connection sets a session GUC through PGOPTIONS"
    return None


# ── the recording connection ────────────────────────────────────────────────


class _Cursor:
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


@dataclass
class Recorder:
    statements: list[Executed] = field(default_factory=list)
    connects: list[str | None] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def violations(self) -> list[str]:
        found = [v for v in map(violation, self.statements) if v]
        return found + [c for c in self.connects if c]


class RecordingConnection:
    """Enough of ``psycopg.Connection`` for the store; records what it is sent."""

    def __init__(self, recorder: Recorder, *, autocommit: bool = False) -> None:
        self._rec = recorder
        self._depth = 0
        self.autocommit = autocommit
        self.closed = False

    def execute(self, query: Any, params: Any = None, **_: Any) -> _Cursor:
        self._rec.statements.append(Executed(render(query, params), self._depth > 0))
        return _Cursor()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._depth += 1
        self._rec.events.append("begin")
        try:
            yield
        finally:
            self._depth -= 1
            self._rec.events.append("end")

    def commit(self) -> None:
        self._rec.events.append("commit")

    def close(self) -> None:
        self.closed = True
        self._rec.events.append("close")

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class RecordingPool:
    """The two calls the store makes on a ``psycopg_pool.ConnectionPool``."""

    def __init__(self, recorder: Recorder) -> None:
        self._rec = recorder

    @contextmanager
    def connection(self) -> Iterator[RecordingConnection]:
        conn = RecordingConnection(self._rec)
        try:
            yield conn
        finally:
            self._rec.events.append("returned")

    def close(self) -> None:
        pass


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()

    def fake_connect(*args: Any, **kwargs: Any) -> RecordingConnection:
        rec.connects.append(connect_violation(args, kwargs))
        return RecordingConnection(rec)

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return rec


def _drive(store: Any) -> None:
    """Every DML entry point of the store, inside one tenant context each."""
    run = uuid4()
    with store.begin_tenant_context(TENANT):
        store.register_run(run, TENANT, event_type="run")
    with store.begin_tenant_context(TENANT):
        store.lookup_run(run, TENANT)
        store.query_runs(TENANT, limit=5)
    with store.begin_tenant_context(TENANT):
        store.record_signature(run, TENANT, "sha256:x", {"k": "v"})
        store.delete_run(run, TENANT)


# ── the shipped store ───────────────────────────────────────────────────────


@pytest.mark.parametrize("pooled", [False, True], ids=["connect", "pool"])
def test_store_writes_the_tenant_guc_only_transaction_scoped(
    recorder: Recorder, pooled: bool
) -> None:
    store = PostgresMetadataStore(
        "postgresql://u@localhost/db", pool=RecordingPool(recorder) if pooled else None
    )
    store.bootstrap()
    store.health_check()
    _drive(store)

    assert recorder.violations() == []
    writes = [s for s in recorder.statements if "current_tenant_id" in
              _SAFE_READ.sub("", s.text)]
    # Non-vacuous: one write per context, every one of them really observed.
    assert len(writes) == 3, writes
    assert all(w.in_transaction for w in writes)
    assert all(w.text == f"set local app.current_tenant_id = '{TENANT}'" for w in writes)
    # Every tenant transaction ends before its connection is closed or handed
    # back to the pool, so a pooled connection never carries the GUC onwards.
    open_tx = False
    for event in recorder.events:
        if event == "begin":
            open_tx = True
        elif event == "end":
            open_tx = False
        elif event in {"close", "returned"}:
            assert not open_tx, recorder.events
    assert recorder.events.count("end") == 3


def test_the_connection_is_opened_without_session_options(recorder: Recorder) -> None:
    store = PostgresMetadataStore("postgresql://u@localhost/db")
    with store.begin_tenant_context(TENANT):
        pass
    assert recorder.connects == [None]


# ── the checker catches every bypass, as executed ───────────────────────────

T = str(TENANT)

#: Each adversarial-review bypass (``a2_rls_guard_bypass.py``), as the server
#: would receive it: (query, params). A static spelling trick changes nothing
#: here — this is what the store would send.
EXECUTED_BYPASSES: dict[str, tuple[Any, Any]] = {
    "plain": (f"SET app.current_tenant_id = '{T}'", None),
    "session": (f"SET SESSION app.current_tenant_id = '{T}'", None),
    "concat": ("SET " + "app.current_tenant_id = '%s'" % T, None),
    "fstring_name": (f"SET {'app.current_tenant_id'} = '{T}'", None),
    "multiline_triple": (f"\n    SET\n    app.current_tenant_id = '{T}'", None),
    "quoted_ident": (f"SET \"app.current_tenant_id\" = '{T}'", None),
    "set_config_param_name": ("SELECT set_config(%s, %s, false)", ("app.current_tenant_id", T)),
    "set_config_param_islocal":
        ("SELECT set_config('app.current_tenant_id', %s, %s)", (T, False)),
    "set_config_str_false": ("SELECT set_config('app.current_tenant_id', %s, 'false')", (T,)),
    "set_config_0bool": ("SELECT set_config('app.current_tenant_id', %s, 0::bool)", (T,)),
    "set_config_multiline":
        ("SELECT set_config(\n 'app.current_tenant_id', %s,\n false)", (T,)),
    "psycopg_sql_compose": (
        sql.SQL("SET {} = {}").format(
            sql.Identifier("app", "current_tenant_id"), sql.Literal(T)),
        None,
    ),
    "set_config_upper": (f"SELECT SET_CONFIG('APP.CURRENT_TENANT_ID', '{T}', FALSE)", None),
}


@pytest.mark.parametrize(
    ("query", "params"), EXECUTED_BYPASSES.values(), ids=EXECUTED_BYPASSES.keys()
)
def test_checker_flags_every_executed_bypass(query: Any, params: Any) -> None:
    stmt = Executed(render(query, params), in_transaction=True)
    assert violation(stmt), stmt.text


def test_checker_flags_the_libpq_options_bypass() -> None:
    assert connect_violation(("dbname=x",), {"options": f"-c app.current_tenant_id={T}"}, {})
    assert connect_violation(
        (f"postgresql://u@h/db?options=-c%20app.current_tenant_id%3D{T}",), {}, {})
    assert connect_violation(("host=h options='-c app.current_tenant_id=x'",), {}, {})
    assert connect_violation(("postgresql://u@h/db",), {}, {"PGOPTIONS": "-c app.x=1"})
    assert connect_violation(("postgresql://u@h/db",), {}, {}) is None


@pytest.mark.parametrize(
    ("query", "params"),
    [
        (f"SET LOCAL app.current_tenant_id = '{T}'", None),
        ("SELECT set_config('app.current_tenant_id', %s, true)", (T,)),
        ("SELECT * FROM runs WHERE tenant_id = %s", (T,)),
        ("CREATE POLICY p ON t USING (tenant_id = "
         "current_setting('app.current_tenant_id')::uuid)", None),
    ],
)
def test_checker_passes_transaction_scoped_and_unrelated_statements(
    query: Any, params: Any
) -> None:
    assert violation(Executed(render(query, params), in_transaction=True)) is None


def test_checker_flags_set_local_outside_a_transaction() -> None:
    assert violation(Executed(f"set local app.current_tenant_id = '{T}'", False))


def test_a_session_set_store_fails_the_behavioural_guard(recorder: Recorder) -> None:
    """Negative control: the known-bad fixture store is caught by what it executes."""
    from fixtures.broken_session_set import BrokenMetadataStore

    store = BrokenMetadataStore("postgresql://u@localhost/db")
    with store.begin_tenant_context(TENANT):
        store.register_run(uuid4(), TENANT)
    assert any("session-scoped SET" in v for v in recorder.violations())


def test_a_store_mutated_to_any_bypass_fails_the_behavioural_guard(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewrite the shipped SET LOCAL into each bypass, as the store would send it."""
    original = RecordingConnection.execute
    for name, (query, params) in EXECUTED_BYPASSES.items():
        recorder.statements.clear()

        def mutated(self: RecordingConnection, q: Any, p: Any = None, *,
                    _q: Any = query, _p: Any = params, **kw: Any) -> _Cursor:
            if isinstance(q, str) and q.startswith("SET LOCAL app.current_tenant_id"):
                return original(self, _q, _p)
            return original(self, q, p, **kw)

        monkeypatch.setattr(RecordingConnection, "execute", mutated)
        store = PostgresMetadataStore("postgresql://u@localhost/db")
        with store.begin_tenant_context(TENANT):
            pass
        assert recorder.violations(), f"behavioural guard missed the {name} mutant"
    monkeypatch.setattr(RecordingConnection, "execute", original)


# ── the checker, pinned against a real Postgres (container tier) ────────────


def test_checker_agrees_with_postgres(postgres_url: str) -> None:
    """For each executable form: flagged by the checker <=> the GUC outlives
    the transaction on a real server. Skips with the container tier."""
    forms = dict(EXECUTED_BYPASSES)
    forms["set_local"] = (f"SET LOCAL app.current_tenant_id = '{T}'", None)
    forms["set_config_true"] = ("SELECT set_config('app.current_tenant_id', %s, true)", (T,))
    for name, (query, params) in forms.items():
        with psycopg.connect(postgres_url) as conn:
            with conn.transaction():
                conn.execute(query, params)
            leaked = conn.execute(
                "SELECT current_setting('app.current_tenant_id', true)"
            ).fetchone()[0]
        persisted = bool(leaked)
        flagged = violation(Executed(render(query, params), in_transaction=True)) is not None
        assert flagged == persisted, f"{name}: checker={flagged} postgres={persisted}"

    # libpq options=-c: the GUC is set for the whole session before any SQL.
    with psycopg.connect(postgres_url, options=f"-c app.current_tenant_id={T}") as conn:
        value = conn.execute("SELECT current_setting('app.current_tenant_id', true)").fetchone()
    assert value is not None and value[0] == T
    assert connect_violation((postgres_url,), {"options": f"-c app.current_tenant_id={T}"}, {})
