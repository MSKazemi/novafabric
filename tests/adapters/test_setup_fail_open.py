"""A capture that cannot open its capsule never blocks the framework call.

Follow-up to review eval-adapters-query-04, which made teardown fail-open: the
setup half — ``mkdir`` of the store and ``CapsuleWriter.open()`` — still ran
unguarded in the shared core and in six of the own-body adapters, so an
unwritable or unusable capsule store raised NovaFabric's ``OSError`` from the
user's own ``kickoff()`` / ``forward()`` / ``run()`` and the call never ran.
``@agent``, ``a2a`` and ``google_adk`` already ran uncaptured instead. Now every
adapter opens its capsule through
:func:`novafabric.capture.teardown.open_capsule_writer`, which logs and counts the
failure and returns ``None``.

The store is made unusable by pointing ``data_dir`` at a regular file: ``mkdir``
then fails the same way for root and non-root.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import novafabric
from novafabric.capture.teardown import open_capsule_writer

SRC = Path(novafabric.__file__).resolve().parent


@pytest.fixture
def unusable_store(tmp_path: Path) -> Path:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the capsule store should be\n")
    return blocker


@pytest.fixture
def _no_real_hooks() -> Iterator[None]:
    with (
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        yield


def test_the_helper_returns_none_and_counts_the_failure(unusable_store: Path) -> None:
    with patch("novafabric.capture.teardown.note_capture_failure") as noted:
        assert open_capsule_writer(unusable_store, "01HXAY7M5JZ8R7K4P9DPBYK2WX", hook="x") is None
    (call,) = noted.call_args_list
    assert call.args[:2] == ("x", "capsule")


def test_the_helper_opens_a_writer_in_a_usable_store(tmp_path: Path) -> None:
    writer = open_capsule_writer(tmp_path / "store", "01HXAY7M5JZ8R7K4P9DPBYK2WX", hook="x")
    assert writer is not None and writer.capsule_dir.is_dir()


def test_the_helper_never_swallows_an_interrupt(tmp_path: Path) -> None:
    with patch("novafabric.capture.capsule.CapsuleWriter.open", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            open_capsule_writer(tmp_path, "01HXAY7M5JZ8R7K4P9DPBYK2WX", hook="x")


# ------------------------------------------------------- the shared core


def test_the_shared_core_runs_uncaptured(unusable_store: Path) -> None:
    from novafabric.adapters import _capsule

    cap = _capsule.begin_capture(framework="haystack", run_name="r", data_dir=unusable_store)
    cap.tags["entry_point"] = "run"
    cap.fail(ValueError("framework error"))
    cap.mark_partial("cancelled")
    cap.finish()  # every capture method is a no-op; nothing raises
    assert unusable_store.is_file()


# ---------------------------------------------------- the per-adapter copies

_WRAPPED = [
    ("autogen", "autogen", "wrap_agent", "initiate_chat"),
    ("crewai", "crewai", "wrap_crew", "kickoff"),
    ("dspy", "dspy", "wrap_program", "forward"),
]


@pytest.mark.usefixtures("_no_real_hooks")
@pytest.mark.parametrize(("module", "framework", "wrap", "method"), _WRAPPED)
def test_an_unusable_store_never_blocks_the_call(
    unusable_store: Path, module: str, framework: str, wrap: str, method: str
) -> None:
    target: Any = MagicMock()
    target.name = "t"
    getattr(target, method).return_value = {"reply": "done"}
    with patch.dict(sys.modules, {framework: MagicMock()}):
        adapter = __import__(f"novafabric.adapters.{module}", fromlist=[wrap])
        getattr(adapter, wrap)(target, run_name="r", data_dir=unusable_store)
        call = getattr(target, method)
        result = call(MagicMock()) if method == "initiate_chat" else call()
    assert result == {"reply": "done"}


@pytest.mark.usefixtures("_no_real_hooks")
def test_openai_agents_processor_runs_uncaptured(unusable_store: Path) -> None:
    from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor

    processor = NovaCapsuleTracingProcessor(unusable_store)
    trace = MagicMock(trace_id="t-1", workflow_name="wf")
    processor.on_trace_start(trace)
    processor.on_trace_end(trace)  # nothing was started, nothing to end


@pytest.mark.usefixtures("_no_real_hooks")
def test_bedrock_agentcore_invoke_runs_uncaptured(unusable_store: Path) -> None:
    from novafabric.adapters.bedrock_agentcore import _WrappedBedrockClient

    inner = MagicMock()
    inner.invoke_agent.return_value = {"completion": [], "sessionId": "s"}
    client = _WrappedBedrockClient(inner, unusable_store)
    assert client.invoke_agent(agentId="a")["sessionId"] == "s"
    inner.invoke_agent.assert_called_once_with(agentId="a")


# ------------------------------------------------------------- structural


def test_no_adapter_opens_a_capsule_writer_itself() -> None:
    """A new adapter cannot reintroduce the unguarded setup silently: no module
    under ``adapters/`` calls ``CapsuleWriter(...)`` — it goes through
    :func:`open_capsule_writer`."""
    offenders: list[str] = []
    for path in sorted((SRC / "adapters").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "CapsuleWriter"
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
