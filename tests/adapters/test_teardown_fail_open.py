"""A capture failure in an adapter's teardown never replaces the framework call's outcome.

Review finding eval-adapters-query-04: every in-process capsule writer ran its
teardown (root span, ``env.lock``, ``replay.yaml``, counts, version lookup) from
a ``finally:`` with no guard, so ENOSPC — or a source checkout without package
metadata — surfaced as NovaFabric's exception instead of the caller's result or
own error. A failure that aborted teardown also skipped finalization, leaving
the raw streams unscanned on disk.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

import novafabric
from novafabric.capture.teardown import TeardownSteps

SRC = Path(novafabric.__file__).resolve().parent


def _manifest_of(root: Path) -> dict[str, Any]:
    found = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    assert len(found) == 1, found
    loaded = yaml.safe_load((found[0] / "capsule.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _enospc(**_: Any) -> dict[str, Any]:
    raise OSError(28, "No space left on device")


# ------------------------------------------------------------- TeardownSteps


def test_steps_return_the_result_or_the_default_and_collect_failures() -> None:
    steps = TeardownSteps("adapter-test")
    assert steps.run("ok", lambda: 3, 0) == 3
    assert steps.incomplete_reason is None

    def boom() -> int:
        raise OSError("disk")

    assert steps.run("counts", boom, 0) == 0
    steps.do("env.lock", lambda: 1 / 0)
    assert steps.failed == ["counts: OSError", "env.lock: ZeroDivisionError"]
    assert steps.incomplete_reason == (
        "capture incomplete: counts: OSError, env.lock: ZeroDivisionError"
    )


def test_steps_never_swallow_an_interrupt() -> None:
    """Fail-open covers capture errors, not the user pressing Ctrl-C mid-teardown."""

    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        TeardownSteps("adapter-test").do("trace", interrupted)


# ------------------------------------------------------- the shared core


def test_the_shared_core_finishes_and_records_a_failed_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.adapters import _capsule

    monkeypatch.setattr(_capsule, "capture_environment", _enospc)
    cap = _capsule.begin_capture(framework="haystack", run_name="r", data_dir=tmp_path)
    cap.finish()  # must not raise
    manifest = _manifest_of(tmp_path)
    assert manifest["status"] == "success"
    assert manifest["metadata"]["finalization_error"] == "capture incomplete: env.lock: OSError"


# ---------------------------------------- adapters wrapping entry points on the core
# autogen, crewai and dspy carried their own copy of the capsule body until
# eval-adapters-query-19 moved them onto adapters/_entry_points.py; teardown runs
# in the shared core, so that is where the env.lock write is made to fail.

from novafabric.adapters import _capsule as _capsule_core  # noqa: E402

_LEGACY = [
    ("autogen", "autogen", "wrap_agent", "initiate_chat"),
    ("crewai", "crewai", "wrap_crew", "kickoff"),
    ("dspy", "dspy", "wrap_program", "forward"),
]


@pytest.fixture
def _no_real_hooks() -> Iterator[None]:
    with (
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        yield


@pytest.mark.usefixtures("_no_real_hooks")
@pytest.mark.parametrize(("module", "framework", "wrap", "method"), _LEGACY)
def test_a_teardown_failure_never_replaces_the_return_value(
    tmp_path: Path, module: str, framework: str, wrap: str, method: str
) -> None:
    target: Any = MagicMock()
    target.name = "t"
    getattr(target, method).return_value = {"reply": "done"}
    with patch.dict(sys.modules, {framework: MagicMock()}):
        adapter = __import__(f"novafabric.adapters.{module}", fromlist=[wrap])
        with patch.object(_capsule_core, "capture_environment", _enospc):
            getattr(adapter, wrap)(target, run_name="r", data_dir=tmp_path)
            call = getattr(target, method)
            result = call(MagicMock()) if method == "initiate_chat" else call()
    assert result == {"reply": "done"}
    assert _manifest_of(tmp_path)["metadata"]["finalization_error"] == (
        "capture incomplete: env.lock: OSError"
    )


@pytest.mark.usefixtures("_no_real_hooks")
@pytest.mark.parametrize(("module", "framework", "wrap", "method"), _LEGACY)
def test_a_teardown_failure_never_replaces_the_framework_exception(
    tmp_path: Path, module: str, framework: str, wrap: str, method: str
) -> None:
    target: Any = MagicMock()
    target.name = "t"
    getattr(target, method).side_effect = ValueError("the framework's own error")
    with patch.dict(sys.modules, {framework: MagicMock()}):
        adapter = __import__(f"novafabric.adapters.{module}", fromlist=[wrap])
        with patch.object(_capsule_core, "capture_environment", _enospc):
            getattr(adapter, wrap)(target, run_name="r", data_dir=tmp_path)
            call = getattr(target, method)
            with pytest.raises(ValueError, match="the framework's own error"):
                call(MagicMock()) if method == "initiate_chat" else call()
    assert _manifest_of(tmp_path)["status"] == "failure"


# ------------------------------------------------------------- structural


def _finalize_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "finalize_in_process_capsule"
        ):
            yield node


def test_every_in_process_writer_reports_failed_teardown_steps() -> None:
    """A new adapter cannot reintroduce the unguarded teardown silently: every
    ``finalize_in_process_capsule`` call that runs inside the user's own call path
    (``sdk/`` and ``adapters/``) hands over a :class:`TeardownSteps`
    ``incomplete_reason``. OTLP ingest is not in scope: it is an explicit import
    command, so a write failure there must surface, not degrade."""
    sites: list[str] = []
    missing: list[str] = []
    paths = sorted([*(SRC / "sdk").rglob("*.py"), *(SRC / "adapters").rglob("*.py")])
    for path in paths:
        for call in _finalize_calls(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.relative_to(SRC)}:{call.lineno}"
            sites.append(where)
            if "incomplete_reason" not in {k.arg for k in call.keywords}:
                missing.append(where)
    assert len(sites) >= 6, sites  # the SDK, the shared core and the four own-body adapters
    assert missing == []
