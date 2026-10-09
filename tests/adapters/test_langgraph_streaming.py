"""LangGraph ``stream()`` / ``astream()``: the capsule records how the stream ended.

The fakes copy the shape of the real API, read from the published wheel
(langgraph 1.2.14, ``langgraph/pregel/main.py``):

* ``Pregel.stream(input, config=None, *, stream_mode=None, ..., subgraphs=False,
  version="v1")`` is a **generator function** and ``astream`` an **async
  generator function** — nothing runs until the first item is requested.
* A compiled ``StateGraph`` defaults to ``stream_mode="updates"``: one
  ``{node_name: update}`` dict per node per step. A *list* of modes yields
  ``(mode, data)`` tuples instead.
* ``invoke`` / ``ainvoke`` iterate ``self.stream`` / ``self.astream``
  internally; sync nodes run in a thread pool that ``copy_context()``s at
  submit time, so a context variable set around ``next()`` reaches the node.

Outcomes, mirroring the LlamaIndex / Pydantic AI streaming fix (d6a1583):
exhausted -> ``success``; raised mid-stream -> ``failure`` (re-raised); closed
early or dropped unread -> ``partial`` / ``abandoned``; cancelled ->
``partial`` / ``cancelled``; a wrapped graph called while another wrapped run
is producing records into the open capsule instead of opening a second one.

``TestRealLangGraph`` runs the same checks against the real package and skips
when it is absent (the default: langgraph is not a NovaFabric dependency).
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

import novafabric.adapters.langgraph as _langgraph_adapter


def _quiet_capsule() -> Any:
    scanner = MagicMock()
    scanner.return_value.scan_and_redact.return_value = {}
    return patch.multiple(
        "novafabric.adapters.langgraph",
        capture_environment=MagicMock(return_value={}),
        SecretScannerV0=scanner,
    )


def _manifests(tmp_path: Path) -> list[dict]:
    return [yaml.safe_load(p.read_text()) for p in sorted(tmp_path.glob("*/capsule.yaml"))]


def _sole(tmp_path: Path) -> dict:
    found = _manifests(tmp_path)
    assert len(found) == 1, f"expected exactly one capsule, got {len(found)}"
    return found[0]


def _transitions(tmp_path: Path) -> list[dict]:
    (cap_dir,) = [p.parent for p in tmp_path.glob("*/capsule.yaml")]
    path = cap_dir / "state_transitions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wrap(graph: Any, tmp_path: Path, run_name: str = "lg") -> Any:
    # The adapter module is imported once, at test-module level (_langgraph_adapter).
    # Importing it *inside* patch.dict(sys.modules) made patch.dict evict it on exit,
    # so every _wrap() re-imported a fresh module with its own `_in_flight`
    # ContextVar: an inner graph then never saw the outer run's flag and opened its
    # own capsule — a failure that appeared only when no earlier test had imported
    # the adapter first (alone, or under a different xdist split).
    with patch.dict(sys.modules, {"langgraph": MagicMock()}):
        return _langgraph_adapter.wrap(graph, run_name=run_name, data_dir=tmp_path)


class _FakeGraph:
    """A compiled graph whose nodes run as the stream is consumed."""

    def __init__(self, nodes: list[str], *, fail_at: str | None = None,
                 on_node: Any = None) -> None:
        self.nodes = nodes
        self.fail_at = fail_at
        self.on_node = on_node
        self.ran: list[str] = []
        self.closed = False

    def _step(self, node: str) -> dict:
        if node == self.fail_at:
            raise RuntimeError(f"node {node} failed")
        if self.on_node is not None:
            self.on_node(node)
        self.ran.append(node)
        return {node: {"visited": list(self.ran)}}

    def _shape(self, node: str, update: dict, stream_mode: Any) -> Any:
        if isinstance(stream_mode, list):
            return ("updates", update)
        return update

    def stream(self, input: Any, config: Any = None, *, stream_mode: Any = None,
               **kwargs: Any) -> Iterator[Any]:
        try:
            for node in self.nodes:
                yield self._shape(node, self._step(node), stream_mode)
        finally:
            self.closed = True

    async def astream(self, input: Any, config: Any = None, *, stream_mode: Any = None,
                      **kwargs: Any) -> AsyncIterator[Any]:
        try:
            for node in self.nodes:
                await asyncio.sleep(0)
                yield self._shape(node, self._step(node), stream_mode)
        finally:
            self.closed = True

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        out: Any = None
        for out in self.stream(input, config, **kwargs):
            pass
        return out

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        out: Any = None
        async for out in self.astream(input, config, **kwargs):
            pass
        return out


@pytest.fixture(autouse=True)
def _standard_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)


class TestSyncStream:
    def test_a_stream_read_to_the_end_is_a_success(self, tmp_path: Path) -> None:
        graph = _FakeGraph(["a", "b"])
        wrapped = _wrap(graph, tmp_path)
        with _quiet_capsule():
            chunks = list(wrapped.stream({"q": 1}))

        assert [next(iter(c)) for c in chunks] == ["a", "b"]
        manifest = _sole(tmp_path)
        assert manifest["status"] == "success"
        assert "partial_reason" not in manifest["metadata"]
        assert [t["agent_id"] for t in _transitions(tmp_path)] == ["a", "b"]

    def test_a_stream_closed_early_is_partial_and_stops_the_graph(
        self, tmp_path: Path
    ) -> None:
        graph = _FakeGraph(["a", "b", "c"])
        wrapped = _wrap(graph, tmp_path)
        with _quiet_capsule():
            stream = wrapped.stream({"q": 1})
            for _ in stream:
                break
            stream.close()

        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"
        assert manifest["exit_code"] == 0
        assert graph.ran == ["a"]
        assert graph.closed, "the real graph stream was left suspended"

    def test_a_stream_dropped_unread_is_partial_and_releases_the_hooks(
        self, tmp_path: Path
    ) -> None:
        graph = _FakeGraph(["a"])
        wrapped = _wrap(graph, tmp_path)
        with _quiet_capsule():
            from novafabric.capture.hooks import current_hook_owner

            wrapped.stream({"q": 1})  # never iterated, reference dropped
            gc.collect()
            assert current_hook_owner() is None, "the dropped stream kept the hooks"

        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"
        assert graph.ran == []

    def test_a_stream_raising_mid_way_is_a_failure_and_propagates(
        self, tmp_path: Path
    ) -> None:
        graph = _FakeGraph(["a", "b"], fail_at="b")
        wrapped = _wrap(graph, tmp_path)
        seen: list[Any] = []
        with _quiet_capsule(), pytest.raises(RuntimeError, match="node b"):
            for chunk in wrapped.stream({"q": 1}):
                seen.append(chunk)

        assert len(seen) == 1
        manifest = _sole(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "RuntimeError"
        assert "partial_reason" not in manifest["metadata"]

    def test_a_stream_interrupted_by_the_user_is_partial_not_success(
        self, tmp_path: Path
    ) -> None:
        def _interrupt(node: str) -> None:
            if node == "b":
                raise KeyboardInterrupt

        wrapped = _wrap(_FakeGraph(["a", "b"], on_node=_interrupt), tmp_path)
        with _quiet_capsule(), pytest.raises(KeyboardInterrupt):
            list(wrapped.stream({"q": 1}))

        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"

    def test_a_list_stream_mode_passes_mode_tuples_through(self, tmp_path: Path) -> None:
        wrapped = _wrap(_FakeGraph(["a", "b"]), tmp_path)
        with _quiet_capsule():
            chunks = list(wrapped.stream({"q": 1}, stream_mode=["updates", "values"]))

        assert [c[0] for c in chunks] == ["updates", "updates"]
        assert _sole(tmp_path)["status"] == "success"
        assert len(_transitions(tmp_path)) == 2

    def test_a_graph_whose_stream_raises_on_call_is_a_failure(self, tmp_path: Path) -> None:
        graph = MagicMock()
        graph.stream.side_effect = ValueError("bad stream_mode")
        wrapped = _wrap(graph, tmp_path)
        with _quiet_capsule(), pytest.raises(ValueError):
            wrapped.stream({"q": 1})

        assert _sole(tmp_path)["status"] == "failure"


class TestAsyncStream:
    def test_an_astream_read_to_the_end_is_a_success(self, tmp_path: Path) -> None:
        wrapped = _wrap(_FakeGraph(["a", "b"]), tmp_path)

        async def _main() -> list[Any]:
            return [c async for c in wrapped.astream({"q": 1})]

        with _quiet_capsule():
            chunks = asyncio.run(_main())

        assert len(chunks) == 2
        assert _sole(tmp_path)["status"] == "success"
        assert [t["agent_id"] for t in _transitions(tmp_path)] == ["a", "b"]

    def test_an_astream_closed_early_is_partial(self, tmp_path: Path) -> None:
        graph = _FakeGraph(["a", "b", "c"])
        wrapped = _wrap(graph, tmp_path)

        async def _main() -> None:
            stream = wrapped.astream({"q": 1})
            async for _ in stream:
                break
            await stream.aclose()

        with _quiet_capsule():
            asyncio.run(_main())

        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"
        assert graph.ran == ["a"]
        assert graph.closed

    def test_an_astream_raising_mid_way_is_a_failure(self, tmp_path: Path) -> None:
        wrapped = _wrap(_FakeGraph(["a", "b"], fail_at="b"), tmp_path)

        async def _main() -> None:
            async for _ in wrapped.astream({"q": 1}):
                pass

        with _quiet_capsule(), pytest.raises(RuntimeError, match="node b"):
            asyncio.run(_main())

        manifest = _sole(tmp_path)
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "RuntimeError"

    def test_an_astream_cancelled_mid_run_is_partial_cancelled(
        self, tmp_path: Path
    ) -> None:
        class SlowGraph(_FakeGraph):
            async def astream(self, input: Any, config: Any = None, **kw: Any) -> Any:
                yield {"a": {}}
                await asyncio.sleep(3600)
                yield {"b": {}}  # pragma: no cover

        wrapped = _wrap(SlowGraph([]), tmp_path)

        async def _main() -> None:
            async def _consume() -> None:
                async for _ in wrapped.astream({"q": 1}):
                    pass

            task = asyncio.ensure_future(_consume())
            await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with _quiet_capsule():
            asyncio.run(_main())

        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"

    def test_ainvoke_is_captured_and_a_cancelled_one_is_partial(
        self, tmp_path: Path
    ) -> None:
        wrapped = _wrap(_FakeGraph(["a"]), tmp_path / "ok")
        with _quiet_capsule():
            out = asyncio.run(wrapped.ainvoke({"q": 1}))
        assert out == {"a": {"visited": ["a"]}}
        assert _sole(tmp_path / "ok")["status"] == "success"

        class Hanging(_FakeGraph):
            async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> Any:
                await asyncio.sleep(3600)

        hanging = _wrap(Hanging([]), tmp_path / "cancel")

        async def _main() -> None:
            task = asyncio.ensure_future(hanging.ainvoke({"q": 1}))
            await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with _quiet_capsule():
            asyncio.run(_main())
        manifest = _sole(tmp_path / "cancel")
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"


class TestNesting:
    def test_a_wrapped_graph_invoked_inside_a_stream_records_into_the_open_capsule(
        self, tmp_path: Path
    ) -> None:
        inner = _wrap(_FakeGraph(["inner"]), tmp_path, run_name="inner")
        outer = _wrap(
            _FakeGraph(["a", "b"], on_node=lambda n: inner.invoke({"from": n})),
            tmp_path,
            run_name="outer",
        )
        with _quiet_capsule():
            list(outer.stream({"q": 1}))

        manifest = _sole(tmp_path)
        assert manifest["command"] == ["@langgraph:outer"]
        assert manifest["status"] == "success"
        # The nested run adds no transitions of its own: the outer chain stays intact.
        assert [t["agent_id"] for t in _transitions(tmp_path)] == ["a", "b"]

    def test_a_wrapped_stream_inside_an_invoke_and_an_astream_inside_an_astream(
        self, tmp_path: Path
    ) -> None:
        inner = _wrap(_FakeGraph(["inner"]), tmp_path, run_name="inner")
        outer = _wrap(
            _FakeGraph(["a"], on_node=lambda n: list(inner.stream({"from": n}))),
            tmp_path / "sync",
            run_name="outer",
        )
        with _quiet_capsule():
            outer.invoke({"q": 1})
        assert _sole(tmp_path / "sync")["command"] == ["@langgraph:outer"]
        assert not list(tmp_path.glob("*/capsule.yaml"))

        class AsyncOuter(_FakeGraph):
            async def astream(self, input: Any, config: Any = None, **kw: Any) -> Any:
                async for _ in inner.astream({"nested": True}):
                    pass
                yield {"a": {}}

        aouter = _wrap(AsyncOuter([]), tmp_path / "async", run_name="aouter")

        async def _main() -> None:
            async for _ in aouter.astream({"q": 1}):
                pass

        with _quiet_capsule():
            asyncio.run(_main())
        assert _sole(tmp_path / "async")["command"] == ["@langgraph:aouter"]
        assert not list(tmp_path.glob("*/capsule.yaml"))


def test_real_hooks_are_held_for_the_whole_stream_and_the_partial_capsule_is_valid(
    tmp_path: Path,
) -> None:
    import jsonschema

    from novafabric.capture.hooks import current_hook_owner

    assert current_hook_owner() is None, "a previous test leaked the hooks"
    owners: list[str | None] = []
    graph = _FakeGraph(["a", "b"], on_node=lambda n: owners.append(current_hook_owner()))
    wrapped = _wrap(graph, tmp_path)
    with _quiet_capsule():
        stream = wrapped.stream({"q": 1})
        next(stream)
        stream.close()

    assert owners and owners[0] is not None
    assert current_hook_owner() is None, "the adapter did not release the hooks"
    manifest = _sole(tmp_path)
    assert manifest["status"] == "partial"
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src" / "novafabric" / "schemas" / "run-capsule.schema.json"
    )
    jsonschema.validate(manifest, json.loads(schema_path.read_text()))


class TestRealLangGraph:
    """The same outcomes against the real package (verified with langgraph 1.2.14)."""

    @pytest.fixture(autouse=True)
    def _requires(self) -> None:
        pytest.importorskip("langgraph.graph")

    def _graph(self, *, fail: bool = False, slow: bool = False, nested: Any = None) -> Any:
        from typing import TypedDict

        from langgraph.graph import END, START, StateGraph

        class State(TypedDict):
            steps: list[str]

        def first(state: State) -> dict:
            if nested is not None:
                nested.invoke({"steps": []})
            return {"steps": [*state["steps"], "first"]}

        def second(state: State) -> dict:
            if fail:
                raise RuntimeError("second failed")
            return {"steps": [*state["steps"], "second"]}

        async def afirst(state: State) -> dict:
            if slow:
                await asyncio.sleep(3600)
            return {"steps": [*state["steps"], "first"]}

        builder = StateGraph(State)
        builder.add_node("first", afirst if slow else first)
        builder.add_node("second", second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        return builder.compile()

    def _wrap_real(self, graph: Any, tmp_path: Path, run_name: str = "real") -> Any:
        from novafabric.adapters.langgraph import wrap

        return wrap(graph, run_name=run_name, data_dir=tmp_path)

    def test_success_default_and_list_stream_modes(self, tmp_path: Path) -> None:
        wrapped = self._wrap_real(self._graph(), tmp_path / "updates")
        with _quiet_capsule():
            chunks = list(wrapped.stream({"steps": []}))
        assert [next(iter(c)) for c in chunks] == ["first", "second"]
        assert _sole(tmp_path / "updates")["status"] == "success"
        assert [t["agent_id"] for t in _transitions(tmp_path / "updates")] == [
            "first", "second",
        ]

        wrapped = self._wrap_real(self._graph(), tmp_path / "multi")
        with _quiet_capsule():
            chunks = list(wrapped.stream({"steps": []}, stream_mode=["updates", "values"]))
        assert {c[0] for c in chunks} == {"updates", "values"}
        assert _sole(tmp_path / "multi")["status"] == "success"

    def test_early_abandon_is_partial(self, tmp_path: Path) -> None:
        wrapped = self._wrap_real(self._graph(), tmp_path)
        with _quiet_capsule():
            stream = wrapped.stream({"steps": []})
            next(stream)
            stream.close()
        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "abandoned"

    def test_exception_mid_stream_is_a_failure(self, tmp_path: Path) -> None:
        wrapped = self._wrap_real(self._graph(fail=True), tmp_path)
        with _quiet_capsule(), pytest.raises(RuntimeError, match="second failed"):
            list(wrapped.stream({"steps": []}))
        assert _sole(tmp_path)["status"] == "failure"

    def test_cancelled_astream_is_partial(self, tmp_path: Path) -> None:
        wrapped = self._wrap_real(self._graph(slow=True), tmp_path)

        async def _main() -> None:
            async def _consume() -> None:
                async for _ in wrapped.astream({"steps": []}):
                    pass

            task = asyncio.ensure_future(_consume())
            await asyncio.sleep(0.2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with _quiet_capsule():
            asyncio.run(_main())
        manifest = _sole(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["metadata"]["partial_reason"] == "cancelled"

    def test_a_nested_wrapped_graph_reuses_the_open_capsule(self, tmp_path: Path) -> None:
        inner = self._wrap_real(self._graph(), tmp_path, run_name="inner")
        outer = self._wrap_real(self._graph(nested=inner), tmp_path, run_name="outer")
        with _quiet_capsule():
            list(outer.stream({"steps": []}))
        manifest = _sole(tmp_path)
        assert manifest["command"] == ["@langgraph:outer"]
        assert manifest["status"] == "success"
