"""Configured WORM state that cannot be read refuses deletion (fail closed).

Attack (PR #158, BL-063): every destructive path treated a ``worm.db`` it could
not read as "no WORM locks" and went ahead.

* The governed delete core (``nova capsule delete``, ``DELETE /v0/capsules``, bulk
  delete, serve's ``DELETE /api/runs``, metadata-index delete) logged a warning
  for an unreadable database and deleted.
* Every reader opened the database read-write through ``LocalWormAdapter``, whose
  constructor runs ``CREATE TABLE IF NOT EXISTS``. A ``worm.db`` truncated to zero
  bytes, or replaced by any SQLite file without the table, was silently turned
  into a valid *empty* WORM database, so the locks it had held were gone and both
  the delete core and ``nova retention apply`` deleted.
* A ``worm.db`` that is a dangling symbolic link or a directory was not even
  found by ``nova retention``.
* The delete core read only ``<capsule_dir>/../registries``, so a lock in
  ``$NOVAFABRIC_HOME/registries/<r>/worm.db`` (where ``nova retention`` reads it)
  did not stop ``nova capsule delete --capsule-dir <elsewhere>``.

Pinned here: a ``worm.db`` entry that exists in any registry location is
*configured* WORM state. If it cannot be read (garbage, empty, no table, a bad
``locked_until`` row, a directory, a dangling link, permission denied, locked by
another writer), every destructive path refuses with ``worm_state_unavailable``
and deletes nothing. Readers never write to the database. No ``worm.db`` entry
anywhere means no local WORM is configured: deletion proceeds as before.
An unreadable ``retention-policy.yaml`` refuses with a named error too.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.server import capsule_delete
from novafabric.storage import _local_worm
from novafabric.storage._local_worm import LocalWormAdapter

runner = CliRunner()
REGISTRY = "prod"
RUN = "01M5JDD2Y0VKD0XK5YZ4CZNND8"
OLD = datetime.now(tz=timezone.utc) - timedelta(days=400)

_POLICY = """
version: 1
registry: prod
retention_days: 30
deletion_mode: defensible
bindings:
  - id: purge-old
    match:
      age: P30D
    window: P90D
    action: purge
"""

#: Every way a configured ``worm.db`` can be unreadable that we defend against.
KINDS = [
    "garbage",
    "empty",
    "no_table",
    "bad_timestamp",
    "naive_timestamp",
    "directory",
    "dangling_symlink",
    "permission_denied",
    "locked",
]


@pytest.fixture(autouse=True)
def _fast_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked database is reported after 50 ms instead of the production wait."""
    monkeypatch.setattr(_local_worm, "READ_BUSY_TIMEOUT_S", 0.05, raising=False)


@pytest.fixture
def break_worm() -> Iterator[Callable[[Path, str], None]]:
    """Make ``<path>`` an unreadable WORM database of the given kind (undone on teardown)."""
    undo: list[Callable[[], None]] = []

    def make(db: Path, kind: str) -> None:
        db.parent.mkdir(parents=True, exist_ok=True)
        if kind == "garbage":
            db.write_bytes(b"this is not a sqlite database " * 8)
        elif kind == "empty":
            db.write_bytes(b"")
        elif kind == "no_table":
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE unrelated (x TEXT)")
            conn.commit()
            conn.close()
        elif kind in ("bad_timestamp", "naive_timestamp"):
            LocalWormAdapter(db).put(RUN, b"x", retention_days=3650)
            value = "not-a-date" if kind == "bad_timestamp" else "2999-01-01T00:00:00"
            conn = sqlite3.connect(db)
            conn.execute("UPDATE worm_capsules SET locked_until = ?", (value,))
            conn.commit()
            conn.close()
        elif kind == "directory":
            db.mkdir()
        elif kind == "dangling_symlink":
            db.symlink_to(db.parent / "moved-away.db")
        elif kind == "permission_denied":
            LocalWormAdapter(db).put(RUN, b"x", retention_days=3650)
            os.chmod(db, 0)
            undo.append(lambda: os.chmod(db, 0o600))
        elif kind == "locked":
            LocalWormAdapter(db).put(RUN, b"x", retention_days=3650)
            holder = sqlite3.connect(db, isolation_level=None)
            holder.execute("BEGIN EXCLUSIVE")
            undo.append(holder.close)
        else:  # pragma: no cover - test bug
            raise AssertionError(kind)

    yield make
    for fn in reversed(undo):
        fn()


