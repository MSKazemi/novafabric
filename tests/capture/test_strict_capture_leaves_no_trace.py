"""A strict-mode refusal must leave nothing behind, in every adapter.

``NOVAFABRIC_CAPTURE_STRICT=1`` makes an overlapping capture raise
``ConcurrentCaptureRefused``. The hooks stay untouched (pinned by
``test_strict_capture.py``); this file pins the other half: the capsule
directory an adapter had already opened must not survive the refusal, because
a refused run must leave no trace and capsules are append-only.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.capture import hooks
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import get_current_writer


@pytest.fixture(autouse=True)
def _owner_holds_the_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path]]:
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    w = CapsuleWriter(run_id="OWNER", base_dir=tmp_path / "owner")
    w.open()
    token = hooks.install_all(writer=w, parent_span_id="a" * 16)
    monkeypatch.setenv(hooks.STRICT_ENV_VAR, "1")
    yield token, tmp_path / "runs"
    monkeypatch.delenv(hooks.STRICT_ENV_VAR, raising=False)
    hooks.uninstall_all(token)
    hooks.uninstall_all()
    hooks._contended_owners.clear()
    assert get_current_writer(None) is None


def _fake(name: str) -> Any:
    mods: dict[str, Any] = {name: MagicMock()}
    if name == "llama_index.core":
        mods["llama_index"] = MagicMock()
    return patch.dict(sys.modules, mods)


# Each driver triggers one capture attempt against ``d``.
def _crewai(d: Path) -> None:
    crew: Any = MagicMock()
    with _fake("crewai"):
        from novafabric.adapters.crewai import wrap_crew

        wrap_crew(crew, data_dir=d)
        crew.kickoff()


def _autogen(d: Path) -> None:
    agent: Any = MagicMock()
    with _fake("autogen"):
        from novafabric.adapters.autogen import wrap_agent

        wrap_agent(agent, data_dir=d)
        agent.initiate_chat(MagicMock(), message="hi")


def _dspy(d: Path) -> None:
    prog: Any = MagicMock()
    with _fake("dspy"):
        from novafabric.adapters.dspy import wrap_program

        wrap_program(prog, data_dir=d)
        prog.forward()


def _langgraph_invoke(d: Path) -> None:
    with _fake("langgraph"):
        from novafabric.adapters.langgraph import wrap

        wrap(MagicMock(), data_dir=d).invoke({})


def _langgraph_stream(d: Path) -> None:
    with _fake("langgraph"):
        from novafabric.adapters.langgraph import wrap

        next(iter(wrap(MagicMock(), data_dir=d).stream({})))


def _haystack(d: Path) -> None:
    p: Any = MagicMock(spec=["run"])
    with _fake("haystack"):
        from novafabric.adapters.haystack import wrap_pipeline

        wrap_pipeline(p, data_dir=d)
        p.run({})


def _llamaindex(d: Path) -> None:
    e: Any = MagicMock(spec=["query"])
    with _fake("llama_index.core"):
        from novafabric.adapters.llamaindex import wrap_engine

        wrap_engine(e, data_dir=d)
        e.query("q")


def _pydantic_ai(d: Path) -> None:
    a: Any = MagicMock(spec=["run_sync", "name"])
    with _fake("pydantic_ai"):
        from novafabric.adapters.pydantic_ai import wrap_agent

        wrap_agent(a, data_dir=d)
        a.run_sync("q")


def _a2a(d: Path) -> None:
    from novafabric.adapters.a2a import NovaA2AInterceptor

    args = SimpleNamespace(method="send_message", agent_card=SimpleNamespace(name="x"), input={})
    asyncio.run(NovaA2AInterceptor(d).before(args))


def _openai_agents(d: Path) -> None:
    from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor

    NovaCapsuleTracingProcessor(d).on_trace_start(SimpleNamespace(trace_id="t1"))


def _bedrock(d: Path) -> None:
    from novafabric.adapters.bedrock_agentcore import wrap_client

    wrap_client(MagicMock(), data_dir=d).invoke_agent(agentId="a")


def _google_adk(d: Path) -> None:
    from novafabric.adapters.google_adk import NovaAdkPlugin

    plugin = NovaAdkPlugin(d)
    asyncio.run(
        plugin.before_run_callback(invocation_context=SimpleNamespace(invocation_id="x"))
    )


DRIVERS: list[Callable[[Path], None]] = [
    _crewai, _autogen, _dspy, _langgraph_invoke, _langgraph_stream, _haystack,
    _llamaindex, _pydantic_ai, _a2a, _openai_agents, _bedrock, _google_adk,
]


@pytest.mark.parametrize("drive", DRIVERS, ids=lambda f: f.__name__.lstrip("_"))
def test_refusal_leaves_no_capsule_dir_and_no_hook_state(
    drive: Callable[[Path], None], _owner_holds_the_hooks: tuple[str, Path]
) -> None:
    token, data_dir = _owner_holds_the_hooks
    bindings, installed = dict(hooks._scope_bindings), list(hooks._installed)

    with pytest.raises(hooks.ConcurrentCaptureRefused):
        drive(data_dir)

    leftovers = sorted(p.name for p in data_dir.rglob("*")) if data_dir.exists() else []
    assert leftovers == []
    assert hooks._scope_bindings == bindings
    assert hooks._installed == installed
    assert hooks.current_hook_owner() == token
    assert hooks.wire_capture_state(token) == "installed"  # owner not marked contended


def test_a_non_empty_capsule_dir_is_never_discarded(tmp_path: Path) -> None:
    w = CapsuleWriter(run_id="X", base_dir=tmp_path)
    w.open()
    w.append_trace_span({"span_id": "s"})
    with pytest.raises(hooks.ConcurrentCaptureRefused):
        hooks.install_all_or_discard(w, "b" * 16)
    assert w.capsule_dir.exists()


def test_default_mode_still_participates_and_keeps_its_dir(
    _owner_holds_the_hooks: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _token, data_dir = _owner_holds_the_hooks
    monkeypatch.delenv(hooks.STRICT_ENV_VAR)
    w = CapsuleWriter(run_id="P", base_dir=data_dir)
    w.open()
    t = hooks.install_all_or_discard(w, "c" * 16)
    assert t.startswith("par:")
    assert w.capsule_dir.exists()
    hooks.uninstall_all(t)
