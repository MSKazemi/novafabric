"""ADR-0209 D2 adapter wirings — OpenAI Agents guardrails, LangGraph state
transitions — tested with fake framework objects (no SDKs required).

Follows the faking conventions of ``tests/adapters/test_adapters.py``:
LangGraph via ``patch.dict(sys.modules)`` + a MagicMock graph; the OpenAI
Agents tracing processor is instantiated directly (its base class degrades
to ``object`` when the SDK is absent) and fed duck-typed span objects.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.capture.event_recorder import EventRecorder, set_current_recorder


@pytest.fixture(autouse=True)
def _clean_recorder_singleton() -> Any:
    set_current_recorder(None)
    yield
    set_current_recorder(None)


def _digest(obj: Any) -> str:
    """Independent reimplementation of the spec's canonical-JSON digest."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _events(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


# ── OpenAI Agents: guardrail spans → guardrail_events.jsonl ───────────────


class TestOpenAIAgentsGuardrailWiring:
    @pytest.fixture()
    def recorder_dir(self, tmp_path: Path) -> Path:
        cap_dir = tmp_path / "cap"
        cap_dir.mkdir()
        set_current_recorder(
            EventRecorder(capsule_dir=cap_dir, run_id="r1", capsule_id="r1")
        )
        return cap_dir

    @pytest.fixture()
    def processor(self, tmp_path: Path) -> Any:
        from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor

        return NovaCapsuleTracingProcessor(tmp_path / "runs")

    @pytest.mark.parametrize(
        ("triggered", "outcome"), [(True, "blocked"), (False, "passed")]
    )
    def test_guardrail_span_maps_triggered_to_outcome(
        self, processor: Any, recorder_dir: Path, triggered: bool, outcome: str
    ) -> None:
        span = SimpleNamespace(span_data=SimpleNamespace(
            type="guardrail", name="pii-filter", triggered=triggered,
        ))
        processor.on_span_end(span)
        (event,) = _events(recorder_dir / "guardrail_events.jsonl")
        assert event["event_type"] == "GuardrailEvaluated"
        assert event["guardrail_name"] == "pii-filter"
        assert event["outcome"] == outcome

    def test_unnamed_guardrail_gets_fallback_name(
        self, processor: Any, recorder_dir: Path
    ) -> None:
        span = SimpleNamespace(span_data=SimpleNamespace(
            type="guardrail", name=None, triggered=True,
        ))
        processor.on_span_end(span)
        (event,) = _events(recorder_dir / "guardrail_events.jsonl")
        assert event["guardrail_name"] == "guardrail"

    def test_non_guardrail_span_emits_nothing(
        self, processor: Any, recorder_dir: Path
    ) -> None:
        span = SimpleNamespace(span_data=SimpleNamespace(
            type="function", name="get_weather", triggered=True,
        ))
        processor.on_span_end(span)
        assert not (recorder_dir / "guardrail_events.jsonl").exists()

    def test_span_without_span_data_is_ignored(
        self, processor: Any, recorder_dir: Path
    ) -> None:
        processor.on_span_end(SimpleNamespace())  # no span_data at all
        processor.on_span_end(SimpleNamespace(span_data=None))
        assert not (recorder_dir / "guardrail_events.jsonl").exists()

    def test_no_recorder_installed_is_safe(self, processor: Any) -> None:
        span = SimpleNamespace(span_data=SimpleNamespace(
            type="guardrail", name="pii", triggered=True,
        ))
        processor.on_span_end(span)  # must not raise

    def test_hostile_span_object_is_fail_open(
        self, processor: Any, recorder_dir: Path
    ) -> None:
        class Hostile:
            @property
            def span_data(self) -> Any:
                raise RuntimeError("SDK internals changed")

        processor.on_span_end(Hostile())  # must not raise


# ── LangGraph: stream()/invoke() → state_transitions.jsonl ────────────────


def _wrapped_graph(mock_graph: MagicMock, tmp_path: Path) -> Any:
    with patch.dict(sys.modules, {"langgraph": MagicMock()}):
        from novafabric.adapters.langgraph import wrap

        return wrap(mock_graph, run_name="wire-test", data_dir=tmp_path)


def _capsule_dir(tmp_path: Path) -> Path:
    dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    return dirs[0]


_ADAPTER_PATCHES = (
    "novafabric.adapters.langgraph.capture_environment",
    "novafabric.adapters.langgraph.SecretScannerV0",
)


