"""Wrapping a framework object never changes how it is called (review eval-adapters-query-19).

The CrewAI wrapper took ``**kwargs`` only, so ``crew.kickoff({"topic": "x"})`` (the
positional ``inputs``) raised ``TypeError``; the AutoGen wrapper took
``(recipient, **kwargs)``, so ``initiate_chat(recipient, True)`` failed the same way.
``crewai.Crew`` is a pydantic ``BaseModel``, which refuses ``crew.kickoff = ...``
(``object has no field "kickoff"``), so ``wrap_crew`` failed on every real crew. And
the async entry points (``kickoff_async``, ``a_initiate_chat``, DSPy's ``aforward``
behind ``acall``) were not wrapped at all.

The frameworks are not installed here: stand-ins with the frameworks' own signatures,
the CrewAI one a real pydantic model.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import BaseModel


@pytest.fixture(autouse=True)
def _frameworks_present() -> Iterator[None]:
    with (
        patch.dict(sys.modules, {"crewai": MagicMock(), "autogen": MagicMock(), "dspy": MagicMock()}),
        patch("novafabric.adapters._capsule.capture_environment", return_value={}),
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        yield


def _manifests(tmp_path: Path) -> list[dict[str, Any]]:
    return [yaml.safe_load(p.read_text()) for p in sorted(tmp_path.rglob("capsule.yaml"))]


# ── CrewAI ──────────────────────────────────────────────────────────────────


class Crew(BaseModel):
    """Stand-in for ``crewai.Crew``: a pydantic model, like the real one."""

    name: str = "crew"
    seen: list[Any] = []

    def kickoff(self, inputs: dict[str, Any] | None = None) -> str:
        self.seen.append(inputs)
        return f"done:{inputs}"

    async def kickoff_async(self, inputs: dict[str, Any] | None = None) -> str:
        self.seen.append(("async", inputs))
        return f"async:{inputs}"


def test_wrap_crew_works_on_a_pydantic_model(tmp_path: Path) -> None:
    from novafabric.adapters.crewai import wrap_crew

    crew = wrap_crew(Crew(), run_name="c", data_dir=tmp_path)
    assert crew.kickoff({"topic": "x"}) == "done:{'topic': 'x'}"  # positional inputs
    assert crew.kickoff(inputs={"topic": "y"}) == "done:{'topic': 'y'}"
    assert [m["status"] for m in _manifests(tmp_path)] == ["success", "success"]


def test_crew_kickoff_async_is_captured(tmp_path: Path) -> None:
    from novafabric.adapters.crewai import wrap_crew

    crew = wrap_crew(Crew(), run_name="c", data_dir=tmp_path)
    assert asyncio.run(crew.kickoff_async({"topic": "z"})) == "async:{'topic': 'z'}"
    (manifest,) = _manifests(tmp_path)
    assert manifest["metadata"]["entry_point"] == "kickoff_async"


def test_a_failing_kickoff_is_captured_as_failure(tmp_path: Path) -> None:
    from novafabric.adapters.crewai import wrap_crew

    class Failing(Crew):
        def kickoff(self, inputs: dict[str, Any] | None = None) -> str:
            raise RuntimeError("task failed")

    crew = wrap_crew(Failing(), run_name="c", data_dir=tmp_path)
    with pytest.raises(RuntimeError):
        crew.kickoff()
    (manifest,) = _manifests(tmp_path)
    assert (manifest["status"], manifest["exit_code"]) == ("failure", 1)


def test_an_async_failure_is_seen_after_the_await(tmp_path: Path) -> None:
    """The capsule is written when the coroutine ends, so its failure is recorded."""
    from novafabric.adapters.crewai import wrap_crew

    class FailingAsync(Crew):
        async def kickoff_async(self, inputs: dict[str, Any] | None = None) -> str:
            await asyncio.sleep(0)
            raise RuntimeError("async task failed")

    crew = wrap_crew(FailingAsync(), run_name="c", data_dir=tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(crew.kickoff_async())
    (manifest,) = _manifests(tmp_path)
    assert manifest["status"] == "failure"


def test_kickoff_async_running_kickoff_in_a_thread_is_one_capsule(tmp_path: Path) -> None:
    """CrewAI's kickoff_async runs kickoff via asyncio.to_thread, which copies the
    context: the nested kickoff records into the outer capsule, not a second one."""
    from novafabric.adapters.crewai import wrap_crew

    class Delegating(Crew):
        async def kickoff_async(self, inputs: dict[str, Any] | None = None) -> str:
            return await asyncio.to_thread(self.kickoff, inputs)

    crew = wrap_crew(Delegating(), run_name="c", data_dir=tmp_path)
    assert asyncio.run(crew.kickoff_async({"t": 1})) == "done:{'t': 1}"
    assert [m["metadata"]["entry_point"] for m in _manifests(tmp_path)] == ["kickoff_async"]


# ── AutoGen ─────────────────────────────────────────────────────────────────


class ConversableAgent:
    name = "assistant"

    def initiate_chat(
        self, recipient: Any, clear_history: bool = True, silent: bool | None = False, **kw: Any
    ) -> tuple[Any, bool, bool | None]:
        return recipient, clear_history, silent

    async def a_initiate_chat(
        self, recipient: Any, clear_history: bool = True, silent: bool | None = False, **kw: Any
    ) -> tuple[Any, bool, bool | None]:
        return recipient, clear_history, silent


def test_initiate_chat_keeps_its_positional_parameters(tmp_path: Path) -> None:
    from novafabric.adapters.autogen import wrap_agent

    agent = wrap_agent(ConversableAgent(), run_name="a", data_dir=tmp_path)
    assert agent.initiate_chat("bob", False, True) == ("bob", False, True)
    assert asyncio.run(agent.a_initiate_chat("bob", False)) == ("bob", False, False)
    entry_points = sorted(m["metadata"]["entry_point"] for m in _manifests(tmp_path))
    assert entry_points == ["a_initiate_chat", "initiate_chat"]


# ── DSPy ────────────────────────────────────────────────────────────────────


class Program:
    def forward(self, question: str) -> str:
        return f"answer:{question}"

    async def aforward(self, question: str) -> str:
        return f"async-answer:{question}"

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return self.forward(*args, **kwargs)

    async def acall(self, *args: Any, **kwargs: Any) -> str:
        return await self.aforward(*args, **kwargs)


def test_dspy_acall_is_captured(tmp_path: Path) -> None:
    from novafabric.adapters.dspy import wrap_program

    program = wrap_program(Program(), run_name="p", data_dir=tmp_path)
    assert program("q1") == "answer:q1"
    assert asyncio.run(program.acall("q2")) == "async-answer:q2"
    entry_points = sorted(m["metadata"]["entry_point"] for m in _manifests(tmp_path))
    assert entry_points == ["aforward", "forward"]
