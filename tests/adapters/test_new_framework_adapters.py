"""Adapters for LlamaIndex, Pydantic AI, and Haystack (issues #1, #2, #3).

None of the three frameworks is installed here, and none is a NovaFabric
dependency, so each is faked through ``sys.modules`` exactly as the existing
adapter tests fake dspy. What is *not* faked is the capsule machinery: these
tests exercise the real ``CapsuleWriter`` and the real manifest writer, so a
capsule that would fail ``nova validate`` fails here first.
"""
from __future__ import annotations

import asyncio
import sys
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
    """Stub the two heavyweight helpers the manifest writer calls.

    ``scan_and_redact`` must return a real dict: the writer json-dumps it, and
    a bare MagicMock is not serializable.
    """
    scanner = MagicMock()
    scanner.return_value.scan_and_redact.return_value = {}
    return patch.multiple(
        "novafabric.adapters._capsule",
        capture_environment=MagicMock(return_value={}),
        SecretScannerV0=scanner,
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