class WS:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.cwd = tmp_path / "cwd"
        self.capsules = self.home / "capsules"
        self.audit = tmp_path / "audit.jsonl"

    def registry(self, root: Path | None = None, registry: str = REGISTRY) -> Path:
        d = (root or self.home) / "registries" / registry
        d.mkdir(parents=True, exist_ok=True)
        return d

    def legacy_root(self) -> Path:
        return self.cwd / ".novafabric"

    def capsule(self, run_id: str = RUN, base: Path | None = None) -> Path:
        d = (base or self.capsules) / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "capsule.yaml").write_text(f"run_id: {run_id}\n")
        return d

    def due_capsule(self, run_id: str = RUN) -> Path:
        """A ``capsule.json`` capsule that the purge binding finds due."""
        d = self.capsules / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "capsule.json").write_text(
            json.dumps({"run_id": run_id, "created_at": OLD.isoformat()})
        )
        return d

    def audit_events(self) -> list[dict[str, Any]]:
        if not self.audit.exists():
            return []
        return [json.loads(x) for x in self.audit.read_text().splitlines() if x.strip()]


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WS:
    w = WS(tmp_path)
    w.cwd.mkdir()
    monkeypatch.chdir(w.cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(w.home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(w.audit))
    return w


def _invoke(*args: str) -> Any:
    res = runner.invoke(app, list(args), env={"COLUMNS": "400"})
    # Exit 1 from a traceback is not a refusal (CliRunner reports both as 1).
    assert res.exception is None or isinstance(res.exception, SystemExit), (
        repr(res.exception),
        res.output,
    )
    return res


# ---------------------------------------------------------------------------
# The governed delete core (CLI delete, /v0 single + bulk, serve, index delete)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_core_refuses_when_configured_worm_db_is_unreadable(
    ws: WS, kind: str, break_worm: Callable[[Path, str], None]
) -> None:
    ws.capsule()
    db = ws.registry() / "worm.db"
    break_worm(db, kind)
    with pytest.raises(capsule_delete.DeleteBlockedError) as ei:
        capsule_delete.check_deletable(ws.capsules, RUN)
    assert ei.value.code == "worm_state_unavailable"
    assert ei.value.details["worm_db"] == str(db)
    assert "worm_state_unavailable" in str(ei.value)


