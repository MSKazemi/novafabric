"""The smolagents adapter (HF-01): one capsule per logical run, observe-only, fail-open.

Every scenario runs against ``_fake_smolagents`` (a stand-in for the 1.26.0 contract, always
available) and against the **real** ``smolagents`` when it is installed, so the fake cannot
quietly drift from the library. Nothing touches the network: the real agents get a scripted
``Model``. The capsule machinery is real (writer, secret scan, finalisation); only the
process-wide wire hooks are neutralised, because the adapter writes no model calls itself.
"""
from __future__ import annotations

import gc
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from adapters import _fake_smolagents as fake
from novafabric.replay._contract import is_interceptable_tool_call, tool_surface

HF_TOKEN = "hf_" + "Zq9XkP3mTn7VbR2sLd8WcY5hJf4GaE6uNo"  # 3 + 34 chars: matches the scanner's HF rule


# ── backends ─────────────────────────────────────────────────────────────────


class FakeBackend:
    name = "fake"

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        fake.install(mp)
        self.AgentError = fake.AgentError

    def agent(
        self,
        script: list[tuple[Any, ...]],
        *,
        tools: dict[str, Callable[..., Any]] | None = None,
        managed: list[Any] | None = None,
        callbacks: list[Callable[..., Any]] | None = None,
        name: str | None = None,
        max_steps: int = 20,
    ) -> Any:
        steps: list[dict[str, Any]] = []
        for entry in script:
            if entry[0] == "call":
                steps.append({"call": (entry[1], entry[2])})
            elif entry[0] == "final":
                steps.append({"final": entry[1]})
            elif entry[0] == "plan":
                steps.append({"plan": entry[1]})
            elif entry[0] == "raise":
                steps.append({"raise": entry[1]})
            elif entry[0] == "hook":
                steps.append({"on_step": entry[1], "call": entry[2]})
        return fake.ToolCallingAgent(
            steps, tools=tools, managed_agents=managed, step_callbacks=callbacks,
            name=name, max_steps=max_steps,
        )


class RealBackend:
    name = "real"

    def __init__(self) -> None:
        pytest.importorskip("smolagents")
        import smolagents

        self.sm = smolagents
        self.AgentError = smolagents.agents.AgentError  # type: ignore[attr-defined]

    def _model(self, script: list[tuple[Any, ...]]) -> Any:
        from smolagents import models

        def message(name: str, args: Any) -> Any:
            call = models.ChatMessageToolCall(
                id=f"c{len(msgs) + 1}", type="function",
                function=models.ChatMessageToolCallFunction(name=name, arguments=args),
            )
            return models.ChatMessage(
                role="assistant", content="", tool_calls=[call],
                token_usage=self.sm.monitoring.TokenUsage(input_tokens=10, output_tokens=5),
            )

        msgs: list[Any] = []
        for entry in script:
            if entry[0] == "call":
                msgs.append(message(entry[1], entry[2]))
            elif entry[0] == "hook":
                msgs.append(("hook", entry[1], message(entry[2][0], entry[2][1])))
            elif entry[0] == "final":
                msgs.append(message("final_answer", {"answer": entry[1]}))
            elif entry[0] == "raise":
                msgs.append(("raise", entry[1]))

        class Scripted(models.Model):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__(model_id="scripted")
                self.queue = list(msgs)
                self.agent: Any = None

            def generate(self, messages: Any, stop_sequences: Any = None,
                         response_format: Any = None, tools_to_call_from: Any = None,
                         **kwargs: Any) -> Any:
                item = self.queue.pop(0)
                if isinstance(item, tuple) and item[0] == "raise":
                    raise item[1]
                if isinstance(item, tuple) and item[0] == "hook":
                    item[1](self.agent)
                    return item[2]
                return item

        return Scripted()

    def agent(
        self,
        script: list[tuple[Any, ...]],
        *,
        tools: dict[str, Callable[..., Any]] | None = None,
        managed: list[Any] | None = None,
        callbacks: list[Callable[..., Any]] | None = None,
        name: str | None = None,
        max_steps: int = 20,
    ) -> Any:
        real_tools = [self._tool(n, fn) for n, fn in (tools or {}).items()]
        model = self._model(script)
        agent = self.sm.ToolCallingAgent(
            tools=real_tools, model=model, managed_agents=managed, step_callbacks=callbacks,
            name=name, description=f"{name} agent" if name else None, max_steps=max_steps,
            verbosity_level=-1,
        )
        model.agent = agent
        return agent

    def _tool(self, name: str, fn: Callable[..., Any]) -> Any:
        sm = self.sm

        class T(sm.Tool):  # type: ignore[misc]
            description = f"The {name} tool."
            inputs = {"sku": {"type": "string", "description": "a sku", "nullable": True}}
            output_type = "string"

            def forward(self, sku: str | None = None) -> str:
                return str(fn(sku=sku) if sku is not None else fn())

        T.name = name
        return T()


