"""``@agent`` records the workload's real outcome and never replaces it.

Review findings eval-adapters-query-03/-04:

* ``sys.exit(3)`` and Ctrl-C inside the decorated function escaped ``except
  Exception`` and the capsule said ``status: success, exit_code: 0``;
* an ``async def`` agent returned an un-awaited coroutine, so the capsule was
  finalised (and the hooks removed) before any agent code ran;
* a capture failure in setup or teardown (an unwritable store, ENOSPC while
  writing ``env.lock``) replaced the function's own return value or exception.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml
from _sdk_capsule import only_capsule

from novafabric.capture import record
from novafabric.sdk.agent import agent


def _manifest(root: Path) -> dict[str, Any]:
    loaded = yaml.safe_load((only_capsule(root) / "capsule.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


# --------------------------------------------------------------- the outcome


@pytest.mark.parametrize(("code", "exit_code", "status"), [(3, 3, "failure"), (0, 0, "success")])
def test_sys_exit_records_its_own_code(
    tmp_path: Path, code: int, exit_code: int, status: str
) -> None:
    @agent(name="quits", version="1.0", capsule_dir=tmp_path / "caps")
    def quits() -> None:
        raise SystemExit(code)

    with pytest.raises(SystemExit) as info:
        quits()
    assert info.value.code == code  # re-raised untouched
    manifest = _manifest(tmp_path / "caps")
    assert (manifest["status"], manifest["exit_code"]) == (status, exit_code)


def test_sys_exit_with_a_message_is_a_failure(tmp_path: Path) -> None:
    @agent(name="quits", version="1.0", capsule_dir=tmp_path / "caps")
    def quits() -> None:
        raise SystemExit("fatal: no config")

    with pytest.raises(SystemExit):
        quits()
    manifest = _manifest(tmp_path / "caps")
    assert (manifest["status"], manifest["exit_code"]) == ("failure", 1)
    assert manifest["error"]["type"] == "SystemExit"


def test_ctrl_c_is_recorded_as_interrupted_failure(tmp_path: Path) -> None:
    @agent(name="interrupted", version="1.0", capsule_dir=tmp_path / "caps")
    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        interrupted()
    manifest = _manifest(tmp_path / "caps")
    assert (manifest["status"], manifest["exit_code"]) == ("failure", 130)
    assert manifest["error"]["type"] == "KeyboardInterrupt"


# ------------------------------------------------------------------- async


def test_an_async_agent_stays_a_coroutine_function(tmp_path: Path) -> None:
    @agent(name="aio", version="1.0", capsule_dir=tmp_path / "caps")
    async def aio() -> int:
        return 1

    assert inspect.iscoroutinefunction(aio)
    assert not (tmp_path / "caps").exists()  # decorating captures nothing


def test_an_async_agent_is_captured_while_it_runs(tmp_path: Path) -> None:
    @record.tool
    def lookup(key: str) -> str:
        return key.upper()

    @agent(name="aio", version="1.0", capsule_dir=tmp_path / "caps")
    async def aio() -> str:
        await asyncio.sleep(0.01)
        return lookup("k")

    assert asyncio.run(aio()) == "K"
    manifest = _manifest(tmp_path / "caps")
    assert manifest["status"] == "success"
    # The tool call ran after the first await: it is in the capsule only if the
    # capture was still open, i.e. the wrapper awaited inside it.
    assert manifest["tool_call_count"] == 1
    assert manifest["duration_ms"] >= 10


def test_an_async_agent_that_raises_is_a_failure(tmp_path: Path) -> None:
    @agent(name="aio", version="1.0", capsule_dir=tmp_path / "caps")
    async def aio() -> None:
        await asyncio.sleep(0)
        raise ValueError("late")

    with pytest.raises(ValueError, match="late"):
        asyncio.run(aio())
    manifest = _manifest(tmp_path / "caps")
    assert (manifest["status"], manifest["exit_code"]) == ("failure", 1)
    assert manifest["error"]["type"] == "ValueError"


# -------------------------------------------- capture failures stay capture's


def _break_env_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(**_: Any) -> dict[str, Any]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("novafabric.capture.env.capture_environment", broken)


def test_a_teardown_failure_never_replaces_the_return_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_env_lock(monkeypatch)

    @agent(name="ok", version="1.0", capsule_dir=tmp_path / "caps")
    def ok() -> int:
        return 42

    assert ok() == 42


def test_a_teardown_failure_never_replaces_the_workload_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_env_lock(monkeypatch)

    @agent(name="bad", version="1.0", capsule_dir=tmp_path / "caps")
    def bad() -> None:
        raise ValueError("the user's own error")

    with pytest.raises(ValueError, match="the user's own error"):
        bad()


def test_a_missing_package_version_never_breaks_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(_: str) -> str:
        raise PackageNotFoundError("novafabric")

    monkeypatch.setattr("novafabric.capture.teardown._pkg_version", missing)

    @agent(name="ok", version="1.0", capsule_dir=tmp_path / "caps")
    def ok() -> int:
        return 7

    assert ok() == 7
    assert _manifest(tmp_path / "caps")["novafabric_version"] == "unknown"


def test_an_unwritable_capsule_store_still_runs_the_workload(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.write_text("a file where the store directory should be")
    calls: list[int] = []

    @agent(name="ok", version="1.0", capsule_dir=store)
    def ok() -> int:
        calls.append(1)
        return 5

    assert ok() == 5
    assert calls == [1]  # ran exactly once, uncaptured


def test_an_invalid_explicit_tag_still_raises_before_the_workload(tmp_path: Path) -> None:
    """Fail-open covers capture I/O, not a programmer error the docs promise to raise."""
    calls: list[int] = []

    @agent(name="ok", version="1.0", capsule_dir=tmp_path / "caps", session_id="not-a-ulid")
    def ok() -> None:
        calls.append(1)

    with pytest.raises(ValueError):
        ok()
    assert calls == []


def test_a_teardown_failure_is_recorded_and_the_capsule_left_unsealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open is not fail-silent: the capsule says what is missing and is never
    signed, so its counts and digests are not vouched for."""
    _break_env_lock(monkeypatch)
    sealed: list[Path] = []
    monkeypatch.setattr(
        "novafabric.capture.finalize.seal_capsule",
        lambda capsule_dir, _m: sealed.append(capsule_dir),
    )

    @agent(name="ok", version="1.0", capsule_dir=tmp_path / "caps")
    def ok() -> None:
        pass

    ok()
    manifest = _manifest(tmp_path / "caps")
    assert manifest["status"] == "success"  # the workload's outcome stands
    assert manifest["metadata"]["finalization_error"] == "capture incomplete: env.lock: OSError"
    assert sealed == []
    assert not (only_capsule(tmp_path / "caps") / ".seal").exists()