class TestLangGraphStateTransitionWiring:
    @pytest.fixture(autouse=True)
    def _standard_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)

    def _run_stream(
        self, tmp_path: Path, chunks: list[Any], graph_input: Any
    ) -> tuple[list[Any], Path]:
        mock_graph = MagicMock()
        mock_graph.stream.return_value = iter(chunks)
        with (
            patch(_ADAPTER_PATCHES[0], return_value={}),
            patch(_ADAPTER_PATCHES[1]) as scanner,
        ):
            scanner.return_value.scan_and_redact.return_value = {}
            wrapped = _wrapped_graph(mock_graph, tmp_path)
            yielded = list(wrapped.stream(graph_input))
        return yielded, _capsule_dir(tmp_path)

    def test_stream_emits_one_transition_per_chunk_with_digest_chain(
        self, tmp_path: Path
    ) -> None:
        graph_input = {"messages": ["start"]}
        chunks = [
            {"node_a": {"messages": ["start", "a"]}},
            {"node_b": {"messages": ["start", "a", "b"]}},
            {"node_c": {"messages": ["start", "a", "b", "c"]}},
        ]
        yielded, cap_dir = self._run_stream(tmp_path, chunks, graph_input)
        assert yielded == chunks  # wrapper stays transparent

        events = _events(cap_dir / "state_transitions.jsonl")
        assert [e["step_index"] for e in events] == [0, 1, 2]
        assert [e["agent_id"] for e in events] == ["node_a", "node_b", "node_c"]
        # Chain seeded with the digest of the invocation input…
        assert events[0]["state_digest_before"] == _digest(graph_input)
        # …then after[i] == before[i+1] (spec digest-chaining invariant).
        for prev, cur in zip(events, events[1:]):
            assert prev["state_digest_after"] == cur["state_digest_before"]
        assert events[-1]["state_digest_after"] == _digest(chunks[-1])

    def test_stream_standard_level_omits_state_payloads(
        self, tmp_path: Path
    ) -> None:
        _, cap_dir = self._run_stream(
            tmp_path, [{"n": {"x": 1}}], {"q": "hi"}
        )
        (event,) = _events(cap_dir / "state_transitions.jsonl")
        assert event["state_before"] is None
        assert event["state_after"] is None

    def test_stream_forensic_level_includes_state_payloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        graph_input = {"q": "hi"}
        chunk = {"n": {"x": 1}}
        _, cap_dir = self._run_stream(tmp_path, [chunk], graph_input)
        (event,) = _events(cap_dir / "state_transitions.jsonl")
        assert event["state_before"] == graph_input
        assert event["state_after"] == chunk

    def test_stream_non_serializable_chunk_falls_back_to_repr_digest(
        self, tmp_path: Path
    ) -> None:
        yielded, cap_dir = self._run_stream(
            tmp_path, [object()], {"q": 1}
        )
        assert len(yielded) == 1
        (event,) = _events(cap_dir / "state_transitions.jsonl")
        assert event["state_digest_after"].startswith("sha256:")
        assert event["agent_id"] is None

    def test_invoke_emits_start_end_pair(self, tmp_path: Path) -> None:
        graph_input = {"q": "hello"}
        result = {"answer": 42}
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = result
        with (
            patch(_ADAPTER_PATCHES[0], return_value={}),
            patch(_ADAPTER_PATCHES[1]) as scanner,
        ):
            scanner.return_value.scan_and_redact.return_value = {}
            wrapped = _wrapped_graph(mock_graph, tmp_path)
            assert wrapped.invoke(graph_input) == result

        cap_dir = _capsule_dir(tmp_path)
        start, end = _events(cap_dir / "state_transitions.jsonl")
        assert [start["step_index"], end["step_index"]] == [0, 1]
        # Start marker: input digest on both sides (no transition observed yet).
        assert start["state_digest_before"] == _digest(graph_input)
        assert start["state_digest_after"] == _digest(graph_input)
        # End: whole-invocation transition; chain holds.
        assert end["state_digest_before"] == start["state_digest_after"]
        assert end["state_digest_after"] == _digest(result)
        # standard level: no payloads.
        assert end["state_before"] is None
        assert end["state_after"] is None

    def test_invoke_forensic_level_includes_payloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        graph_input = {"q": "hello"}
        result = {"answer": 42}
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = result
        with (
            patch(_ADAPTER_PATCHES[0], return_value={}),
            patch(_ADAPTER_PATCHES[1]) as scanner,
        ):
            scanner.return_value.scan_and_redact.return_value = {}
            wrapped = _wrapped_graph(mock_graph, tmp_path)
            wrapped.invoke(graph_input)

        _, end = _events(_capsule_dir(tmp_path) / "state_transitions.jsonl")
        assert end["state_before"] == graph_input
        assert end["state_after"] == result

    def test_invoke_failure_still_leaves_start_marker(
        self, tmp_path: Path
    ) -> None:
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("node exploded")
        with (
            patch(_ADAPTER_PATCHES[0], return_value={}),
            patch(_ADAPTER_PATCHES[1]) as scanner,
        ):
            scanner.return_value.scan_and_redact.return_value = {}
            wrapped = _wrapped_graph(mock_graph, tmp_path)
            with pytest.raises(RuntimeError, match="node exploded"):
                wrapped.invoke({"q": 1})

        events = _events(_capsule_dir(tmp_path) / "state_transitions.jsonl")
        assert len(events) == 1  # start marker only; no fabricated end event
        assert events[0]["step_index"] == 0

    def test_stream_with_hooks_patched_out_is_a_noop_for_transitions(
        self, tmp_path: Path
    ) -> None:
        """When no recorder exists (hook install patched away, as the legacy
        adapter tests do), the wiring silently records nothing."""
        mock_graph = MagicMock()
        mock_graph.stream.return_value = iter([{"n": {"x": 1}}])
        with (
            patch(_ADAPTER_PATCHES[0], return_value={}),
            patch(_ADAPTER_PATCHES[1]) as scanner,
            patch("novafabric.capture.hooks.install_all"),
            patch("novafabric.capture.hooks.uninstall_all"),
        ):
            scanner.return_value.scan_and_redact.return_value = {}
            wrapped = _wrapped_graph(mock_graph, tmp_path)
            assert list(wrapped.stream({"q": 1})) == [{"n": {"x": 1}}]
        cap_dir = _capsule_dir(tmp_path)
        assert not (cap_dir / "state_transitions.jsonl").exists()
