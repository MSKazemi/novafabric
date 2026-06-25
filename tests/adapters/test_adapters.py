"""Tests for novafabric framework adapters.

None of the target frameworks (langgraph, autogen, crewai, dspy) need to be
installed.  All framework objects are mocked.
"""
from __future__ import annotations

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
        assert manifest["capture_mode"] == "adapter-langgraph"
        assert manifest["status"] == "success"
        assert manifest["tags"]["framework"] == "langgraph"

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
        assert manifest["capture_mode"] == "adapter-autogen"
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
        assert manifest["capture_mode"] == "adapter-crewai"
        assert manifest["tags"]["framework"] == "crewai"

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
        assert manifest["capture_mode"] == "adapter-dspy"
        assert manifest["tags"]["framework"] == "dspy"

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
        assert manifest["capture_mode"] == "adapter-openai-agents"
        assert manifest["tags"]["framework"] == "openai-agents"

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
        assert manifest["capture_mode"] == "adapter-google-adk"

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
        assert manifest["capture_mode"] == "adapter-bedrock-agentcore"

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
        """before+after send_message calls must produce a capsule."""
        import asyncio

        from novafabric.adapters.a2a import NovaA2AInterceptor

        interceptor = NovaA2AInterceptor(tmp_path)

        mock_agent_card = MagicMock()
        mock_agent_card.name = "test-agent"

        before_args = MagicMock()
        before_args.method = "send_message"
        before_args.agent_card = mock_agent_card
        before_args.input = {"message": {"parts": [{"text": "hello"}]}}

        after_args = MagicMock()
        after_args.method = "send_message"
        after_args.agent_card = mock_agent_card
        after_args.result = {"status": {"state": "completed"}}

        with (
            patch("novafabric.adapters.a2a.capture_environment", return_value={}),
            patch("novafabric.adapters.a2a.SecretScannerV0") as MockScanner,
            patch("novafabric.capture.hooks.install_all"),
            patch("novafabric.capture.hooks.uninstall_all"),
        ):
            MockScanner.return_value.scan_and_redact.return_value = {}
            asyncio.run(interceptor.before(before_args))
            # Link after_args to the key set by before()
            after_args._nova_key = before_args._nova_key
            asyncio.run(interceptor.after(after_args))

        capsules = list(tmp_path.rglob("capsule.yaml"))
        assert len(capsules) == 1
        manifest = yaml.safe_load(capsules[0].read_text())
        assert manifest["capture_mode"] == "adapter-a2a"

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
