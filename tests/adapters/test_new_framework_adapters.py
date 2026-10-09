"""Adapters for LlamaIndex, Pydantic AI, and Haystack (issues #1, #2, #3).

None of the three frameworks is installed here, and none is a NovaFabric
dependency, so each is faked through ``sys.modules`` exactly as the existing
adapter tests fake dspy. What is *not* faked is the capsule machinery: these
tests exercise the real ``CapsuleWriter`` and the real manifest writer, so a
capsule that would fail ``nova validate`` fails here first.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import gc
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml


def _fake(name: str) -> Any:
    """A stand-in module tree for a framework that is not installed."""
    mods = {name: MagicMock()}
    if name == "llama_index.core":
        mods["llama_index"] = MagicMock()
    return patch.dict(sys.modules, mods)


def _no_hooks() -> Any:
    """Neutralise the process-wide wire hooks; the capsule body is the subject."""
    return patch.multiple(
        "novafabric.capture.hooks",
        install_all=MagicMock(return_value="own:test"),
        uninstall_all=MagicMock(),
        wire_capture_state=MagicMock(return_value="installed"),
    )


def _quiet_capsule() -> Any:
    """Stub the environment snapshot, the one heavyweight helper the writer calls.

    The secret scanner is not stubbed: since 2026-10-09 the capsule is finalized
    through the shared path (``capture/finalize.py``), which runs the real scan.
    """
    return patch.multiple(
        "novafabric.adapters._capsule",
        capture_environment=MagicMock(return_value={}),
    )


def _manifests(tmp_path: Path) -> list[dict]:
    return [
        yaml.safe_load(p.read_text()) for p in sorted(tmp_path.glob("*/capsule.yaml"))
    ]


def _sole_manifest(tmp_path: Path) -> dict:
    found = _manifests(tmp_path)
    assert len(found) == 1, f"expected exactly one capsule, got {len(found)}"
    return found[0]


# --------------------------------------------------------------------------
# Import errors — the contract every adapter shares
# --------------------------------------------------------------------------

class TestImportErrors:
    @pytest.mark.parametrize(
        ("module", "func", "missing", "match"),
        [
            ("llamaindex", "wrap_engine", "llama_index.core", "llama_index"),
            ("pydantic_ai", "wrap_agent", "pydantic_ai", "pydantic_ai"),
            ("haystack", "wrap_pipeline", "haystack", "haystack"),
        ],
    )
    def test_raises_when_the_framework_is_absent(
        self, module: str, func: str, missing: str, match: str
    ) -> None:
        import importlib

        mod = importlib.import_module(f"novafabric.adapters.{module}")
        with patch.dict(sys.modules, {missing: None}):
            with pytest.raises(ImportError, match=match):
                getattr(mod, func)(MagicMock())

    def test_the_error_names_the_install_command(self) -> None:
        """A first-time user's next action must be in the message."""
        from novafabric.adapters.haystack import wrap_pipeline

        with patch.dict(sys.modules, {"haystack": None}):
            with pytest.raises(ImportError, match="pip install haystack-ai"):
                wrap_pipeline(MagicMock())


# --------------------------------------------------------------------------
# LlamaIndex
# --------------------------------------------------------------------------

class TestLlamaIndex:
    def test_query_engine_run_writes_a_capsule(self, tmp_path: Path) -> None:
        engine: Any = MagicMock(spec=["query"])
        engine.query.return_value = "an answer"

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, run_name="rag", data_dir=tmp_path)
            assert engine.query("what changed?") == "an answer"

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "llamaindex"
        assert manifest["metadata"]["entry_point"] == "query"
        assert manifest["metadata"]["wire_capture"] == "installed"
        assert manifest["command"] == ["@llamaindex:rag"]

    def test_chat_engine_is_detected_when_there_is_no_query(
        self, tmp_path: Path
    ) -> None:
        """The reason the entry point is a list and not a hardcoded name."""
        engine: Any = MagicMock(spec=["chat"])

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            engine.chat("hello")

        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "chat"

    def test_an_object_with_no_entry_point_fails_loudly(self, tmp_path: Path) -> None:
        """Silently capturing nothing would be the worse outcome."""
        with _fake("llama_index.core"):
            from novafabric.adapters.llamaindex import wrap_engine

            with pytest.raises(AttributeError, match="none of"):
                wrap_engine(MagicMock(spec=[]), data_dir=tmp_path)

    def test_failure_is_recorded_and_the_exception_still_propagates(
        self, tmp_path: Path
    ) -> None:
        engine: Any = MagicMock(spec=["query"])
        engine.query.side_effect = ValueError("index missing")

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            with pytest.raises(ValueError, match="index missing"):
                engine.query("x")

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["exit_code"] == 1
        assert manifest["error"]["type"] == "ValueError"


# --------------------------------------------------------------------------
# Pydantic AI
# --------------------------------------------------------------------------

