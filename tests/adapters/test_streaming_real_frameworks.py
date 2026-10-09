"""Streaming capture against the REAL Pydantic AI and LlamaIndex packages.

Neither package is a NovaFabric dependency, so each class here skips when its
framework is absent — which is the default in CI. The always-on coverage is the
fake-based suite in ``test_new_framework_adapters.py``; this file exists to
check those fakes against the real objects whenever the packages are present
(verified with pydantic-ai-slim 2.54.0, llama-index-core 0.14.25 and
llama-index-workflows 2.25.0). No network: Pydantic AI's ``TestModel`` /
``FunctionModel`` and LlamaIndex's ``MockLLM`` / ``MockEmbedding`` are offline.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml


def _quiet_capsule() -> Any:
    return patch.multiple(
        "novafabric.adapters._capsule",
        capture_environment=MagicMock(return_value={}),
    )


def _manifests(tmp_path: Path) -> list[dict]:
    return [yaml.safe_load(p.read_text()) for p in sorted(tmp_path.glob("*/capsule.yaml"))]


def _sole(tmp_path: Path) -> dict:
    found = _manifests(tmp_path)
    assert len(found) == 1, f"expected exactly one capsule, got {len(found)}"
    return found[0]


def _join_stream_closers() -> None:
    for t in threading.enumerate():
        if t.name == "novafabric-stream-close":
            t.join(timeout=30)


class TestRealPydanticAI:
    @pytest.fixture(autouse=True)
    def _requires(self) -> None:
        pytest.importorskip("pydantic_ai")

    def _agent(self, tmp_path: Path, model: Any = None) -> Any:
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        from novafabric.adapters.pydantic_ai import wrap_agent

        agent = Agent(model or TestModel(custom_output_text="the capital is Paris"), name="real")
        return wrap_agent(agent, data_dir=tmp_path)

    def test_run_stream_read_to_the_end_is_one_successful_capsule(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)

        async def _main() -> str:
            async with agent.run_stream("capital?") as response:
                assert not list(tmp_path.glob("*/capsule.yaml"))
                return str(await response.get_output())

        with _quiet_capsule():
            assert "Paris" in asyncio.run(_main())
        manifest = _sole(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "run_stream"

    def test_run_stream_left_unread_is_partial(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)

        async def _main() -> None:
            async with agent.run_stream("capital?"):
                pass

        with _quiet_capsule():
            asyncio.run(_main())
        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"

    def test_run_stream_raising_mid_stream_is_a_failure(self, tmp_path: Path) -> None:
        from pydantic_ai.models.function import FunctionModel

        async def _stream(messages: Any, info: Any) -> Any:
            yield "partial "
            raise ConnectionError("stream reset by peer")

        agent = self._agent(tmp_path, FunctionModel(stream_function=_stream))

        async def _main() -> None:
            async with agent.run_stream("q") as response:
                await response.get_output()

        with _quiet_capsule(), pytest.raises(ConnectionError):
            asyncio.run(_main())
        manifest = _sole(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "ConnectionError"

    def test_iter_to_end_and_abandoned(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)

        async def _full() -> None:
            async with agent.iter("q") as run:
                async for _ in run:
                    pass

        async def _abandon() -> None:
            async with agent.iter("q") as run:
                async for _ in run:
                    break

        with _quiet_capsule():
            asyncio.run(_full())
            asyncio.run(_abandon())
        found = _manifests(tmp_path)
        assert sorted(m["status"] for m in found) == ["partial", "success"]
        assert {m["metadata"]["entry_point"] for m in found} == {"iter"}

    def test_run_sync_and_run_stream_sync_each_write_one_capsule(self, tmp_path: Path) -> None:
        """``run_sync`` -> ``run`` -> ``iter``; ``run_stream_sync`` -> ``run_stream`` -> ``iter``."""
        agent = self._agent(tmp_path)
        with _quiet_capsule():
            agent.run_sync("q")
        assert _sole(tmp_path)["metadata"]["entry_point"] == "run_sync"

        other = tmp_path / "sync-stream"
        agent = self._agent(other)
        with _quiet_capsule():
            with agent.run_stream_sync("q") as response:
                response.get_output()
        manifest = _sole(other)
        assert manifest["metadata"]["entry_point"] == "run_stream"
        assert manifest["status"] == "success"


class TestRealLlamaIndex:
    @pytest.fixture(autouse=True)
    def _requires(self) -> None:
        pytest.importorskip("llama_index.core")

    def test_chat_engine_stream_chat_and_astream_chat(self, tmp_path: Path) -> None:
        from llama_index.core.chat_engine import SimpleChatEngine
        from llama_index.core.llms import MockLLM

        from novafabric.adapters.llamaindex import wrap_engine

        engine = wrap_engine(
            SimpleChatEngine.from_defaults(llm=MockLLM(max_tokens=8)), data_dir=tmp_path
        )

        async def _astream() -> str:
            response = await engine.astream_chat("hello")
            return "".join([t async for t in response.async_response_gen()])

        with _quiet_capsule():
            response = engine.stream_chat("hello")
            "".join(response.response_gen)
            _join_stream_closers()
            asyncio.run(_astream())

        found = _manifests(tmp_path)
        assert {m["metadata"]["entry_point"] for m in found} == {"stream_chat", "astream_chat"}
        assert {m["status"] for m in found} == {"success"}

    def test_streaming_query_engine(self, tmp_path: Path) -> None:
        from llama_index.core import Document, VectorStoreIndex
        from llama_index.core.base.response.schema import StreamingResponse
        from llama_index.core.embeddings import MockEmbedding
        from llama_index.core.llms import MockLLM

        from novafabric.adapters.llamaindex import wrap_engine

        index = VectorStoreIndex.from_documents(
            [Document(text="NovaFabric writes run capsules.")],
            embed_model=MockEmbedding(embed_dim=8),
        )
        engine = wrap_engine(
            index.as_query_engine(streaming=True, llm=MockLLM(max_tokens=8)),
            data_dir=tmp_path,
        )
        with _quiet_capsule():
            response = engine.query("what does it write?")
            assert isinstance(response, StreamingResponse)
            assert not list(tmp_path.glob("*/capsule.yaml"))
            response.get_response()
            closed_early = engine.query("again")
            next(closed_early.response_gen)
            closed_early.response_gen.close()

        assert sorted(m["status"] for m in _manifests(tmp_path)) == ["partial", "success"]

    def test_workflow_handler_is_captured_until_the_run_settles(self, tmp_path: Path) -> None:
        from workflows import Workflow, step
        from workflows.events import StartEvent, StopEvent

        from novafabric.adapters.llamaindex import wrap_engine

        class Echo(Workflow):
            @step
            async def go(self, ev: StartEvent) -> StopEvent:
                await asyncio.sleep(0.01)
                return StopEvent(result="done")

        workflow = wrap_engine(Echo(), data_dir=tmp_path)

        async def _main() -> Any:
            handler = workflow.run()
            assert not list(tmp_path.glob("*/capsule.yaml"))
            async for _ in handler.stream_events():
                pass
            return await handler

        with _quiet_capsule():
            assert asyncio.run(_main()) == "done"
        manifest = _sole(tmp_path)
        assert manifest["status"] == "success"
        assert manifest["metadata"]["entry_point"] == "run"