def test_core_refuses_even_with_the_sealed_delete_override(
    ws: WS, break_worm: Callable[[Path, str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.capsule()
    break_worm(ws.registry() / "worm.db", "garbage")
    monkeypatch.setenv(capsule_delete.ALLOW_SEALED_ENV, "1")
    with pytest.raises(capsule_delete.DeleteBlockedError) as ei:
        capsule_delete.check_deletable(ws.capsules, RUN, allow_sealed=True)
    assert ei.value.code == "worm_state_unavailable"


def test_unreadable_registry_directory_refuses(ws: WS) -> None:
    """A registry directory that cannot be searched may hide a worm.db: refuse."""
    ws.capsule()
    reg = ws.registry()
    LocalWormAdapter(reg / "worm.db").put(RUN, b"x", retention_days=3650)
    os.chmod(reg, 0)
    try:
        with pytest.raises(capsule_delete.DeleteBlockedError) as ei:
            capsule_delete.check_deletable(ws.capsules, RUN)
    finally:
        os.chmod(reg, 0o755)
    assert ei.value.code == "worm_state_unavailable"


def test_lock_in_home_registry_blocks_delete_from_another_capsule_dir(ws: WS) -> None:
    """The core reads the same worm.db locations as ``nova retention``."""
    elsewhere = ws.cwd / "other-store" / "capsules"
    ws.capsule(base=elsewhere)
    LocalWormAdapter(ws.registry() / "worm.db").put(RUN, b"x", retention_days=3650)
    with pytest.raises(capsule_delete.DeleteBlockedError) as ei:
        capsule_delete.check_deletable(elsewhere, RUN)
    assert ei.value.code == "worm_hold"


def test_reading_never_writes_to_the_worm_db(ws: WS) -> None:
    """A reader must not repair an emptied database into a valid empty one."""
    db = ws.registry() / "worm.db"
    db.write_bytes(b"")
    with pytest.raises(capsule_delete.DeleteBlockedError):
        capsule_delete.check_deletable(ws.capsules, RUN)
    assert db.stat().st_size == 0


@pytest.mark.parametrize("setup", ["no_registries", "registry_without_worm_db", "empty_worm"])
def test_no_or_empty_worm_configured_still_allows_delete(ws: WS, setup: str) -> None:
    ws.capsule()
    if setup == "registry_without_worm_db":
        ws.registry()
    elif setup == "empty_worm":
        LocalWormAdapter(ws.registry() / "worm.db")  # valid database, no locks
    capsule_delete.check_deletable(ws.capsules, RUN)  # does not raise


def test_expired_lock_still_allows_delete(ws: WS) -> None:
    ws.capsule()
    db = ws.registry() / "worm.db"
    LocalWormAdapter(db).put(RUN, b"x", retention_days=1)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE worm_capsules SET locked_until = ?",
        ((datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat(),),
    )
    conn.commit()
    conn.close()
    capsule_delete.check_deletable(ws.capsules, RUN)


# ---------------------------------------------------------------------------
# nova capsule delete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["garbage", "empty", "dangling_symlink", "permission_denied"])
@pytest.mark.parametrize("force", [False, True])
def test_cli_delete_refuses_on_unreadable_worm_db(
    ws: WS, kind: str, force: bool, break_worm: Callable[[Path, str], None]
) -> None:
    cap = ws.capsule()
    break_worm(ws.registry() / "worm.db", kind)
    res = _invoke("capsule", "delete", RUN, "--registry", REGISTRY, *(["--force"] if force else []))
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert "Deleted" not in res.output
    assert cap.is_dir()
    types = [e["event_type"] for e in ws.audit_events()]
    assert "capsule.delete" not in types
    assert types == ["run.index_delete_refused"]


def test_cli_delete_from_another_capsule_dir_reads_the_home_worm_db(
    ws: WS, break_worm: Callable[[Path, str], None]
) -> None:
    elsewhere = ws.cwd / "other-store" / "capsules"
    cap = ws.capsule(base=elsewhere)
    break_worm(ws.registry() / "worm.db", "garbage")
    res = _invoke("capsule", "delete", RUN, "--registry", REGISTRY, "--capsule-dir", str(elsewhere))
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert cap.is_dir()


def test_cli_delete_reads_the_legacy_cwd_worm_db(
    ws: WS, break_worm: Callable[[Path, str], None]
) -> None:
    cap = ws.capsule()
    break_worm(ws.registry(ws.legacy_root()) / "worm.db", "empty")
    res = _invoke("capsule", "delete", RUN, "--registry", REGISTRY)
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert cap.is_dir()


def test_cli_delete_without_any_worm_db_still_deletes(ws: WS) -> None:
    cap = ws.capsule()
    ws.registry()  # a registry directory alone configures nothing
    res = _invoke("capsule", "delete", RUN, "--registry", REGISTRY)
    assert res.exit_code == 0, res.output
    assert not cap.exists()


@pytest.mark.parametrize("kind", ["malformed_yaml", "permission_denied", "dangling_symlink"])
def test_cli_delete_refuses_on_unreadable_policy(ws: WS, kind: str) -> None:
    cap = ws.capsule()
    policy = ws.registry(ws.legacy_root()) / "retention-policy.yaml"
    if kind == "malformed_yaml":
        policy.write_text("registry: [unclosed\n  retention_days: : :\n")
    elif kind == "permission_denied":
        policy.write_text("registry: prod\nretention_days: 1\n")
        os.chmod(policy, 0)
    else:
        policy.symlink_to(policy.parent / "moved-away.yaml")
    try:
        res = _invoke("capsule", "delete", RUN, "--registry", REGISTRY, "--force")
    finally:
        if kind == "permission_denied":
            os.chmod(policy, 0o600)
    assert res.exit_code == 1, res.output
    assert "retention_policy_unavailable" in res.output
    assert cap.is_dir()


# ---------------------------------------------------------------------------
# nova retention apply / plan
# ---------------------------------------------------------------------------


def _policy(ws: WS, root: Path | None = None, body: str = _POLICY) -> Path:
    p = ws.registry(root) / "retention-policy.yaml"
    p.write_text(body)
    return p


@pytest.mark.parametrize("kind", KINDS)
def test_apply_refuses_on_unreadable_worm_db(
    ws: WS, kind: str, break_worm: Callable[[Path, str], None]
) -> None:
    cap = ws.due_capsule()
    _policy(ws)
    break_worm(ws.registry() / "worm.db", kind)
    res = _invoke("retention", "apply", "--registry", REGISTRY, "--yes")
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert cap.is_dir()
    assert not any(e["event_type"].startswith("retention") for e in ws.audit_events())


def test_plan_refuses_on_unreadable_worm_db(
    ws: WS, break_worm: Callable[[Path, str], None]
) -> None:
    ws.due_capsule()
    _policy(ws)
    break_worm(ws.registry() / "worm.db", "empty")
    res = _invoke("retention", "plan", "--registry", REGISTRY, "--json")
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output


def test_apply_refuses_when_explicit_worm_db_is_missing(ws: WS, tmp_path: Path) -> None:
    cap = ws.due_capsule()
    _policy(ws)
    res = _invoke(
        "retention", "apply", "--registry", REGISTRY, "--yes",
        "--worm-db", str(tmp_path / "typo" / "worm.db"),
    )
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert cap.is_dir()
    assert not (tmp_path / "typo").exists()  # the reader created nothing


def test_apply_refuses_when_explicit_worm_db_is_unreadable(
    ws: WS, tmp_path: Path, break_worm: Callable[[Path, str], None]
) -> None:
    cap = ws.due_capsule()
    _policy(ws)
    db = tmp_path / "ops" / "worm.db"
    break_worm(db, "garbage")
    res = _invoke("retention", "apply", "--registry", REGISTRY, "--yes", "--worm-db", str(db))
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert cap.is_dir()


def test_apply_without_any_worm_db_still_purges(ws: WS) -> None:
    cap = ws.due_capsule()
    _policy(ws)
    res = _invoke("retention", "apply", "--registry", REGISTRY, "--yes")
    assert res.exit_code == 0, res.output
    assert not cap.exists()


def test_worm_db_emptied_at_the_prompt_is_caught_by_the_apply_time_recheck(
    ws: WS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truncating worm.db while the operator confirms must not erase its locks."""
    cap = ws.due_capsule()
    _policy(ws)
    db = ws.registry() / "worm.db"
    LocalWormAdapter(db).put("some-other-capsule", b"x", retention_days=3650)

    def confirm_while_the_db_is_emptied(*_: object, **__: object) -> bool:
        db.write_bytes(b"")
        return True

    monkeypatch.setattr("typer.confirm", confirm_while_the_db_is_emptied)
    res = _invoke("retention", "apply", "--registry", REGISTRY, "--json")
    assert cap.is_dir(), res.output
    held = json.loads(res.stdout[res.stdout.index("{") :])["held"]  # after the prompt text
    assert [r["outcome"] for r in held] == ["skipped"]
    assert held[0]["reason"].startswith("worm_state_unavailable")


@pytest.mark.parametrize("kind", ["malformed_yaml", "permission_denied", "dangling_symlink"])
def test_apply_refuses_on_unreadable_policy_copy(ws: WS, kind: str) -> None:
    """A policy copy that cannot be read may say ``deletion_mode: prohibited``."""
    cap = ws.due_capsule()
    _policy(ws)  # a readable canonical policy with a due purge binding
    legacy = ws.registry(ws.legacy_root()) / "retention-policy.yaml"
    if kind == "malformed_yaml":
        legacy.write_text("registry: [unclosed\n  deletion_mode: : :\n")
    elif kind == "permission_denied":
        legacy.write_text(_POLICY)
        os.chmod(legacy, 0)
    else:
        legacy.symlink_to(legacy.parent / "moved-away.yaml")
    try:
        res = _invoke("retention", "apply", "--registry", REGISTRY, "--yes")
    finally:
        if kind == "permission_denied":
            os.chmod(legacy, 0o600)
    assert res.exit_code == 1, res.output
    assert "retention_policy_unavailable" in res.output
    assert cap.is_dir()
