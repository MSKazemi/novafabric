import contextlib
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Real shutil.which captured at import, before any test can monkeypatch it (TEST-OPA-1).
_REAL_WHICH = shutil.which


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_opa: do NOT force NoopEngine — let get_policy_engine() select the "
        "real OpaEngine when the opa binary is installed.",
    )


@pytest.fixture(autouse=True)
def _hermetic_novafabric_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hermetic ``NOVAFABRIC_*`` environment for every test (suite-health 2026-07-15).

    A developer shell that exports real data paths (e.g. ``NOVAFABRIC_HOME=~/novafabric-data/nova``,
    ``NOVAFABRIC_CAPSULE_DIR=…``) leaks the developer's live registry/capsule store into any test
    that resolves paths through ``novafabric._paths`` without overriding every var — tests then
    *read* real role assignments (test_serve_admin) and *write* capsules into the real store
    (tests/daemon). CI has none of these vars, so the breakage is dev-machine-only. Stripping the
    whole prefix makes every test see the same clean environment CI sees; tests that need a var
    set it afterwards via their own ``monkeypatch.setenv``, which always wins over this fixture.

    ``NOVAFABRIC_HOME`` is then *pointed at a per-test tmp dir* rather than merely deleted:
    with it unset, path resolution falls back to the real ``~/.novafabric`` (e.g. the topology
    store's ``dashboard.duckdb``), which both touches real user data and makes parallel xdist
    workers fight over one DuckDB file lock.
    """
    for key in list(os.environ):
        if key.startswith("NOVAFABRIC_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / ".nova-home-hermetic"))
    # The audit log lives OUTSIDE the home (~/.local/share/novafabric/) with a
    # hard-coded default; without this override, backup tests would slurp the
    # developer's real hash-chained audit log into test backup sets (ADR-0216).
    monkeypatch.setenv(
        "NOVAFABRIC_AUDIT_LOG_PATH",
        str(tmp_path / ".nova-home-hermetic" / "audit-hermetic.jsonl"),
    )
    # The registry-schema DDL memo (init-once-per-db, B4) must not leak across
    # tests: a test that deletes/replaces a db file another test path shared
    # would otherwise skip re-initialisation.
    from novafabric.registry.store import reset_schema_memo

    reset_schema_memo()


@pytest.fixture(autouse=True)
def _isolated_capture_recorder() -> Iterator[None]:
    """No test inherits, or bequeaths, a capture recorder (ADR-0254 Stage 0).

    ``capture.event_recorder`` keeps a **module-level** ``_current_recorder`` plus a
    ``ContextVar``. Neither was reset between tests, so a test that set a recorder and did
    not clear it left it visible to every later test in the same process. Measured
    2026-08-28 across ``tests/capture``, ``tests/test_capture_hooks.py`` and
    ``tests/adapters``: **113 tests finished with recorder state still set.**

    That is what made the suite un-subsettable. Under ``pytest-xdist`` the *selection*
    decides which tests share a worker, so changing the selection changes who inherits
    whose leftover recorder — which is why a reduced pre-merge selection failed three
    ``EventRecorder`` tests on one run and passed the identical command on the next.
    Speed work (test impact analysis, a smaller pre-merge tier, re-sharding) all works by
    changing the selection, so all of it depends on this fixture existing.

    Resetting **before** each test is the half that protects; resetting after keeps the
    report honest for anything that inspects state at session end.

    This is test hygiene, not a substitute for ADR-0224 phase 2 — production code still
    has a process-global recorder, and concurrent in-process captures still need the
    task-scoped fix.
    """
    from novafabric.capture import event_recorder as _er

    def _reset() -> None:
        _er._current_recorder = None
        _er._recorder_var.set(None)

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _default_noop_policy_engine(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Default the policy engine to NoopEngine (allow-all) — CI parity (TEST-OPA-1).

    ``get_policy_engine()`` returns a real ``OpaEngine`` iff the ``opa`` binary is on
    PATH. CI never installs opa, so gated operations run under ``NoopEngine``; but a
    dev machine *with* opa installed flips every un-seeded gated test to a policy
    DENY. We hide ``opa`` from ``shutil.which`` by default so the suite passes
    regardless of whether opa is installed locally.

    Unaffected (intentionally): tests that inject their own engine via
    ``patch("…service.get_policy_engine", …)`` and tests that construct
    ``OpaEngine()`` directly (which resolves ``opa`` through the OS, not
    ``shutil.which``). Opt out with ``@pytest.mark.real_opa``.
    """
    if request.node.get_closest_marker("real_opa"):
        yield
        return

    def _which(
        cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None
    ) -> str | None:
        if cmd == "opa":
            return None
        return _REAL_WHICH(cmd, mode, path)

    monkeypatch.setattr(shutil, "which", _which)
    yield


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def valid_model_yaml(fixtures_dir: Path) -> Path:
    return fixtures_dir / "valid_model.yaml"


@pytest.fixture
def valid_agent_yaml(fixtures_dir: Path) -> Path:
    return fixtures_dir / "valid_agent.yaml"


@pytest.fixture(scope="session", autouse=True)
def _materialise_promote_keys() -> None:
    """Mint the promote/seal fixture keys when they are absent.

    `.gitignore` carries a blanket `*.pem` — the right default, since it is what
    stops a real key being committed by accident. It also drops the six fixture
    keys under `tests/fixtures/promote/keys/`, so every `tests/promote` test
    failed on a clone of the public repository while passing for anyone with the
    untracked files already on disk (issue #24).

    `tests/fixtures/promote/generate_keys.py` has always been able to produce
    them; its docstring says "Run once". Nothing ever ran it, so a public clone
    got no keys and a confusing `EnvelopeError` instead. This runs it on demand.

    Allow-listing the six paths was the alternative, and it trades the default
    away: for a project whose premise is verifiable provenance, committing
    private-key material — even throwaway material — costs more to explain than
    it is worth. Files already present are left untouched, so a maintainer's
    working tree does not churn.

    **Exactly one process may generate.** ``scope="session"`` is per *worker*
    under pytest-xdist, so every worker runs this, and on CI the keys are always
    absent (they are untracked) — so every CI run raced. Two workers each saw
    "not all present" and both generated; the second overwrote the first's
    ``admin.pem`` between another test signing with the old key and verifying
    against the new one. The symptom was ``DSSE signature verification failed:
    signature mismatch`` in ``tests/promote``, hitting a *different* set of tests
    on each run — which is what a race looks like from the outside, and why it
    survived two green runs before showing up.

    So one worker wins an ``O_CREAT | O_EXCL`` lock and generates while the rest
    wait for the files instead of writing their own. No new dependency: an
    exclusive create is atomic on every filesystem this suite runs on.
    """
    keys = Path(__file__).parent / "fixtures" / "promote" / "keys"
    expected = [
        f"{role}{suffix}"
        for role in ("admin", "approver", "proposer")
        for suffix in (".pem", "_cert.pem")
    ]

    def _complete() -> bool:
        return all((keys / name).exists() for name in expected)

    if _complete():
        return

    keys.mkdir(parents=True, exist_ok=True)
    lock = keys / ".generating.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Another worker holds it. Wait for the keys rather than racing to write
        # them. Bounded, so a stale lock from a killed run reports itself instead
        # of hanging the suite until the 300 s pytest-timeout fires.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if _complete():
                return
            time.sleep(0.05)
        raise RuntimeError(
            f"timed out waiting for another worker to mint the promote keys in "
            f"{keys}; if a previous run was killed mid-generation, delete {lock}"
        )

    try:
        # Loaded by path rather than imported as `fixtures.promote.generate_keys`:
        # `pythonpath=tests` lets an `__init__.py` under tests/ shadow an installed
        # distribution of the same name, and `fixtures` is exactly that kind of name.
        import importlib.util  # noqa: PLC0415

        script = Path(__file__).parent / "fixtures" / "promote" / "generate_keys.py"
        spec = importlib.util.spec_from_file_location("_promote_generate_keys", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for role in module.NAMES:
            module.generate_key_and_cert(role)
    finally:
        os.close(fd)
        # Released only once the keys are on disk, so a waiter that sees the lock
        # disappear also sees a complete, self-consistent set.
        with contextlib.suppress(OSError):
            lock.unlink()