class TestPydanticAI:
    def test_run_sync_writes_a_capsule(self, tmp_path: Path) -> None:
        agent: Any = MagicMock(spec=["run_sync", "name"])
        agent.name = "support"
        agent.run_sync.return_value = "ok"

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.pydantic_ai import wrap_agent

            wrap_agent(agent, data_dir=tmp_path)
            assert agent.run_sync("where is my order?") == "ok"

        manifest = _sole_manifest(tmp_path)
        assert manifest["metadata"]["framework"] == "pydantic-ai"
        assert manifest["metadata"]["entry_point"] == "run_sync"
        assert manifest["command"] == ["@pydantic-ai:support"]

    def test_the_async_run_writes_a_capsule(self, tmp_path: Path) -> None:
        agent: Any = MagicMock(spec=["run"])

        async def _run(*a: Any, **k: Any) -> str:
            return "async ok"

        agent.run = _run

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.pydantic_ai import wrap_agent

            wrap_agent(agent, run_name="a", data_dir=tmp_path)
            assert asyncio.run(agent.run("q")) == "async ok"

        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "run"

    def test_run_sync_delegating_to_run_produces_exactly_one_capsule(
        self, tmp_path: Path
    ) -> None:
        """The re-entrancy guard, asserted rather than trusted.

        Pydantic AI's ``run_sync`` drives ``run`` internally. Both are patched,
        so without the guard one user-visible call opens two capsules and the
        inner one takes the wire hooks away from the outer — an event stream
        split across two capsules, neither of them complete.
        """
        calls: list[str] = []

        class FakeAgent:
            async def run(self, *a: Any, **k: Any) -> str:
                calls.append("run")
                return "done"

            def run_sync(self, *a: Any, **k: Any) -> str:
                calls.append("run_sync")
                return asyncio.run(self.run(*a, **k))

        agent = FakeAgent()

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.pydantic_ai import wrap_agent

            wrap_agent(agent, run_name="nested", data_dir=tmp_path)
            assert agent.run_sync("q") == "done"

        assert calls == ["run_sync", "run"], calls
        manifest = _sole_manifest(tmp_path)
        assert manifest["metadata"]["entry_point"] == "run_sync", (
            "the outer sync call must own the capsule"
        )

    def test_async_failure_is_recorded(self, tmp_path: Path) -> None:
        agent: Any = MagicMock(spec=["run"])

        async def _boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("model refused")

        agent.run = _boom

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.pydantic_ai import wrap_agent

            wrap_agent(agent, data_dir=tmp_path)
            with pytest.raises(RuntimeError, match="model refused"):
                asyncio.run(agent.run("q"))

        assert _sole_manifest(tmp_path)["error"]["type"] == "RuntimeError"


# --------------------------------------------------------------------------
# Haystack
# --------------------------------------------------------------------------

