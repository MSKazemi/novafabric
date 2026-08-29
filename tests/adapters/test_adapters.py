"""Tests for novafabric framework adapters.

None of the target frameworks (langgraph, autogen, crewai, dspy) need to be
installed.  All framework objects are mocked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_capsule_infra(tmp_path: Path):
    """Return (writer_mock, install_mock, uninstall_mock) pre-wired to write
    the minimum capsule files so that _write_capsule() can succeed."""

    # We don't mock CapsuleWriter — we let it run for real so the file I/O
    # tests work.  But we DO stub out install_all/uninstall_all (no real SDKs
    # available in CI).
    install_mock = MagicMock()
    uninstall_mock = MagicMock()
    return install_mock, uninstall_mock


# ---------------------------------------------------------------------------
# LangGraph adapter — ImportError when not installed
# ---------------------------------------------------------------------------

class TestLangGraphImportError:
    def test_wrap_raises_when_langgraph_missing(self) -> None:
        with patch.dict(sys.modules, {"langgraph": None}):
            # Force reimport so the ImportError path is hit
            import importlib

            import novafabric.adapters.langgraph as _m
            importlib.reload(_m)

            with pytest.raises(ImportError, match="langgraph"):
                _m.wrap(MagicMock())


# ---------------------------------------------------------------------------
# LangGraph adapter — happy path
# ---------------------------------------------------------------------------

class TestLangGraphWrap:
    def test_returns_object_with_invoke_and_stream(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap

            with (
                patch("novafabric.adapters.langgraph.capture_environment", return_value={}),  # noqa: SIM117
                patch("novafabric.adapters.langgraph.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap(mock_graph, data_dir=tmp_path)

            assert hasattr(wrapped, "invoke")
            assert hasattr(wrapped, "stream")

    def test_invoke_calls_inner_graph(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"answer": 42}

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap

            with (
                patch("novafabric.adapters.langgraph.capture_environment", return_value={}),
                patch("novafabric.adapters.langgraph.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap(mock_graph, data_dir=tmp_path)
                result = wrapped.invoke({"input": "hello"})

        assert result == {"answer": 42}
        mock_graph.invoke.assert_called_once()

    def test_invoke_creates_capsule_yaml(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {}

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap

            with (
                patch("novafabric.adapters.langgraph.capture_environment", return_value={}),
                patch("novafabric.adapters.langgraph.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap(mock_graph, run_name="test-wf", data_dir=tmp_path)
                wrapped.invoke({"x": 1})

        capsule_dirs = list(tmp_path.iterdir())
        assert len(capsule_dirs) == 1
        capsule_yaml = capsule_dirs[0] / "capsule.yaml"
        assert capsule_yaml.exists()
        manifest = yaml.safe_load(capsule_yaml.read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["status"] == "success"
        assert manifest["metadata"]["framework"] == "langgraph"

    def test_invoke_records_failure(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("graph boom")

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap

            with (
                patch("novafabric.adapters.langgraph.capture_environment", return_value={}),
                patch("novafabric.adapters.langgraph.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap(mock_graph, data_dir=tmp_path)

                with pytest.raises(RuntimeError, match="graph boom"):
                    wrapped.invoke({})

        capsule_dirs = list(tmp_path.iterdir())
        manifest = yaml.safe_load((capsule_dirs[0] / "capsule.yaml").read_text())
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "RuntimeError"

    def test_stream_calls_inner_graph(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.stream.return_value = iter([{"chunk": 1}, {"chunk": 2}])

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap

            with (
                patch("novafabric.adapters.langgraph.capture_environment", return_value={}),
                patch("novafabric.adapters.langgraph.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap(mock_graph, data_dir=tmp_path)
                chunks = list(wrapped.stream({"x": 1}))

        assert chunks == [{"chunk": 1}, {"chunk": 2}]
        mock_graph.stream.assert_called_once()

    def test_getattr_delegates_to_inner(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.some_custom_attr = "hello"

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap
            wrapped = wrap(mock_graph, data_dir=tmp_path)

        assert wrapped.some_custom_attr == "hello"

    def test_run_name_appears_in_capsule(self, tmp_path: Path) -> None:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {}

        with patch.dict(sys.modules, {"langgraph": MagicMock()}):
            from novafabric.adapters.langgraph import wrap

            with (
                patch("novafabric.adapters.langgraph.capture_environment", return_value={}),
                patch("novafabric.adapters.langgraph.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap(mock_graph, run_name="my-custom-name", data_dir=tmp_path)
                wrapped.invoke({})

        manifest = yaml.safe_load(
            next(tmp_path.iterdir(), None / "capsule.yaml")  # type: ignore[operator]
            if False
            else (list(tmp_path.iterdir())[0] / "capsule.yaml").read_text()
        )
        assert "@langgraph:my-custom-name" in manifest["command"][0]


# ---------------------------------------------------------------------------
# AutoGen adapter — ImportError when not installed
# ---------------------------------------------------------------------------

class TestAutoGenImportError:
    def test_wrap_agent_raises_when_autogen_missing(self) -> None:
        with patch.dict(sys.modules, {"autogen": None}):
            import importlib

            import novafabric.adapters.autogen as _m
            importlib.reload(_m)

            with pytest.raises(ImportError, match="autogen"):
                _m.wrap_agent(MagicMock())


# ---------------------------------------------------------------------------
# AutoGen adapter — happy path
# ---------------------------------------------------------------------------

class TestAutoGenWrap:
    def test_patches_initiate_chat(self) -> None:
        mock_agent: Any = MagicMock()
        mock_agent.name = "AssistantAgent"
        original_initiate = mock_agent.initiate_chat

        with patch.dict(sys.modules, {"autogen": MagicMock()}):
            from novafabric.adapters.autogen import wrap_agent
            wrap_agent(mock_agent)

        # initiate_chat must have been replaced
        assert mock_agent.initiate_chat is not original_initiate

    def test_initiate_chat_calls_original(self, tmp_path: Path) -> None:
        mock_agent: Any = MagicMock()
        mock_agent.name = "TestAgent"
        mock_recipient = MagicMock()

        # Capture a reference to the original mock before wrapping replaces it.
        original_initiate = mock_agent.initiate_chat

        with patch.dict(sys.modules, {"autogen": MagicMock()}):
            from novafabric.adapters.autogen import wrap_agent

            with (
                patch("novafabric.adapters.autogen.capture_environment", return_value={}),
                patch("novafabric.adapters.autogen.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_agent(mock_agent, run_name="autogen-test", data_dir=tmp_path)
                # After wrapping, mock_agent.initiate_chat is our wrapper function.
                # Calling it should delegate to the original mock.
                mock_agent.initiate_chat(mock_recipient, message="hi")

        # The original initiate_chat mock was called once via the wrapper.
        original_initiate.assert_called_once()

    def test_initiate_chat_creates_capsule(self, tmp_path: Path) -> None:
        original_call_result = {"reply": "done"}
        mock_agent: Any = MagicMock()
        mock_agent.name = "Bot"
        mock_agent.initiate_chat.return_value = original_call_result

        recipient = MagicMock()

        with patch.dict(sys.modules, {"autogen": MagicMock()}):
            from novafabric.adapters.autogen import wrap_agent

            with (
                patch("novafabric.adapters.autogen.capture_environment", return_value={}),
                patch("novafabric.adapters.autogen.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_agent(mock_agent, run_name="bot-run", data_dir=tmp_path)
                # After wrapping, initiate_chat is the wrapper; call it to trigger capture.
                mock_agent.initiate_chat(recipient, message="go")

        capsule_dirs = list(tmp_path.iterdir())
        assert len(capsule_dirs) == 1
        manifest = yaml.safe_load((capsule_dirs[0] / "capsule.yaml").read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "autogen"
        assert manifest["status"] == "success"


# ---------------------------------------------------------------------------
# CrewAI adapter — ImportError when not installed
# ---------------------------------------------------------------------------

class TestCrewAIImportError:
    def test_wrap_crew_raises_when_crewai_missing(self) -> None:
        with patch.dict(sys.modules, {"crewai": None}):
            import importlib

            import novafabric.adapters.crewai as _m
            importlib.reload(_m)

            with pytest.raises(ImportError, match="crewai"):
                _m.wrap_crew(MagicMock())


# ---------------------------------------------------------------------------
# CrewAI adapter — happy path
# ---------------------------------------------------------------------------

class TestCrewAIWrap:
    def test_patches_kickoff(self) -> None:
        mock_crew: Any = MagicMock()
        original_kickoff = mock_crew.kickoff

        with patch.dict(sys.modules, {"crewai": MagicMock()}):
            from novafabric.adapters.crewai import wrap_crew
            wrap_crew(mock_crew)

        assert mock_crew.kickoff is not original_kickoff

    def test_kickoff_creates_capsule(self, tmp_path: Path) -> None:
        mock_crew: Any = MagicMock()
        mock_crew.kickoff.return_value = "crew result"

        with patch.dict(sys.modules, {"crewai": MagicMock()}):
            from novafabric.adapters.crewai import wrap_crew

            with (
                patch("novafabric.adapters.crewai.capture_environment", return_value={}),
                patch("novafabric.adapters.crewai.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_crew(mock_crew, run_name="my-crew", data_dir=tmp_path)
                mock_crew.kickoff()

        capsule_dirs = list(tmp_path.iterdir())
        assert len(capsule_dirs) == 1
        manifest = yaml.safe_load((capsule_dirs[0] / "capsule.yaml").read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "crewai"

    def test_kickoff_records_failure(self, tmp_path: Path) -> None:
        mock_crew: Any = MagicMock()
        mock_crew.kickoff.side_effect = ValueError("crew exploded")

        with patch.dict(sys.modules, {"crewai": MagicMock()}):
            from novafabric.adapters.crewai import wrap_crew

            with (
                patch("novafabric.adapters.crewai.capture_environment", return_value={}),
                patch("novafabric.adapters.crewai.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_crew(mock_crew, data_dir=tmp_path)
                with pytest.raises(ValueError, match="crew exploded"):
                    mock_crew.kickoff()

        manifest = yaml.safe_load(
            (list(tmp_path.iterdir())[0] / "capsule.yaml").read_text()
        )
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "ValueError"


# ---------------------------------------------------------------------------
# DSPy adapter — ImportError when not installed
# ---------------------------------------------------------------------------

class TestDSPyImportError:
    def test_wrap_program_raises_when_dspy_missing(self) -> None:
        with patch.dict(sys.modules, {"dspy": None}):
            import importlib

            import novafabric.adapters.dspy as _m
            importlib.reload(_m)

            with pytest.raises(ImportError, match="dspy"):
                _m.wrap_program(MagicMock())


# ---------------------------------------------------------------------------
# DSPy adapter — happy path
# ---------------------------------------------------------------------------

class TestDSPyWrap:
    def test_patches_forward(self) -> None:
        mock_program: Any = MagicMock()
        original_forward = mock_program.forward

        with patch.dict(sys.modules, {"dspy": MagicMock()}):
            from novafabric.adapters.dspy import wrap_program
            wrap_program(mock_program)

        assert mock_program.forward is not original_forward

    def test_forward_creates_capsule(self, tmp_path: Path) -> None:
        mock_program: Any = MagicMock()
        mock_program.forward.return_value = {"answer": "42"}

        with patch.dict(sys.modules, {"dspy": MagicMock()}):
            from novafabric.adapters.dspy import wrap_program

            with (
                patch("novafabric.adapters.dspy.capture_environment", return_value={}),
                patch("novafabric.adapters.dspy.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_program(mock_program, run_name="my-chain", data_dir=tmp_path)
                mock_program.forward(question="What is 2+2?")

        capsule_dirs = list(tmp_path.iterdir())
        assert len(capsule_dirs) == 1
        manifest = yaml.safe_load((capsule_dirs[0] / "capsule.yaml").read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "dspy"

    def test_forward_records_failure(self, tmp_path: Path) -> None:
        mock_program: Any = MagicMock()
        mock_program.forward.side_effect = TypeError("bad input")

        with patch.dict(sys.modules, {"dspy": MagicMock()}):
            from novafabric.adapters.dspy import wrap_program

            with (
                patch("novafabric.adapters.dspy.capture_environment", return_value={}),
                patch("novafabric.adapters.dspy.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_program(mock_program, data_dir=tmp_path)
                with pytest.raises(TypeError, match="bad input"):
                    mock_program.forward("x")

        manifest = yaml.safe_load(
            (list(tmp_path.iterdir())[0] / "capsule.yaml").read_text()
        )
        assert manifest["status"] == "failure"
        assert manifest["error"]["type"] == "TypeError"

    def test_forward_returns_value(self, tmp_path: Path) -> None:
        mock_program: Any = MagicMock()
        mock_program.forward.return_value = {"prediction": "blue"}

        with patch.dict(sys.modules, {"dspy": MagicMock()}):
            from novafabric.adapters.dspy import wrap_program

            with (
                patch("novafabric.adapters.dspy.capture_environment", return_value={}),
                patch("novafabric.adapters.dspy.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrap_program(mock_program, data_dir=tmp_path)
                result = mock_program.forward(x=1)

        assert result == {"prediction": "blue"}


# ---------------------------------------------------------------------------
# Top-level __init__ aliases
# ---------------------------------------------------------------------------

class TestTopLevelAliases:
    def test_wrap_langgraph_alias_raises_without_framework(self) -> None:
        with patch.dict(sys.modules, {"langgraph": None}):
            import importlib

            import novafabric.adapters.langgraph as _m
            importlib.reload(_m)

            import novafabric.adapters as _pkg
            with pytest.raises(ImportError):
                _pkg.wrap_langgraph(MagicMock())

    def test_wrap_autogen_alias_raises_without_framework(self) -> None:
        with patch.dict(sys.modules, {"autogen": None}):
            import importlib

            import novafabric.adapters.autogen as _m
            importlib.reload(_m)

            import novafabric.adapters as _pkg
            with pytest.raises(ImportError):
                _pkg.wrap_autogen(MagicMock())

    def test_wrap_crewai_alias_raises_without_framework(self) -> None:
        with patch.dict(sys.modules, {"crewai": None}):
            import importlib

            import novafabric.adapters.crewai as _m
            importlib.reload(_m)

            import novafabric.adapters as _pkg
            with pytest.raises(ImportError):
                _pkg.wrap_crewai(MagicMock())

    def test_wrap_dspy_alias_raises_without_framework(self) -> None:
        with patch.dict(sys.modules, {"dspy": None}):
            import importlib

            import novafabric.adapters.dspy as _m
            importlib.reload(_m)

            import novafabric.adapters as _pkg
            with pytest.raises(ImportError):
                _pkg.wrap_dspy(MagicMock())


# ---------------------------------------------------------------------------
# OpenAI Agents SDK adapter
# ---------------------------------------------------------------------------

class TestOpenAIAgentsImportError:
    def test_register_raises_when_not_installed(self, tmp_path: Path) -> None:
        with patch.dict(sys.modules, {"agents": None, "agents.tracing": None}):
            import importlib

            import novafabric.adapters.openai_agents as _m
            importlib.reload(_m)
            with pytest.raises(ImportError, match="openai-agents"):
                _m.register(data_dir=tmp_path)


class TestOpenAIAgentsProcessor:
    def test_processor_creates_capsule_on_trace_end(self, tmp_path: Path) -> None:
        """on_trace_end must write capsule.yaml to data_dir."""
        from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor

        mock_trace = MagicMock()
        mock_trace.trace_id = "trace-001"
        mock_trace.workflow_name = "test-workflow"

        processor = NovaCapsuleTracingProcessor(tmp_path)

        with (
            patch("novafabric.adapters.openai_agents.capture_environment", return_value={}),
            patch("novafabric.adapters.openai_agents.SecretScannerV0") as MockScanner,
            patch("novafabric.capture.hooks.install_all"),
            patch("novafabric.capture.hooks.uninstall_all"),
        ):
            MockScanner.return_value.scan_and_redact.return_value = {}
            processor.on_trace_start(mock_trace)
            processor.on_trace_end(mock_trace)

        capsules = list(tmp_path.rglob("capsule.yaml"))
        assert len(capsules) == 1
        manifest = yaml.safe_load(capsules[0].read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "openai-agents"

    def test_span_callbacks_do_not_raise(self, tmp_path: Path) -> None:
        from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor
        processor = NovaCapsuleTracingProcessor(tmp_path)
        # These must not raise even with no active trace
        processor.on_span_start(MagicMock())
        processor.on_span_end(MagicMock())
        processor.shutdown()
        processor.force_flush()

    def test_trace_end_without_start_is_safe(self, tmp_path: Path) -> None:
        from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor
        processor = NovaCapsuleTracingProcessor(tmp_path)
        mock_trace = MagicMock()
        mock_trace.trace_id = "nonexistent-trace"
        # Must not raise when on_trace_start was never called
        processor.on_trace_end(mock_trace)
        assert list(tmp_path.rglob("capsule.yaml")) == []


# ---------------------------------------------------------------------------
# Google ADK adapter
# ---------------------------------------------------------------------------

class TestGoogleAdkImportError:
    def test_make_plugin_raises_when_not_installed(self, tmp_path: Path) -> None:
        with patch.dict(sys.modules, {"google": None, "google.adk": None,
                                       "google.adk.plugins": None,
                                       "google.adk.plugins.base_plugin": None}):
            import importlib

            import novafabric.adapters.google_adk as _m
            importlib.reload(_m)
            with pytest.raises(ImportError, match="google-adk"):
                _m.make_plugin(data_dir=tmp_path)


class TestGoogleAdkPlugin:
    def test_plugin_creates_capsule_after_run(self, tmp_path: Path) -> None:
        """after_run_callback must write capsule.yaml."""
        import asyncio

        from novafabric.adapters.google_adk import NovaAdkPlugin

        plugin = NovaAdkPlugin(tmp_path)
        mock_ctx = MagicMock()

        with (
            patch("novafabric.adapters.google_adk.capture_environment", return_value={}),
            patch("novafabric.adapters.google_adk.SecretScannerV0") as MockScanner,
            patch("novafabric.capture.hooks.install_all"),
            patch("novafabric.capture.hooks.uninstall_all"),
        ):
            MockScanner.return_value.scan_and_redact.return_value = {}
            asyncio.run(plugin.before_run_callback(mock_ctx))
            asyncio.run(plugin.after_run_callback(mock_ctx))

        capsules = list(tmp_path.rglob("capsule.yaml"))
        assert len(capsules) == 1
        manifest = yaml.safe_load(capsules[0].read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "google-adk"

    def test_after_run_without_before_is_safe(self, tmp_path: Path) -> None:
        import asyncio

        from novafabric.adapters.google_adk import NovaAdkPlugin
        plugin = NovaAdkPlugin(tmp_path)
        # Must not raise even without a preceding before_run_callback
        asyncio.run(plugin.after_run_callback(MagicMock()))
        assert list(tmp_path.rglob("capsule.yaml")) == []


# ---------------------------------------------------------------------------
# Bedrock AgentCore adapter
# ---------------------------------------------------------------------------

class TestBedrockAgentCoreImportError:
    def test_wrap_raises_when_boto3_missing(self, tmp_path: Path) -> None:
        with patch.dict(sys.modules, {"boto3": None}):
            import importlib

            import novafabric.adapters.bedrock_agentcore as _m
            importlib.reload(_m)
            with pytest.raises(ImportError, match="boto3"):
                _m.wrap_client(MagicMock(), data_dir=tmp_path)


class TestBedrockAgentCoreAdapter:
    def test_invoke_agent_creates_capsule(self, tmp_path: Path) -> None:
        """invoke_agent must create a capsule with Bedrock trace events."""
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            from novafabric.adapters.bedrock_agentcore import wrap_client

            mock_client = MagicMock()
            # Simulate a streaming response with an orchestration trace chunk
            mock_event_stream = [
                {"chunk": {"bytes": b"Hello"}},
                {"trace": {"orchestrationTrace": {"invocationInput": {"actionGroupInvocationInput": {"actionGroupName": "TestAction"}}}}},
            ]
            mock_client.invoke_agent.return_value = {"completion": iter(mock_event_stream)}

            with (
                patch("novafabric.adapters.bedrock_agentcore.capture_environment", return_value={}),
                patch("novafabric.adapters.bedrock_agentcore.SecretScannerV0") as MockScanner,
                patch("novafabric.capture.hooks.install_all"),
                patch("novafabric.capture.hooks.uninstall_all"),
            ):
                MockScanner.return_value.scan_and_redact.return_value = {}
                wrapped = wrap_client(mock_client, data_dir=tmp_path)
                result = wrapped.invoke_agent(agentId="agent-1", agentAliasId="alias-1",
                                               sessionId="sess-1", inputText="hello")
                # Drain the generator
                chunks = list(result["completion"])

        assert len(chunks) == 2
        capsules = list(tmp_path.rglob("capsule.yaml"))
        assert len(capsules) == 1
        manifest = yaml.safe_load(capsules[0].read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "bedrock-agentcore"

    def test_passthrough_for_non_invoke_methods(self, tmp_path: Path) -> None:
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            from novafabric.adapters.bedrock_agentcore import wrap_client
            mock_client = MagicMock()
            mock_client.list_agents.return_value = {"agents": []}
            wrapped = wrap_client(mock_client, data_dir=tmp_path)
            result = wrapped.list_agents()
            assert result == {"agents": []}


# ---------------------------------------------------------------------------
# A2A adapter
# ---------------------------------------------------------------------------

class TestA2AImportError:
    def test_make_interceptor_raises_when_not_installed(self, tmp_path: Path) -> None:
        with patch.dict(sys.modules, {"a2a": None, "a2a.client": None,
                                       "a2a.client.interceptors": None}):
            import importlib

            import novafabric.adapters.a2a as _m
            importlib.reload(_m)
            with pytest.raises(ImportError, match="a2a-sdk"):
                _m.make_interceptor(data_dir=tmp_path)


class TestA2AInterceptor:
    def test_interceptor_creates_capsule_per_send_message(self, tmp_path: Path) -> None:
        """before+after send_message calls must produce a capsule.

        Uses the SDK's real ``BeforeArgs``/``AfterArgs`` rather than mocks,
        because the whole correlation problem lives in the fact that they are
        two *separate* objects: ``BaseClient._execute_with_interceptors``
        builds a fresh ``AfterArgs`` from the transport result and never
        carries anything the interceptor stashed on the ``before`` args.

        An earlier version of this test used ``MagicMock`` and hand-copied
        ``after_args._nova_key = before_args._nova_key``, which is a step the
        SDK does not perform — so it asserted a handoff that never happens in
        production. It went unnoticed because ``tests/a2a/`` shadowed the
        installed ``a2a`` distribution, making the real dataclasses
        unimportable under pytest.
        """
        import asyncio

        from a2a.client.interceptors import AfterArgs, BeforeArgs

        from novafabric.adapters.a2a import NovaA2AInterceptor

        interceptor = NovaA2AInterceptor(tmp_path)

        mock_agent_card = MagicMock()
        mock_agent_card.name = "test-agent"

        before_args = BeforeArgs(
            input={"message": {"parts": [{"text": "hello"}]}},
            method="send_message",
            agent_card=mock_agent_card,
        )
        after_args = AfterArgs(
            result={"status": {"state": "completed"}},
            method="send_message",
            agent_card=mock_agent_card,
        )

        async def _one_call() -> None:
            # Both hooks awaited in one task, exactly as the SDK does it.
            await interceptor.before(before_args)
            await interceptor.after(after_args)

        with (
            patch("novafabric.adapters.a2a.capture_environment", return_value={}),
            patch("novafabric.adapters.a2a.SecretScannerV0") as MockScanner,
            patch("novafabric.capture.hooks.install_all"),
            patch("novafabric.capture.hooks.uninstall_all"),
        ):
            MockScanner.return_value.scan_and_redact.return_value = {}
            asyncio.run(_one_call())

        assert not hasattr(after_args, "_nova_key"), (
            "the SDK builds AfterArgs independently; if this ever holds the key "
            "the test is fabricating the handoff instead of exercising it"
        )
        capsules = list(tmp_path.rglob("capsule.yaml"))
        assert len(capsules) == 1
        manifest = yaml.safe_load(capsules[0].read_text())
        assert manifest["capture_mode"] == "sdk-decorator"
        assert manifest["metadata"]["framework"] == "a2a"

    def test_concurrent_calls_do_not_cross_pair(self, tmp_path: Path) -> None:
        """A response must land in the capsule of *its own* request.

        Two calls are interleaved so that they complete in the reverse of the
        order they started. The old fallback picked "the first still-pending
        entry for this method", which under that interleaving writes agent-b's
        response into agent-a's capsule — a silent evidence-integrity fault,
        the worst possible kind for a provenance tool.

        Both capsules still get written either way, so the assertion has to be
        on the request/response *pairing* inside each capsule, not on the count.
        """
        import asyncio

        from a2a.client.interceptors import AfterArgs, BeforeArgs

        from novafabric.adapters.a2a import NovaA2AInterceptor

        interceptor = NovaA2AInterceptor(tmp_path)

        def _card(name: str) -> MagicMock:
            card = MagicMock()
            card.name = name
            return card

        b_started = asyncio.Event()
        b_finished = asyncio.Event()

        async def _call_a() -> None:
            await interceptor.before(
                BeforeArgs(
                    input={"agent": "agent-a"},
                    method="send_message",
                    agent_card=_card("agent-a"),
                )
            )
            await b_started.wait()   # both captures now outstanding
            await b_finished.wait()  # b completes first: start order != finish order
            await interceptor.after(
                AfterArgs(
                    result={"agent": "agent-a"},
                    method="send_message",
                    agent_card=_card("agent-a"),
                )
            )

        async def _call_b() -> None:
            await interceptor.before(
                BeforeArgs(
                    input={"agent": "agent-b"},
                    method="send_message",
                    agent_card=_card("agent-b"),
                )
            )
            b_started.set()
            await interceptor.after(
                AfterArgs(
                    result={"agent": "agent-b"},
                    method="send_message",
                    agent_card=_card("agent-b"),
                )
            )
            b_finished.set()

        async def _both() -> None:
            await asyncio.gather(_call_a(), _call_b())

        with (
            patch("novafabric.adapters.a2a.capture_environment", return_value={}),
            patch("novafabric.adapters.a2a.SecretScannerV0") as MockScanner,
            patch("novafabric.capture.hooks.install_all"),
            patch("novafabric.capture.hooks.uninstall_all"),
        ):
            MockScanner.return_value.scan_and_redact.return_value = {}
            asyncio.run(_both())

        task_logs = sorted(tmp_path.rglob("a2a-tasks.jsonl"))
        assert len(task_logs) == 2, "each concurrent call must get its own capsule"
        for log in task_logs:
            entries = [json.loads(line) for line in log.read_text().splitlines() if line]
            request = next(e for e in entries if e["direction"] == "request")
            response = next(e for e in entries if e["direction"] == "response")
            assert response["result"]["agent"] == request["input"]["agent"], (
                "response was filed under a different call's capsule: "
                f"request={request['input']} response={response['result']}"
            )

    def test_concurrent_calls_do_not_tear_down_each_others_hooks(
        self, tmp_path: Path
    ) -> None:
        """The first call to finish must not end wire capture for the others.

        ``capture.hooks`` keeps ONE module-level ``_installed`` list and ONE
        recorder singleton. Both A2A hooks used to drive it unconditionally, so
        two concurrent calls stacked two patch layers and then the first
        ``after()`` ran ``uninstall_all()`` and removed both — wire-level
        capture for the in-flight call stopped silently, mid-run.

        Note what this test does *not* do: patch out ``install_all`` /
        ``uninstall_all``. The pre-existing interleaving test does, which is
        exactly why it could never see this. Here they are counted for real.
        """
        import asyncio

        from a2a.client.interceptors import AfterArgs, BeforeArgs

        import novafabric.capture.hooks as hooks_mod
        from novafabric.adapters.a2a import NovaA2AInterceptor

        interceptor = NovaA2AInterceptor(tmp_path)

        installs: list[str] = []
        uninstalls: list[str] = []
        # Observe the real call sites without letting them patch live SDKs, but
        # mimic the REAL ownership contract (ADR-0224): install_all returns an
        # owner token to the capture that claims the hooks and a *participant*
        # token to one that finds them already owned, and uninstall_all only
        # tears down for the owner. A fake that drifts from the real contract
        # proves nothing about the behaviour under test — so the prefixes are
        # taken from the module rather than hardcoded here, and this fake fails
        # loudly if they are ever renamed.
        _owner: list[str] = []

        def _fake_install(**kw: object) -> str:
            installs.append(str(kw["parent_span_id"]))
            if _owner:
                return hooks_mod._PARTICIPANT_PREFIX + str(kw["parent_span_id"])
            _owner.append(hooks_mod._OWNER_PREFIX + str(kw["parent_span_id"]))
            return _owner[0]

        def _fake_uninstall(token: str | None = None) -> bool:
            if token and _owner and token == _owner[0]:
                _owner.clear()
                uninstalls.append(token)
                return True
            return False

        with (
            patch.object(hooks_mod, "install_all", side_effect=_fake_install),
            patch.object(hooks_mod, "uninstall_all", side_effect=_fake_uninstall),
            patch("novafabric.adapters.a2a.capture_environment", return_value={}),
            patch("novafabric.adapters.a2a.SecretScannerV0") as MockScanner,
        ):
            MockScanner.return_value.scan_and_redact.return_value = {}

            def _card(name: str) -> MagicMock:
                card = MagicMock()
                card.name = name
                return card

            b_done = asyncio.Event()

            async def _call_a() -> None:
                await interceptor.before(BeforeArgs(
                    input={"agent": "a"}, method="send_message",
                    agent_card=_card("agent-a")))
                await b_done.wait()
                # B has fully finished. A is still in flight, so the hooks A
                # installed must still be in place.
                assert not uninstalls, (
                    "the concurrent call's after() tore down the hooks that "
                    "belong to a capture still in flight"
                )
                await interceptor.after(AfterArgs(
                    result={"agent": "a"}, method="send_message",
                    agent_card=_card("agent-a")))

            async def _call_b() -> None:
                await interceptor.before(BeforeArgs(
                    input={"agent": "b"}, method="send_message",
                    agent_card=_card("agent-b")))
                await interceptor.after(AfterArgs(
                    result={"agent": "b"}, method="send_message",
                    agent_card=_card("agent-b")))
                b_done.set()

            async def _both() -> None:
                await asyncio.gather(_call_a(), _call_b())

            asyncio.run(_both())

        # Both calls now REACH install_all — the guard moved into it (ADR-0224),
        # so safety no longer depends on each adapter remembering to ask first.
        # What must hold is that only one of them ends up owning the hooks.
        assert len(installs) == 2, (
            f"both concurrent calls should reach install_all, got {len(installs)}"
        )
        assert len(_owner) == 0, "ownership leaked — the next capture gets no wire capture"
        assert len(uninstalls) == 1, (
            f"exactly one capture may tear the hooks down, got {len(uninstalls)} — "
            "more than one means a call still in flight lost its wire capture"
        )

        # Both capsules exist, and each says truthfully whether wire-level
        # capture was active for it.
        manifests = [
            yaml.safe_load(p.read_text()) for p in sorted(tmp_path.glob("*/capsule.yaml"))
        ]
        assert len(manifests) == 2, manifests
        states = sorted(m["metadata"]["wire_capture"] for m in manifests)
        # ADR-0224 phase 2: the capture that loses the hook race no longer
        # records nothing. It binds its own recorder and writer, so its events
        # reach its own capsule through the winner's single patch layer, and it
        # says so — `scoped-concurrent`, not `skipped-concurrent`.
        assert states == ["installed", "scoped-concurrent"], states

    def test_non_send_message_calls_are_passthrough(self, tmp_path: Path) -> None:
        import asyncio

        from novafabric.adapters.a2a import NovaA2AInterceptor
        interceptor = NovaA2AInterceptor(tmp_path)
        before_args = MagicMock()
        before_args.method = "get_task"
        before_args.agent_card = MagicMock()
        # Must not raise or create state
        asyncio.run(interceptor.before(before_args))
        asyncio.run(interceptor.after(MagicMock(method="get_task")))
        assert list(tmp_path.rglob("capsule.yaml")) == []