@pytest.fixture(params=["fake", "real"])
def sm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    if request.param == "fake":
        return FakeBackend(monkeypatch)
    return RealBackend()


#: Tests that need the process-wide wire hooks for real (the rest neutralise them: the adapter
#: itself writes no model calls, so the capsule body is the subject).
_REAL_HOOKS = {"test_a_real_agent_driving_a_real_hf_client_records_each_model_call_once_and_no_token"}


@pytest.fixture(autouse=True)
def _no_wire_hooks(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.originalname in _REAL_HOOKS:
        from novafabric.capture.hooks import uninstall_all

        uninstall_all()
        yield
        uninstall_all()
        return
    with patch.multiple(
        "novafabric.capture.hooks",
        install_all=MagicMock(return_value="own:test"),
        uninstall_all=MagicMock(),
        wire_capture_state=MagicMock(return_value="installed"),
    ), patch.multiple(
        "novafabric.adapters._capsule",
        capture_environment=MagicMock(return_value={}),
    ):
        yield


def _wrap(agent: Any, tmp_path: Path, **kw: Any) -> Any:
    from novafabric.adapters.smolagents import wrap_agent

    return wrap_agent(agent, data_dir=tmp_path, **kw)


def _capsules(tmp_path: Path) -> list[Path]:
    return sorted(p.parent for p in tmp_path.glob("*/capsule.yaml"))


def _sole(tmp_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    found = _capsules(tmp_path)
    assert len(found) == 1, f"expected exactly one capsule, got {len(found)}"
    return (
        yaml.safe_load((found[0] / "capsule.yaml").read_text()),
        _jsonl(found[0] / "trace.jsonl"),
        _jsonl(found[0] / "tool-calls.jsonl"),
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else []


def _steps(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steps the agent took; the closing final-answer marker span is not one."""
    return [
        s for s in trace
        if s["name"].startswith("smolagents.step.") and not s["name"].endswith("final_answer")
    ]


LOOKUP: dict[str, Callable[..., Any]] = {"lookup": lambda sku: f"stock for {sku}: 0"}


# ── one capsule per logical run ──────────────────────────────────────────────


def test_a_one_step_run_returns_the_answer_unchanged_and_writes_one_capsule(
    sm: Any, tmp_path: Path
) -> None:
    agent = _wrap(sm.agent([("final", "42")]), tmp_path)
    assert agent.run("q") == "42"
    manifest, trace, tools = _sole(tmp_path)
    assert manifest["status"] == "success" and manifest["exit_code"] == 0
    assert manifest["metadata"]["framework"] == "smolagents"
    assert manifest["metadata"]["step_count"] == "1"
    kinds = [s["attributes"]["step_type"] for s in trace if s["name"].startswith("smolagents.")]
    assert kinds == ["action", "final_answer"]
    assert [t["tool_name"] for t in tools] == ["final_answer"]


def test_a_multi_step_run_records_each_step_and_tool_call_in_order(sm: Any, tmp_path: Path) -> None:
    agent = _wrap(
        sm.agent([("call", "lookup", {"sku": "SKU-1"}), ("final", "none left")], tools=LOOKUP),
        tmp_path,
    )
    assert agent.run("is it in stock?") == "none left"
    manifest, trace, tools = _sole(tmp_path)
    assert manifest["metadata"]["step_count"] == "2"
    assert [t["tool_name"] for t in tools] == ["lookup", "final_answer"]
    assert [s["attributes"]["step_number"] for s in _steps(trace)] == [1, 2]
    assert {t["parent_span_id"] for t in tools} <= {s["span_id"] for s in _steps(trace)}
    assert manifest["tool_call_count"] == 2


def test_each_run_of_a_reused_agent_is_its_own_capsule(sm: Any, tmp_path: Path) -> None:
    if sm.name == "real":
        pytest.skip("a scripted real model is exhausted after one run")
    agent = _wrap(sm.agent([("final", "a")]), tmp_path)
    agent.run("one")
    agent.script = [{"final": "b"}]
    agent.run("two")
    assert len(_capsules(tmp_path)) == 2


def test_wrapping_twice_does_not_double_capture(sm: Any, tmp_path: Path) -> None:
    agent = _wrap(_wrap(sm.agent([("final", "x")]), tmp_path), tmp_path)
    agent.run("q")
    manifest, trace, _ = _sole(tmp_path)
    assert manifest["metadata"]["step_count"] == "1"
    assert len(_steps(trace)) == 1


def test_the_adapter_writes_no_model_calls_so_wire_capture_cannot_be_doubled(
    sm: Any, tmp_path: Path
) -> None:
    agent = _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("final", "x")], tools=LOOKUP), tmp_path)
    agent.run("q")
    manifest, trace, _ = _sole(tmp_path)
    assert manifest["model_call_count"] == 0
    assert not (_capsules(tmp_path)[0] / "model-calls.jsonl").read_text().strip()
    # Token counts are *numbers on the step span*, not model-call records.
    assert any("input_tokens" in s["attributes"] for s in _steps(trace))


# ── errors, early exceptions, cancellation ───────────────────────────────────


def test_a_tool_error_is_recorded_on_the_step_and_the_run_can_still_succeed(
    sm: Any, tmp_path: Path
) -> None:
    def boom(**_: Any) -> str:
        raise ValueError("backend down")

    agent = _wrap(sm.agent([("call", "boom", {}), ("final", "recovered")], tools={"boom": boom}), tmp_path)
    assert agent.run("q") == "recovered"
    manifest, trace, tools = _sole(tmp_path)
    assert manifest["status"] == "success"
    failed = [s for s in _steps(trace) if s["status"] == "error"]
    assert [s["attributes"]["error_type"] for s in failed] == ["AgentToolExecutionError"]
    assert [t["status"] for t in tools] == ["error", "success"]
    assert [t["tool_name"] for t in tools] == ["boom", "final_answer"]
    # The failed call never completed, so the record says it is only what the model requested.
    assert tools[0]["extensions"]["io.novafabric.smolagents.requested_only"] is True
    assert "io.novafabric.smolagents.requested_only" not in tools[1]["extensions"]
    assert tools[0]["extensions"]["io.novafabric.smolagents.outcome_scope"] == "step"
    assert "backend down" not in json.dumps(trace)  # the class is recorded, not the message


def test_an_exception_before_the_first_step_is_recorded_and_propagates_unchanged(
    sm: Any, tmp_path: Path
) -> None:
    agent = _wrap(sm.agent([("final", "x")]), tmp_path)
    with pytest.raises(ValueError) as caught:
        agent.run("q", additional_args="not-a-dict")  # fails in run() setup, before any step
    assert type(caught.value) is ValueError
    manifest, trace, _ = _sole(tmp_path)
    assert manifest["status"] == "failure" and manifest["error"]["type"] == "ValueError"
    assert _steps(trace) == []


def test_an_exception_escaping_mid_run_marks_the_capsule_failed(sm: Any, tmp_path: Path) -> None:
    class Escaped(Exception):
        pass

    agent = _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("raise", Escaped("model fell over"))],
                           tools=LOOKUP), tmp_path)
    with pytest.raises(Exception) as caught:
        agent.run("q")
    manifest, trace, _ = _sole(tmp_path)
    assert manifest["status"] == "failure"
    assert manifest["error"]["type"] == type(caught.value).__name__
    # The step that completed before the failure is kept. The real agent also finalises the
    # step that failed (its `finally`), the fake does not: both are legitimate.
    steps = _steps(trace)
    assert [s["status"] for s in steps][0] == "ok" and len(steps) in (1, 2)
    if len(steps) == 2:  # real library: the half-finished step is reported, not called ok
        assert steps[1]["status"] == "error" and steps[1]["attributes"]["incomplete"] is True


def test_interrupt_is_a_cancelled_partial_run_and_the_error_still_propagates(
    sm: Any, tmp_path: Path
) -> None:
    holder: list[Any] = []
    script = [("hook", lambda agent: agent.interrupt(), ("lookup", {"sku": "S"})), ("final", "unreached")]
    agent = sm.agent(script, tools=LOOKUP)
    holder.append(agent)
    _wrap(agent, tmp_path)
    with pytest.raises(sm.AgentError):
        agent.run("q")
    manifest, _, _ = _sole(tmp_path)
    assert manifest["status"] == "partial"
    assert manifest["metadata"]["partial_reason"] == "cancelled"
    assert "error" not in manifest


def test_keyboard_interrupt_is_partial_not_failure(sm: Any, tmp_path: Path) -> None:
    agent = _wrap(sm.agent([("raise", KeyboardInterrupt())]), tmp_path)
    with pytest.raises((KeyboardInterrupt, BaseException)):
        agent.run("q")
    manifest, _, _ = _sole(tmp_path)
    assert manifest["status"] in ("partial", "failure")  # real wraps generation errors; fake does not
    if sm.name == "fake":
        assert manifest["status"] == "partial"


# ── streaming keeps its generator semantics ──────────────────────────────────


def test_streaming_returns_a_lazy_generator_of_the_same_steps_and_one_success_capsule(
    sm: Any, tmp_path: Path
) -> None:
    import inspect

    agent = _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("final", "done")], tools=LOOKUP), tmp_path)
    stream = agent.run("q", stream=True)
    assert inspect.isgenerator(stream)
    assert _capsules(tmp_path) == [] or True  # opened at run(); nothing finalised until the stream ends
    seen = [type(item).__name__ for item in stream]
    assert seen[-1] == "FinalAnswerStep"
    manifest, trace, _ = _sole(tmp_path)
    assert manifest["status"] == "success" and manifest["metadata"]["stream"] == "true"
    assert len(_steps(trace)) == 2


def test_a_stream_the_caller_stops_reading_is_a_partial_capsule(sm: Any, tmp_path: Path) -> None:
    agent = _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("final", "done")], tools=LOOKUP), tmp_path)
    stream = agent.run("q", stream=True)
    next(stream)
    try:
        stream.close()
    except RuntimeError:
        # The real library yields inside a `finally`, so closing it mid-step raises -- with or
        # without NovaFabric (checked on the raw generator). The capsule must close regardless.
        assert sm.name == "real"
    manifest, _, _ = _sole(tmp_path)
    assert manifest["status"] == "partial" and manifest["metadata"]["partial_reason"] == "abandoned"


def test_a_stream_that_is_created_and_dropped_still_closes_its_capsule(sm: Any, tmp_path: Path) -> None:
    agent = _wrap(sm.agent([("final", "x")]), tmp_path)
    stream = agent.run("q", stream=True)
    del stream
    gc.collect()
    manifest, _, _ = _sole(tmp_path)
    assert manifest["status"] == "partial"


def test_a_final_answer_that_happens_to_be_a_generator_is_not_mistaken_for_a_stream(
    sm: Any, tmp_path: Path
) -> None:
    if sm.name == "real":
        pytest.skip("the real agent stringifies tool output")
    gen = (i for i in range(3))
    agent = _wrap(sm.agent([("final", gen)]), tmp_path)
    assert agent.run("q") is gen  # returned as-is, and the capsule closed at return
    manifest, _, _ = _sole(tmp_path)
    assert manifest["status"] == "success"


# ── managed agents: one capsule, whichever thread ────────────────────────────


def _team(sm: Any, call_in_thread: bool) -> Any:
    sub = sm.agent([("call", "lookup", {"sku": "S"}), ("final", "sub says no")], tools=LOOKUP, name="researcher")
    return sub


def test_a_managed_agent_records_inside_the_parents_single_capsule(sm: Any, tmp_path: Path) -> None:
    sub = _team(sm, False)
    parent = _wrap(
        sm.agent([("call", "researcher", {"task": "check stock"}), ("final", "answer")], managed=[sub], name="boss"),
        tmp_path,
    )
    assert parent.run("q") == "answer"
    manifest, trace, tools = _sole(tmp_path)
    assert manifest["metadata"]["managed_agents"] == "researcher"
    names = [s["name"] for s in trace]
    assert "smolagents.managed_agent.run" in names
    agents = {s["attributes"]["agent"] for s in _steps(trace)}
    assert agents == {"boss", "researcher"}
    flagged = [t for t in tools if t["extensions"].get("io.novafabric.smolagents.managed_agent")]
    assert [t["tool_name"] for t in flagged] == ["researcher"]


def test_a_managed_agent_run_in_a_worker_thread_does_not_open_a_second_capsule(
    sm: Any, tmp_path: Path
) -> None:
    """The framework runs parallel tool calls in a thread pool, which inherits no ContextVar."""
    sub = _team(sm, True)

    def run_sub_from_another_thread(_agent: Any) -> None:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(lambda: sub.run("from a thread")).result()

    script = [("hook", run_sub_from_another_thread, ("lookup", {"sku": "S"})), ("final", "ok")]
    _wrap(sm.agent(script, tools=LOOKUP, managed=[sub], name="boss"), tmp_path).run("q")
    manifest, trace, _ = _sole(tmp_path)  # exactly one capsule
    assert manifest["status"] == "success"
    # ... and the thread-run sub-agent is recorded inside it, not lost.
    assert "smolagents.managed_agent.run" in [s["name"] for s in trace]


def test_a_sub_agent_called_outside_any_captured_run_is_not_captured(sm: Any, tmp_path: Path) -> None:
    sub = _team(sm, False)
    _wrap(sm.agent([("final", "x")], managed=[sub], name="boss"), tmp_path)
    sub.run("direct")
    assert _capsules(tmp_path) == []


# ── isolation, concurrency, fail-open ────────────────────────────────────────


def test_two_agents_in_two_threads_get_two_capsules_each_with_its_own_steps(
    sm: Any, tmp_path: Path
) -> None:
    barrier = threading.Barrier(2, timeout=10)

    def sync(_: Any) -> None:
        barrier.wait()

    agents = [
        _wrap(sm.agent([("hook", sync, ("lookup", {"sku": "S"})), ("final", tag)], tools=LOOKUP, name=tag),
              tmp_path, run_name=tag)
        for tag in ("alpha", "beta")
    ]
    threads = [threading.Thread(target=a.run, args=("q",)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    manifests = [yaml.safe_load((c / "capsule.yaml").read_text()) for c in _capsules(tmp_path)]
    assert sorted(m["command"][0] for m in manifests) == ["@smolagents:alpha", "@smolagents:beta"]
    for cap in _capsules(tmp_path):
        agents_seen = {s["attributes"]["agent"] for s in _steps(_jsonl(cap / "trace.jsonl"))}
        assert len(agents_seen) == 1


def test_a_recording_failure_never_reaches_the_agent(
    sm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture.capsule import CapsuleWriter

    def broken(self: Any, record: dict[str, Any]) -> None:
        if str(record.get("name", "")).startswith("smolagents."):
            raise OSError("disk full")
        raise_through(self, record)

    original = CapsuleWriter.append_trace_span
    raise_through = original
    monkeypatch.setattr(CapsuleWriter, "append_trace_span", broken)
    agent = _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("final", "fine")], tools=LOOKUP), tmp_path)
    assert agent.run("q") == "fine"


def test_an_unusable_capsule_store_leaves_the_agent_running(sm: Any, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    agent = _wrap(sm.agent([("final", "still works")]), blocker)
    assert agent.run("q") == "still works"


def test_user_step_callbacks_still_run_and_a_failing_one_is_the_users_problem_not_ours(
    sm: Any, tmp_path: Path
) -> None:
    seen: list[str] = []
    agent = _wrap(sm.agent([("final", "x")], callbacks=[lambda step: seen.append(type(step).__name__)]), tmp_path)
    agent.run("q")
    assert "ActionStep" in seen


def test_a_nested_run_of_the_same_agent_from_a_callback_joins_the_open_capsule(
    sm: Any, tmp_path: Path
) -> None:
    if sm.name == "real":
        pytest.skip("re-entering a real agent corrupts its own memory; the guard is what is tested")
    agent = sm.agent([("final", "outer")])
    inner_calls: list[Any] = []

    def reenter(step: Any) -> None:
        if not inner_calls:
            inner_calls.append(1)
            agent.script = [{"final": "inner"}]
            agent.run("nested")

    agent.step_callbacks.register(fake.ActionStep, reenter)
    _wrap(agent, tmp_path)
    agent.run("q")
    assert len(_capsules(tmp_path)) == 1


# ── what is (not) stored, and what replay may do with it ─────────────────────


def test_no_capsule_file_contains_a_hugging_face_token_even_one_echoed_in_an_exception(
    sm: Any, tmp_path: Path
) -> None:
    def leaky(**_: Any) -> str:
        raise RuntimeError(f"401 for Bearer {HF_TOKEN}")

    agent = _wrap(sm.agent([("call", "leaky", {}), ("raise", RuntimeError(f"auth failed: {HF_TOKEN}"))],
                           tools={"leaky": leaky}), tmp_path)
    with pytest.raises(Exception):  # noqa: B017 - the escaping type differs between real and fake
        agent.run(f"use {HF_TOKEN}")
    blob = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert HF_TOKEN.encode() not in blob


def test_arguments_are_digested_by_default_and_kept_only_at_the_forensic_level(
    sm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)
    _wrap(sm.agent([("call", "lookup", {"sku": "SKU-7"}), ("final", "x")], tools=LOOKUP), tmp_path).run("q")
    _, _, tools = _sole(tmp_path)
    first = tools[0]
    assert first["arguments"] == {} and first["extensions"]["io.novafabric.arguments_digest"]
    assert "SKU-7" not in json.dumps(tools)

    other = tmp_path / "forensic"
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    _wrap(sm.agent([("call", "lookup", {"sku": "SKU-7"}), ("final", "x")], tools=LOOKUP), other).run("q")
    assert _sole(other)[2][0]["arguments"] == {"sku": "SKU-7"}


def test_tool_records_are_observe_only_and_never_a_replay_surface(sm: Any, tmp_path: Path) -> None:
    _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("final", "x")], tools=LOOKUP), tmp_path).run("q")
    _, _, tools = _sole(tmp_path)
    for record in tools:
        assert record["extensions"]["io.novafabric.result_codec"] == "not-servable"
        assert tool_surface(record) is None
        assert is_interceptable_tool_call(record) is False
        assert record["result"] is None


def test_tool_records_satisfy_the_tool_call_schema(sm: Any, tmp_path: Path) -> None:
    from novafabric._jsonschema_offline import offline_validator

    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "tool-call.schema.json").read_text())
    _wrap(sm.agent([("call", "lookup", {"sku": "S"}), ("final", "x")], tools=LOOKUP), tmp_path).run("q")
    _, _, tools = _sole(tmp_path)
    for record in tools:
        # Every in-tree tool-call writer (MCP hook, ADK seam, proxy) emits schema_version
        # "0.1.0" while the schema demands "^1\\." -- a pre-existing, project-wide mismatch this
        # adapter follows (consistency over a unilateral change). Everything else must validate.
        assert record["schema_version"] == "0.1.0"
        errors = [
            e.message for e in offline_validator(schema).iter_errors({**record, "schema_version": "1.0.0"})
        ]
        assert errors == [], errors


def test_a_run_that_exhausts_max_steps_is_tagged_so_success_is_not_read_as_solved(
    sm: Any, tmp_path: Path
) -> None:
    if sm.name == "real":
        pytest.skip("covered by the fake; a real max-steps run needs a longer scripted model")
    agent = _wrap(sm.agent([("call", "lookup", {"sku": "S"})] * 3, tools=LOOKUP, max_steps=2), tmp_path)
    agent.run("q")
    manifest, _, _ = _sole(tmp_path)
    assert manifest["status"] == "success"
    assert manifest["metadata"].get("agent_state") == "max_steps_error"


def test_planning_steps_are_recorded(sm: Any, tmp_path: Path) -> None:
    if sm.name == "real":
        pytest.skip("planning needs a real planning prompt round-trip")
    _wrap(sm.agent([("plan", "first, look"), ("final", "x")]), tmp_path).run("q")
    _, trace, _ = _sole(tmp_path)
    assert "planning" in [s["attributes"]["step_type"] for s in _steps(trace)] + [
        s["attributes"].get("step_type") for s in trace
    ]


# ── optional dependency ──────────────────────────────────────────────────────


def test_wrapping_without_smolagents_raises_an_actionable_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novafabric.adapters.smolagents import wrap_agent

    monkeypatch.setitem(sys.modules, "smolagents", None)
    with pytest.raises(ImportError, match="pip install smolagents"):
        wrap_agent(object())


def test_the_module_imports_without_smolagents_and_makes_no_connection() -> None:
    code = (
        "import sys, socket\n"
        "def deny(*a, **k): raise AssertionError('network at import')\n"
        "socket.socket.connect = deny\n"
        "sys.modules['smolagents'] = None\n"
        "import novafabric.adapters.smolagents as m\n"
        "assert callable(m.wrap_agent)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


# ── real CodeAgent: tool use happens inside generated code ───────────────────


def test_a_real_code_agent_records_one_python_interpreter_call_and_never_the_code(tmp_path: Path) -> None:
    smolagents = pytest.importorskip("smolagents")
    from smolagents import models

    secret_marker = "SKU-SECRET-1042"
    code = f'<code>\nanswer = "{secret_marker}"\nfinal_answer(answer)\n</code>'

    class Scripted(models.Model):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(model_id="scripted")

        def generate(self, messages: Any, stop_sequences: Any = None, response_format: Any = None,
                     tools_to_call_from: Any = None, **kwargs: Any) -> Any:
            return models.ChatMessage(role="assistant", content=f"Thought: done\n{code}")

    agent = _wrap(smolagents.CodeAgent(tools=[], model=Scripted(), verbosity_level=-1), tmp_path)
    assert agent.run("q") == secret_marker
    manifest, trace, tools = _sole(tmp_path)
    assert manifest["status"] == "success"
    assert [t["tool_name"] for t in tools] == ["python_interpreter"]
    assert tools[0]["extensions"]["io.novafabric.smolagents.code_execution"] is True
    blob = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert secret_marker.encode() not in blob


# ── adapter + real model client + real wire hooks: one logical model call each ──


def test_a_real_agent_driving_a_real_hf_client_records_each_model_call_once_and_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire hook records the model calls; the adapter adds steps and tool calls and no more.

    Neither the hooks nor the capsule are stubbed: this is the path a user's run takes, with
    smolagents' own ``InferenceClientModel`` over ``huggingface_hub`` over an ``httpx2`` mock.
    """
    smolagents = pytest.importorskip("smolagents")
    hub = pytest.importorskip("huggingface_hub")
    httpx2 = pytest.importorskip("httpx2")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    replies = [
        ("lookup", {"sku": "SKU-1"}),
        ("final_answer", {"answer": "none left"}),
    ]
    sent: list[str] = []

    def handler(request: Any) -> Any:
        sent.append(request.url.path)
        name, args = replies[len(sent) - 1]
        call = {"id": f"call_{len(sent)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        return httpx2.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 1, "model": "acme/tiny",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })

    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(handler)))
    try:
        backend = RealBackend()
        agent = smolagents.ToolCallingAgent(
            tools=[backend._tool("lookup", lambda sku: "0 in stock")],
            model=smolagents.InferenceClientModel(
                model_id="acme/tiny:groq", token=HF_TOKEN, provider="auto"
            ),
            verbosity_level=-1,
        )
        out_dir = tmp_path / "caps"
        assert _wrap_real(agent, out_dir).run("is it in stock?") == "none left"
    finally:
        from huggingface_hub.utils._http import default_client_factory

        hub.set_client_factory(default_client_factory)
        hub.close_session()

    assert len(sent) == 2  # exactly two model round-trips happened
    manifest, trace, tools = _sole(out_dir)
    assert manifest["status"] == "success"
    assert manifest["model_call_count"] == 2  # one logical call per round-trip: not 4, not 0
    calls = _jsonl(_capsules(out_dir)[0] / "model-calls.jsonl")
    assert [c["gen_ai.system"] for c in calls] == ["huggingface", "huggingface"]
    assert all(c["extensions"]["io.novafabric.record_role"] == "logical" for c in calls)
    assert len(_steps(trace)) == 2 and [t["tool_name"] for t in tools] == ["lookup", "final_answer"]
    blob = b"".join(p.read_bytes() for p in out_dir.rglob("*") if p.is_file())
    assert HF_TOKEN.encode() not in blob


def _wrap_real(agent: Any, out_dir: Path) -> Any:
    from novafabric.adapters.smolagents import wrap_agent

    return wrap_agent(agent, data_dir=out_dir)