class TestHaystack:
    def test_pipeline_run_writes_a_capsule(self, tmp_path: Path) -> None:
        pipeline: Any = MagicMock(spec=["run"])
        pipeline.run.return_value = {"answers": ["blue"]}

        with _fake("haystack"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.haystack import wrap_pipeline

            wrap_pipeline(pipeline, run_name="rag-qa", data_dir=tmp_path)
            assert pipeline.run({"q": "colour?"}) == {"answers": ["blue"]}

        manifest = _sole_manifest(tmp_path)
        assert manifest["metadata"]["framework"] == "haystack"
        assert manifest["metadata"]["entry_point"] == "run"

    def test_async_pipeline_is_wrapped_too(self, tmp_path: Path) -> None:
        pipeline: Any = MagicMock(spec=["run_async"])

        async def _run_async(*a: Any, **k: Any) -> dict:
            return {"answers": []}

        pipeline.run_async = _run_async

        with _fake("haystack"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.haystack import wrap_pipeline

            wrap_pipeline(pipeline, data_dir=tmp_path)
            asyncio.run(pipeline.run_async({}))

        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "run_async"

    def test_failure_is_recorded(self, tmp_path: Path) -> None:
        pipeline: Any = MagicMock(spec=["run"])
        pipeline.run.side_effect = KeyError("retriever")

        with _fake("haystack"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.haystack import wrap_pipeline

            wrap_pipeline(pipeline, data_dir=tmp_path)
            with pytest.raises(KeyError):
                pipeline.run({})

        assert _sole_manifest(tmp_path)["status"] == "failure"


# --------------------------------------------------------------------------
# Registry aliases
# --------------------------------------------------------------------------

class TestAliases:
    @pytest.mark.parametrize(
        ("alias", "missing"),
        [
            ("wrap_llamaindex", "llama_index.core"),
            ("wrap_pydantic_ai", "pydantic_ai"),
            ("wrap_haystack", "haystack"),
        ],
    )
    def test_alias_is_exported_and_reaches_the_adapter(
        self, alias: str, missing: str
    ) -> None:
        import novafabric.adapters as adapters

        assert alias in adapters.__all__
        with patch.dict(sys.modules, {missing: None}):
            with pytest.raises(ImportError):
                getattr(adapters, alias)(MagicMock())


# --------------------------------------------------------------------------
# Call shapes that finish after the patched method returns (2026-10-09 audit)
# --------------------------------------------------------------------------

class TestLlamaIndexDeferredCalls:
    def test_agent_run_handler_is_captured_when_awaited_not_when_returned(
        self, tmp_path: Path
    ) -> None:
        """An agent's ``run`` returns a WorkflowHandler (an asyncio.Future).

        The work happens when the caller awaits it. Finishing the capsule when
        ``run`` returns wrote an empty, successful capsule and released the wire
        hooks before the first model call — the whole run went uncaptured.
        """
        seen_during_run: list[int] = []

        class FakeAgent:
            def run(self, *a: Any, **k: Any) -> asyncio.Future:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()

                async def _work() -> None:
                    await asyncio.sleep(0)
                    seen_during_run.append(len(list(tmp_path.glob("*/capsule.yaml"))))
                    fut.set_result("agent answer")

                loop.create_task(_work())
                return fut

        agent = FakeAgent()

        async def _main() -> str:
            handler = agent.run(user_msg="hi")
            return await handler

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(agent, run_name="agent", data_dir=tmp_path)
            assert asyncio.run(_main()) == "agent answer"

        assert seen_during_run == [0], "the capsule was finished before the run ran"
        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "run"

    def test_a_failed_workflow_handler_is_recorded_as_a_failure(
        self, tmp_path: Path
    ) -> None:
        class FakeAgent:
            def run(self, *a: Any, **k: Any) -> asyncio.Future:
                fut = asyncio.get_running_loop().create_future()
                fut.set_exception(RuntimeError("tool crashed"))
                return fut

        agent = FakeAgent()

        async def _main() -> None:
            await agent.run()
            await asyncio.sleep(0)

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(agent, data_dir=tmp_path)
            with pytest.raises(RuntimeError, match="tool crashed"):
                asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "RuntimeError"

    def test_the_async_twin_is_patched_too(self, tmp_path: Path) -> None:
        class FakeChatEngine:
            def chat(self, *a: Any, **k: Any) -> str:
                return "sync"

            async def achat(self, *a: Any, **k: Any) -> str:
                return "async"

        engine = FakeChatEngine()
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            assert asyncio.run(engine.achat("hi")) == "async"

        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "achat"

    def test_a_nested_call_records_into_the_open_capsule(self, tmp_path: Path) -> None:
        class FakeQueryEngine:
            def query(self, *a: Any, **k: Any) -> str:
                return asyncio.run(self.aquery(*a, **k))

            async def aquery(self, *a: Any, **k: Any) -> str:
                return "nested"

        engine = FakeQueryEngine()
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            assert engine.query("q") == "nested"

        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "query"


class TestHaystackNesting:
    def test_async_pipeline_run_driving_run_async_produces_exactly_one_capsule(
        self, tmp_path: Path
    ) -> None:
        """Haystack 2.x ``AsyncPipeline.run`` is ``asyncio.run(self.run_async(...))``.

        Both are patched; without the guard one call opened two capsules.
        """
        calls: list[str] = []

        class FakeAsyncPipeline:
            async def run_async(self, *a: Any, **k: Any) -> dict:
                calls.append("run_async")
                return {"answers": ["x"]}

            def run(self, *a: Any, **k: Any) -> dict:
                calls.append("run")
                return asyncio.run(self.run_async(*a, **k))

        pipe = FakeAsyncPipeline()
        with _fake("haystack"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.haystack import wrap_pipeline

            wrap_pipeline(pipe, data_dir=tmp_path)
            assert pipe.run({}) == {"answers": ["x"]}

        assert calls == ["run", "run_async"], calls
        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "run"


# --------------------------------------------------------------------------
# The real wire hooks and the real schema (issues #1-#3 definition of done)
# --------------------------------------------------------------------------

def _packaged_capsule_schema() -> dict:
    import json

    root = Path(__file__).resolve().parents[2]
    return json.loads(
        (root / "src" / "novafabric" / "schemas" / "run-capsule.schema.json").read_text()
    )


@pytest.mark.parametrize(
    ("module", "func", "missing", "method"),
    [
        ("llamaindex", "wrap_engine", "llama_index.core", "query"),
        ("pydantic_ai", "wrap_agent", "pydantic_ai", "run_sync"),
        ("haystack", "wrap_pipeline", "haystack", "run"),
    ],
)
def test_real_hooks_are_claimed_for_the_call_and_released_after(
    tmp_path: Path, module: str, func: str, missing: str, method: str
) -> None:
    """No hook mocks: the owner token is held during the call and gone after it,
    the manifest says the wire stream was installed, and the capsule passes the
    packaged run-capsule schema."""
    import importlib

    import jsonschema

    from novafabric.capture.hooks import current_hook_owner

    assert current_hook_owner() is None, "a previous test leaked the hooks"
    owner_during_call: list[str | None] = []

    class Target:
        name = "real-hooks"

    def _call(*a: Any, **k: Any) -> str:
        owner_during_call.append(current_hook_owner())
        return "ok"

    target = Target()
    setattr(target, method, _call)

    # Only the wrap needs the fake framework module. The call must run OUTSIDE
    # patch.dict(sys.modules): the real install_all imports SDKs, and
    # patch.dict would evict them on exit and break every later import.
    with _fake(missing):
        wrap = getattr(importlib.import_module(f"novafabric.adapters.{module}"), func)
        wrap(target, data_dir=tmp_path)
    with _quiet_capsule():
        assert getattr(target, method)("q") == "ok"

    assert owner_during_call and owner_during_call[0] is not None
    assert current_hook_owner() is None, "the adapter did not release the hooks"
    manifest = _sole_manifest(tmp_path)
    assert manifest["metadata"]["wire_capture"] == "installed"
    jsonschema.validate(manifest, _packaged_capsule_schema())


# --------------------------------------------------------------------------
# Streaming (issues #1, #2 follow-up): the capsule spans the stream
# --------------------------------------------------------------------------
#
# Neither framework is installed here, so the fakes below copy the *shape* of
# the real objects, read from the published wheels (pydantic-ai-slim 2.54.0,
# llama-index-core 0.14.25, llama-index-workflows 2.25.0, 2.14.0 and 1.3.0):
#
# * Pydantic AI ``run_stream`` / ``iter`` are ``@asynccontextmanager`` methods;
#   ``run`` and ``run_stream`` drive ``self.iter`` internally. A
#   ``StreamedRunResult`` sets ``is_complete`` once its output is read to the
#   end; an ``AgentRun`` has ``result`` only once the graph reached ``End``.
# * LlamaIndex ``StreamingResponse`` / ``AsyncStreamingResponse`` are
#   dataclasses whose ``response_gen`` *field* is the model stream.
#   ``StreamingAgentChatResponse`` drains the model stream in its own
#   ``write_response_to_history_thread`` (sync) or
#   ``awrite_response_to_history_task`` (async), whether or not the caller
#   reads ``response_gen``.
# * ``WorkflowHandler`` is an ``asyncio.Future`` in workflows 1.x/2.0 and, from
#   2.14 at least, a plain awaitable whose run is the ``_result_task`` behind
#   ``stop_event_result()`` / ``stream_events()``.

def _capsule_count(tmp_path: Path) -> int:
    return len(list(tmp_path.glob("*/capsule.yaml")))


def _join_stream_closers() -> None:
    for t in threading.enumerate():
        if t.name == "novafabric-stream-close":
            t.join(timeout=10)


class _FakeStreamedRunResult:
    """Pydantic AI ``StreamedRunResult``: complete once read to the end."""

    def __init__(self, tokens: list[str], fail_after: int | None = None) -> None:
        self._tokens = tokens
        self._fail_after = fail_after
        self._stream_response = object()
        self.is_complete = False
        self.cancelled = False

    async def stream_text(self, delta: bool = True) -> Any:
        for i, tok in enumerate(self._tokens):
            if self._fail_after is not None and i == self._fail_after:
                raise RuntimeError("provider dropped the stream")
            yield tok
        self.is_complete = True

    async def get_output(self) -> str:
        return "".join([t async for t in self.stream_text()])


class _FakeAgentRun:
    """Pydantic AI ``AgentRun``: async-iterates nodes; ``result`` set at End."""

    def __init__(self, nodes: int, fail_at: int | None = None) -> None:
        self._nodes = nodes
        self._fail_at = fail_at
        self.result: str | None = None

    def __aiter__(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        for i in range(self._nodes):
            if self._fail_at == i:
                raise RuntimeError("tool raised")
            yield f"node-{i}"
        self.result = "final"


class _FakePydanticAgent:
    name = "streamer"

    def __init__(
        self,
        *,
        nodes: int = 3,
        fail_at: int | None = None,
        tokens: list[str] | None = None,
        fail_after: int | None = None,
    ) -> None:
        self._nodes, self._fail_at = nodes, fail_at
        self._tokens = tokens or ["Par", "is"]
        self._fail_after = fail_after

    @contextlib.asynccontextmanager
    async def iter(self, prompt: str) -> Any:
        yield _FakeAgentRun(self._nodes, self._fail_at)

    async def run(self, prompt: str) -> str | None:
        async with self.iter(prompt) as agent_run:
            async for _ in agent_run:
                pass
        return agent_run.result

    @contextlib.asynccontextmanager
    async def run_stream(self, prompt: str) -> Any:
        async with self.iter(prompt):
            yield _FakeStreamedRunResult(self._tokens, self._fail_after)


class TestPydanticAIStreaming:
    def _wrap(self, agent: Any, tmp_path: Path) -> Any:
        from novafabric.adapters.pydantic_ai import wrap_agent

        return wrap_agent(agent, data_dir=tmp_path)

    def test_run_stream_capsule_spans_the_body_and_is_one_capsule(
        self, tmp_path: Path
    ) -> None:
        """``run_stream`` drives ``iter``; both are patched — one capsule, the outer's."""
        agent = _FakePydanticAgent()
        inside: list[int] = []

        async def _main() -> str:
            async with agent.run_stream("capital?") as response:
                inside.append(_capsule_count(tmp_path))
                return await response.get_output()

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            assert asyncio.run(_main()) == "Paris"

        assert inside == [0], "the capsule was written before the stream was read"
        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "run_stream"
        assert "partial_reason" not in manifest["metadata"]

    def test_run_stream_left_before_the_output_is_read_is_partial(
        self, tmp_path: Path
    ) -> None:
        agent = _FakePydanticAgent()

        async def _main() -> None:
            async with agent.run_stream("q") as response:
                async for _ in response.stream_text():
                    break  # the caller stops reading after one token

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"
        assert "error" not in manifest

    def test_run_stream_raising_mid_stream_is_a_failure_and_propagates(
        self, tmp_path: Path
    ) -> None:
        agent = _FakePydanticAgent(tokens=["a", "b", "c"], fail_after=1)

        async def _main() -> None:
            async with agent.run_stream("q") as response:
                await response.get_output()

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            with pytest.raises(RuntimeError, match="dropped the stream"):
                asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "RuntimeError"

    def test_iter_driven_to_end_is_a_success(self, tmp_path: Path) -> None:
        agent = _FakePydanticAgent(nodes=3)

        async def _main() -> list[str]:
            async with agent.iter("q") as agent_run:
                return [n async for n in agent_run]

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            assert asyncio.run(_main()) == ["node-0", "node-1", "node-2"]

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "iter"

    def test_iter_abandoned_before_end_is_partial(self, tmp_path: Path) -> None:
        agent = _FakePydanticAgent(nodes=3)

        async def _main() -> None:
            async with agent.iter("q") as agent_run:
                async for _ in agent_run:
                    break

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"

    def test_iter_raising_mid_run_is_a_failure(self, tmp_path: Path) -> None:
        agent = _FakePydanticAgent(nodes=3, fail_at=1)

        async def _main() -> None:
            async with agent.iter("q") as agent_run:
                async for _ in agent_run:
                    pass

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            with pytest.raises(RuntimeError, match="tool raised"):
                asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["metadata"]["entry_point"] == "iter"

    def test_run_driving_iter_is_one_capsule_owned_by_run(self, tmp_path: Path) -> None:
        agent = _FakePydanticAgent(nodes=2)

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            assert asyncio.run(agent.run("q")) == "final"

        manifest = _sole_manifest(tmp_path)
        assert manifest["metadata"]["entry_point"] == "run"
        assert manifest["status"] == "success"

    def test_a_call_inside_the_stream_body_records_into_the_open_capsule(
        self, tmp_path: Path
    ) -> None:
        agent = _FakePydanticAgent()

        async def _main() -> None:
            async with agent.run_stream("q") as response:
                await agent.run("a follow-up inside the block")
                await response.get_output()

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            asyncio.run(_main())

        assert _sole_manifest(tmp_path)["metadata"]["entry_point"] == "run_stream"

    def test_a_cancelled_run_is_partial_not_success(self, tmp_path: Path) -> None:
        """``run_stream_events`` abandoned mid-run cancels the ``run`` task."""

        class SlowAgent:
            async def run(self, prompt: str) -> str:
                await asyncio.sleep(3600)
                return "never"

        agent = SlowAgent()

        async def _main() -> None:
            task = asyncio.ensure_future(agent.run("q"))
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"


@dataclasses.dataclass
class _FakeStreamingResponse:
    """LlamaIndex ``StreamingResponse``: ``response_gen`` is a dataclass field."""

    response_gen: Any
    response_txt: str | None = None

    def __str__(self) -> str:
        if self.response_txt is None and self.response_gen is not None:
            self.response_txt = "".join(self.response_gen)
        return self.response_txt or "None"


@dataclasses.dataclass
class _FakeAsyncStreamingResponse:
    """LlamaIndex ``AsyncStreamingResponse``."""

    response_gen: Any
    response_txt: str | None = None

    async def async_response_gen(self) -> Any:
        async for text in self.response_gen:
            yield text


def _tokens(seen: list[int], tmp_path: Path, fail_after: int | None = None) -> Any:
    for i, tok in enumerate(["The ", "answer ", "is ", "42"]):
        if fail_after is not None and i == fail_after:
            raise ConnectionError("stream reset by peer")
        seen.append(_capsule_count(tmp_path))
        yield tok


async def _atokens(fail_after: int | None = None) -> Any:
    for i, tok in enumerate(["a", "b", "c"]):
        if fail_after is not None and i == fail_after:
            raise ConnectionError("stream reset by peer")
        await asyncio.sleep(0)
        yield tok


async def _settle() -> None:
    """Let done-callbacks scheduled by a just-finished task run."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestLlamaIndexStreaming:
    def _engine(
        self, tmp_path: Path, seen: list[int], fail_after: int | None = None
    ) -> Any:
        class FakeQueryEngine:  # built with streaming=True
            def query(self, q: str) -> _FakeStreamingResponse:
                return _FakeStreamingResponse(_tokens(seen, tmp_path, fail_after))

            async def aquery(self, q: str) -> _FakeAsyncStreamingResponse:
                return _FakeAsyncStreamingResponse(_atokens(fail_after))

        return FakeQueryEngine()

    def _wrap(self, engine: Any, tmp_path: Path) -> Any:
        from novafabric.adapters.llamaindex import wrap_engine

        return wrap_engine(engine, run_name="rag", data_dir=tmp_path)

    def test_streaming_query_capsule_closes_when_the_stream_is_exhausted(
        self, tmp_path: Path
    ) -> None:
        seen: list[int] = []
        engine = self._engine(tmp_path, seen)
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            response = engine.query("q")
            assert _capsule_count(tmp_path) == 0, "closed before the stream was read"
            assert str(response) == "The answer is 42"

        assert seen == [0, 0, 0, 0], "the capsule was written mid-stream"
        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "query"

    def test_a_stream_closed_early_is_partial(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path, [])
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            response = engine.query("q")
            assert next(response.response_gen) == "The "
            response.response_gen.close()

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"

    def test_a_stream_dropped_unread_is_partial_and_releases_the_capsule(
        self, tmp_path: Path
    ) -> None:
        """A generator never started runs none of its code when closed."""
        engine = self._engine(tmp_path, [])
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            response = engine.query("q")
            del response
            gc.collect()

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"

    def test_a_stream_raising_mid_way_is_a_failure_and_propagates(
        self, tmp_path: Path
    ) -> None:
        engine = self._engine(tmp_path, [], fail_after=2)
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            response = engine.query("q")
            with pytest.raises(ConnectionError, match="reset by peer"):
                str(response)

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "ConnectionError"

    def test_a_call_made_while_producing_the_stream_reuses_the_capsule(
        self, tmp_path: Path
    ) -> None:
        holder: dict[str, Any] = {}

        def _gen() -> Any:
            yield "outer "
            yield str(holder["engine"].query("nested"))  # e.g. a sub-question

        class FakeQueryEngine:
            def __init__(self) -> None:
                self.calls = 0

            def query(self, q: str) -> Any:
                self.calls += 1
                if self.calls == 1:
                    return _FakeStreamingResponse(_gen())
                return "inner"

        engine = FakeQueryEngine()
        holder["engine"] = engine
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            assert str(engine.query("q")) == "outer inner"

        assert _sole_manifest(tmp_path)["status"] == "success"

    def test_async_streaming_query_success_and_abandon(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path, [])

        async def _read_all() -> str:
            response = await engine.aquery("q")
            assert _capsule_count(tmp_path) == 0
            return "".join([t async for t in response.async_response_gen()])

        async def _read_one() -> None:
            response = await engine.aquery("q")
            gen = response.response_gen
            await gen.__anext__()
            await gen.aclose()

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            assert asyncio.run(_read_all()) == "abc"
            asyncio.run(_read_one())

        found = _manifests(tmp_path)
        assert sorted(m["status"] for m in found) == ["partial", "success"]
        assert {m["metadata"]["entry_point"] for m in found} == {"aquery"}

    def test_async_stream_raising_mid_way_is_a_failure(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path, [], fail_after=1)

        async def _read() -> None:
            response = await engine.aquery("q")
            async for _ in response.async_response_gen():
                pass

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            with pytest.raises(ConnectionError):
                asyncio.run(_read())

        assert _sole_manifest(tmp_path)["status"] == "failure"


@dataclasses.dataclass
class _FakeStreamingChatResponse:
    """``StreamingAgentChatResponse``: a writer drains the model stream itself."""

    chat_stream: Any = None
    exception: Exception | None = None
    tokens: list[str] = dataclasses.field(default_factory=list)
    write_response_to_history_thread: threading.Thread | None = None
    awrite_response_to_history_task: Any = None

    def write_response_to_history(self, gate: threading.Event) -> None:
        try:
            for tok in self.chat_stream:
                gate.wait(10)
                self.tokens.append(tok)
        except Exception as e:
            self.exception = e
            raise

    async def awrite_response_to_history(self, gate: asyncio.Event) -> None:
        try:
            async for tok in self.chat_stream:
                await gate.wait()
                self.tokens.append(tok)
        except Exception as e:
            self.exception = e
            raise


class TestLlamaIndexChatStreaming:
    def _engine(self, gate: Any, fail_after: int | None = None) -> Any:
        class FakeChatEngine:
            def chat(self, m: str) -> str:
                return "sync"

            def stream_chat(self, m: str) -> _FakeStreamingChatResponse:
                stream = _tokens([], Path("/nonexistent"), fail_after)
                resp = _FakeStreamingChatResponse(chat_stream=stream)
                t = threading.Thread(target=resp.write_response_to_history, args=(gate,))
                resp.write_response_to_history_thread = t
                t.start()
                return resp

            async def astream_chat(self, m: str) -> _FakeStreamingChatResponse:
                resp = _FakeStreamingChatResponse(chat_stream=_atokens(fail_after))
                resp.awrite_response_to_history_task = asyncio.create_task(
                    resp.awrite_response_to_history(gate)
                )
                return resp

        return FakeChatEngine()

    def _wrap(self, engine: Any, tmp_path: Path) -> Any:
        from novafabric.adapters.llamaindex import wrap_engine

        return wrap_engine(engine, data_dir=tmp_path)

    def test_stream_chat_closes_when_the_writer_drains_the_model_stream(
        self, tmp_path: Path
    ) -> None:
        """Even unread: LlamaIndex's own writer drains the stream, so the model
        call runs to completion whether or not ``response_gen`` is consumed."""
        gate = threading.Event()
        engine = self._engine(gate)
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            resp = engine.stream_chat("hi")
            assert _capsule_count(tmp_path) == 0, "closed while the model was streaming"
            gate.set()
            _join_stream_closers()

        assert resp.tokens == ["The ", "answer ", "is ", "42"]
        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "stream_chat"

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_stream_chat_writer_failure_is_a_failure(self, tmp_path: Path) -> None:
        gate = threading.Event()
        gate.set()
        engine = self._engine(gate, fail_after=1)
        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(engine, tmp_path)
            engine.stream_chat("hi")
            _join_stream_closers()

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "ConnectionError"

    def test_astream_chat_closes_when_the_writer_task_finishes(
        self, tmp_path: Path
    ) -> None:
        async def _main() -> list[str]:
            gate = asyncio.Event()
            engine = self._engine(gate)
            self._wrap(engine, tmp_path)
            resp = await engine.astream_chat("hi")
            await _settle()
            assert _capsule_count(tmp_path) == 0
            gate.set()
            await resp.awrite_response_to_history_task
            return resp.tokens

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            assert asyncio.run(_main()) == ["a", "b", "c"]

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "astream_chat"

    def test_astream_chat_writer_failure_is_a_failure(self, tmp_path: Path) -> None:
        async def _main() -> None:
            gate = asyncio.Event()
            gate.set()
            engine = self._engine(gate, fail_after=1)
            self._wrap(engine, tmp_path)
            resp = await engine.astream_chat("hi")
            with pytest.raises(ConnectionError):
                await resp.awrite_response_to_history_task

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            asyncio.run(_main())

        assert _sole_manifest(tmp_path)["status"] == "failure"


class _FakeWorkflowHandlerV2:
    """llama-index-workflows 2.x ``WorkflowHandler``: awaitable, not a Future."""

    def __init__(self, run: Any, events: asyncio.Queue) -> None:
        self._events = events
        self._result_task = asyncio.create_task(run)

    async def stop_event_result(self) -> Any:
        return await self._result_task

    def __await__(self) -> Any:
        return self.stop_event_result().__await__()

    async def stream_events(self) -> Any:
        while True:
            ev = await self._events.get()
            yield ev
            if ev == "StopEvent":
                break


class _FakeWorkflowHandlerPublicOnly(_FakeWorkflowHandlerV2):
    """A handler exposing only the public surface — exercises the fallback."""

    def __init__(self, run: Any, events: asyncio.Queue) -> None:
        self._events = events
        self._task = asyncio.create_task(run)

    async def stop_event_result(self) -> Any:
        return await self._task


class TestLlamaIndexWorkflowHandlerV2:
    def _agent(
        self, *, fail: bool = False, hang: bool = False, public_only: bool = False
    ) -> Any:
        handler_cls = _FakeWorkflowHandlerPublicOnly if public_only else _FakeWorkflowHandlerV2

        class FakeFunctionAgent:
            def run(self, user_msg: str = "") -> _FakeWorkflowHandlerV2:
                events: asyncio.Queue = asyncio.Queue()

                async def _work() -> str:
                    for i in range(3):
                        await events.put(f"AgentStream-{i}")
                        await asyncio.sleep(0)
                    if fail:
                        raise RuntimeError("step crashed")
                    if hang:
                        await asyncio.sleep(3600)
                    await events.put("StopEvent")
                    return "done"

                return handler_cls(_work(), events)

        return FakeFunctionAgent()

    def _wrap(self, agent: Any, tmp_path: Path) -> Any:
        from novafabric.adapters.llamaindex import wrap_engine

        return wrap_engine(agent, data_dir=tmp_path)

    @pytest.mark.parametrize("public_only", [False, True])
    def test_a_v2_handler_is_captured_until_the_workflow_settles(
        self, tmp_path: Path, public_only: bool
    ) -> None:
        """A ``Future`` check alone wrote this capsule empty, at ``run()`` return.

        The caller returns straight after ``await handler``: the capsule must
        still say success, not be cancelled by ``asyncio.run``'s teardown.
        """
        agent = self._agent(public_only=public_only)
        during: list[int] = []

        async def _main() -> tuple[list[str], Any]:
            handler = agent.run(user_msg="hi")
            during.append(_capsule_count(tmp_path))
            events = [ev async for ev in handler.stream_events()]
            return events, await handler

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            events, result = asyncio.run(_main())

        assert during == [0]
        assert events[-1] == "StopEvent" and result == "done"
        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "run"

    def test_a_failing_v2_workflow_is_a_failure(self, tmp_path: Path) -> None:
        agent = self._agent(fail=True)

        async def _main() -> None:
            handler = agent.run()
            with pytest.raises(RuntimeError, match="step crashed"):
                await handler

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "RuntimeError"

    @pytest.mark.parametrize("public_only", [False, True])
    def test_stream_events_abandoned_and_the_loop_closed_is_partial(
        self, tmp_path: Path, public_only: bool
    ) -> None:
        agent = self._agent(hang=True, public_only=public_only)

        async def _main() -> None:
            handler = agent.run()
            async for _ in handler.stream_events():
                break  # never awaits the handler; asyncio.run cancels the rest

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            self._wrap(agent, tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"


def test_real_hooks_are_held_for_the_whole_stream_and_released_after(
    tmp_path: Path,
) -> None:
    """No hook mocks: the owner token is held while tokens are produced, gone once
    the stream ends, and a ``partial`` capsule passes the packaged schema."""
    import jsonschema

    from novafabric.capture.hooks import current_hook_owner

    assert current_hook_owner() is None, "a previous test leaked the hooks"
    owners: list[str | None] = []

    def _gen() -> Any:
        for tok in ("x", "y"):
            owners.append(current_hook_owner())
            yield tok

    class Engine:
        def query(self, q: str) -> _FakeStreamingResponse:
            return _FakeStreamingResponse(_gen())

    engine = Engine()
    # As in the test above: only the wrap runs under the fake module.
    with _fake("llama_index.core"):
        from novafabric.adapters.llamaindex import wrap_engine

        wrap_engine(engine, data_dir=tmp_path)
    with _quiet_capsule():
        response = engine.query("q")
        assert current_hook_owner() is not None, "released before the stream ran"
        assert next(response.response_gen) == "x"
        response.response_gen.close()

    assert owners and owners[0] is not None
    assert current_hook_owner() is None, "the adapter did not release the hooks"
    manifest = _sole_manifest(tmp_path)
    assert manifest["status"] == "partial"
    assert manifest["metadata"]["wire_capture"] == "installed"
    jsonschema.validate(manifest, _packaged_capsule_schema())


def test_real_hooks_span_a_pydantic_ai_run_stream_body(tmp_path: Path) -> None:
    import jsonschema

    from novafabric.capture.hooks import current_hook_owner

    assert current_hook_owner() is None, "a previous test leaked the hooks"
    agent = _FakePydanticAgent()
    owners: list[str | None] = []

    async def _main() -> None:
        async with agent.run_stream("q") as response:
            owners.append(current_hook_owner())
            await response.get_output()

    with _fake("pydantic_ai"):
        from novafabric.adapters.pydantic_ai import wrap_agent

        wrap_agent(agent, data_dir=tmp_path)
    with _quiet_capsule():
        asyncio.run(_main())

    assert owners and owners[0] is not None
    assert current_hook_owner() is None, "the adapter did not release the hooks"
    manifest = _sole_manifest(tmp_path)
    assert manifest["status"] == "success"
    jsonschema.validate(manifest, _packaged_capsule_schema())


class TestStreamingEdgeCases:
    """Failure paths outside the happy stream: before the first token, on exit,
    on cancellation, and through a coroutine handed back by a sync method."""

    def test_run_stream_failing_before_the_first_token_is_a_failure(
        self, tmp_path: Path
    ) -> None:
        """``run_stream`` makes the model request inside ``__aenter__``."""

        class Agent:
            @contextlib.asynccontextmanager
            async def run_stream(self, prompt: str) -> Any:
                raise PermissionError("401 from provider")
                yield  # pragma: no cover

        agent = Agent()

        async def _main() -> None:
            async with agent.run_stream("q"):
                pass  # pragma: no cover

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.pydantic_ai import wrap_agent

            wrap_agent(agent, data_dir=tmp_path)
            with pytest.raises(PermissionError):
                asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "PermissionError"

    def test_iter_whose_cleanup_raises_is_a_failure(self, tmp_path: Path) -> None:
        class Agent:
            @contextlib.asynccontextmanager
            async def iter(self, prompt: str) -> Any:
                yield _FakeAgentRun(1)
                raise OSError("could not close the transport")

        agent = Agent()

        async def _main() -> None:
            async with agent.iter("q") as agent_run:
                async for _ in agent_run:
                    pass

        with _fake("pydantic_ai"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.pydantic_ai import wrap_agent

            wrap_agent(agent, data_dir=tmp_path)
            with pytest.raises(OSError, match="transport"):
                asyncio.run(_main())

        assert _sole_manifest(tmp_path)["error"]["type"] == "OSError"

    def test_async_entry_point_failure_and_cancellation(self, tmp_path: Path) -> None:
        class Engine:
            def query(self, q: str) -> str:
                return "sync"

            async def aquery(self, q: str) -> str:
                if q == "boom":
                    raise ValueError("bad index")
                await asyncio.sleep(3600)
                return "never"

        engine = Engine()

        async def _cancelled() -> None:
            task = asyncio.ensure_future(engine.aquery("slow"))
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            with pytest.raises(ValueError):
                asyncio.run(engine.aquery("boom"))
            asyncio.run(_cancelled())

        statuses = sorted(m["status"] for m in _manifests(tmp_path))
        assert statuses == ["failure", "partial"]

    def test_a_consumer_cancelled_mid_stream_is_partial(self, tmp_path: Path) -> None:
        class Engine:
            async def aquery(self, q: str) -> _FakeAsyncStreamingResponse:
                async def _slow() -> Any:
                    yield "first"
                    await asyncio.sleep(3600)
                    yield "never"  # pragma: no cover

                return _FakeAsyncStreamingResponse(_slow())

            def query(self, q: str) -> str:
                return "sync"

        engine = Engine()

        async def _main() -> None:
            response = await engine.aquery("q")

            async def _consume() -> None:
                async for _ in response.response_gen:
                    pass

            task = asyncio.ensure_future(_consume())
            await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"

    def test_a_coroutine_handed_back_by_a_sync_method_is_followed_into_its_stream(
        self, tmp_path: Path
    ) -> None:
        seen: list[int] = []

        class Engine:
            def query(self, q: str) -> Any:
                async def _later() -> _FakeStreamingResponse:
                    if q == "boom":
                        raise KeyError("retriever")
                    return _FakeStreamingResponse(_tokens(seen, tmp_path))

                return _later()

        engine = Engine()

        async def _main() -> str:
            response = await engine.query("q")
            assert _capsule_count(tmp_path) == 0
            return str(response)

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(engine, data_dir=tmp_path)
            assert asyncio.run(_main()) == "The answer is 42"
            with pytest.raises(KeyError):
                asyncio.run(engine.query("boom"))

        assert seen == [0, 0, 0, 0]
        statuses = sorted(m["status"] for m in _manifests(tmp_path))
        assert statuses == ["failure", "success"]

    def test_a_workflow_cancelled_by_the_user_is_partial(self, tmp_path: Path) -> None:
        class WorkflowCancelledByUser(Exception):
            """Same name as ``workflows.errors.WorkflowCancelledByUser``."""

        class Agent:
            def run(self) -> asyncio.Future:
                fut = asyncio.get_running_loop().create_future()
                fut.set_exception(WorkflowCancelledByUser())
                return fut

        agent = Agent()

        async def _main() -> None:
            with contextlib.suppress(WorkflowCancelledByUser):
                await agent.run()

        with _fake("llama_index.core"), _no_hooks(), _quiet_capsule():
            from novafabric.adapters.llamaindex import wrap_engine

            wrap_engine(agent, data_dir=tmp_path)
            asyncio.run(_main())

        manifest = _sole_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"
