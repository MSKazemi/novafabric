"""NovaFabric adapter for AWS Bedrock AgentCore.

Wraps a ``boto3`` ``bedrock-agent-runtime`` client.  ``invoke_agent()`` is
intercepted: the response EventStream is consumed and AgentCore trace chunks
(``orchestrationTrace``, ``preProcessingTrace``, ``postProcessingTrace``) are
written to ``bedrock-traces.jsonl`` inside the capsule alongside the standard
capsule artifacts.

``boto3`` is an **optional** dependency.  :func:`wrap_client` raises
:class:`ImportError` at call time if not installed.
"""
from __future__ import annotations

import json
import platform
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture.env import capture_environment
from novafabric.capture.secrets import SecretScannerV0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _extract_trace_events(event_stream: Any) -> Iterator[dict[str, Any]]:
    """Yield raw event dicts from a bedrock-agent-runtime EventStream."""
    for event in event_stream:
        yield event


class _WrappedBedrockClient:
    """Thin wrapper around a boto3 bedrock-agent-runtime client."""

    def __init__(self, inner: Any, data_dir: Path) -> None:
        self._inner = inner
        self._data_dir = data_dir

    def invoke_agent(self, **kwargs: Any) -> dict[str, Any]:
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all, uninstall_all
        from novafabric.capture.replay import minimal_replay_policy

        run_id = new_ulid()
        span_id = new_span_id()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        writer = CapsuleWriter(run_id=run_id, base_dir=self._data_dir)
        writer.open()
        created_at = _now()
        t0 = time.monotonic()
        install_all(writer=writer, parent_span_id=span_id)

        try:
            response = self._inner.invoke_agent(**kwargs)
        except Exception:
            uninstall_all()
            raise

        cap_dir = writer.capsule_dir
        trace_log = cap_dir / "bedrock-traces.jsonl"

        def _streaming_completion(raw_stream: Any) -> Iterator[dict[str, Any]]:
            for event in _extract_trace_events(raw_stream):
                # Capture trace chunks to bedrock-traces.jsonl
                _trace_keys = ("orchestrationTrace", "preProcessingTrace", "postProcessingTrace")
                for trace_key in _trace_keys:
                    if "trace" in event and trace_key in event["trace"]:
                        with trace_log.open("a") as f:
                            f.write(json.dumps({trace_key: event["trace"][trace_key]}) + "\n")
                yield event
            # Finalize capsule when stream exhausted
            uninstall_all()
            finished_at = _now()
            duration_ms = int((time.monotonic() - t0) * 1000)

            writer.append_trace_span({
                "span_id": span_id,
                "parent_span_id": None,
                "name": f"novafabric.adapter.bedrock_agentcore.{kwargs.get('agentId', 'agent')}",
                "started_at": created_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "status": "ok",
                "attributes": {
                    "framework": "bedrock-agentcore",
                    "agent_id": kwargs.get("agentId", ""),
                    "agent_alias_id": kwargs.get("agentAliasId", ""),
                },
            })

            env_lock = capture_environment(created_at=created_at, run_id=run_id)
            writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))
            scanner = SecretScannerV0(capsule_dir=cap_dir, run_id=run_id)
            proof = scanner.scan_and_redact()
            writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))
            writer.write_text("replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True))

            manifest: dict[str, Any] = {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "created_at": created_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "status": "success",
                "command": [f"@bedrock-agentcore:{kwargs.get('agentId', 'agent')}"],
                "capture_mode": "adapter-bedrock-agentcore",
                "novafabric_version": _pkg_version("novafabric"),
                "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
                "host": {
                    "os": platform.system().lower(),
                    "arch": "x86_64",
                    "python": platform.python_version(),
                    "cpu_count": 1,
                    "memory_bytes": 0,
                    "gpu": [],
                    "hostname_redacted": True,
                },
                "environment_ref": "env.lock",
                "replay_policy_ref": "replay.yaml",
                "redaction_proof_ref": "redaction-proof.json",
                "trace_ref": "trace.jsonl",
                "trace_root_span_id": span_id,
                "model_calls_ref": "model-calls.jsonl",
                "tool_calls_ref": "tool-calls.jsonl",
                "assets_ref": "assets.jsonl",
                "inputs": [{"type": "text", "value": kwargs.get("inputText", "")}],
                "outputs": [],
                "model_call_count": _count_jsonl(cap_dir / "model-calls.jsonl"),
                "tool_call_count": _count_jsonl(cap_dir / "tool-calls.jsonl"),
                "mutating_tool_count": 0,
                "exit_code": 0,
                "tags": {
                    "framework": "bedrock-agentcore",
                    "agent_id": kwargs.get("agentId", ""),
                },
                "bedrock_traces_ref": "bedrock-traces.jsonl",
            }
            writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))

        raw_stream = response.get("completion", iter([]))
        return {**response, "completion": _streaming_completion(raw_stream)}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_client(client: Any, data_dir: Path | None = None) -> _WrappedBedrockClient:
    """Wrap a boto3 ``bedrock-agent-runtime`` client for NovaFabric capture.

    Args:
        client: A ``boto3.client('bedrock-agent-runtime')`` instance.
        data_dir: Base directory for capsules.

    Returns:
        Wrapped client with the same interface.

    Raises:
        ImportError: If ``boto3`` is not installed.

    Usage::

        import boto3
        from novafabric.adapters.bedrock_agentcore import wrap_client
        client = wrap_client(boto3.client("bedrock-agent-runtime", region_name="us-east-1"))
        response = client.invoke_agent(agentId="...", agentAliasId="...",
                                       sessionId="...", inputText="hello")
        for event in response["completion"]:
            if "chunk" in event:
                print(event["chunk"]["bytes"].decode())
    """
    try:
        import boto3  # noqa: F401
    except ImportError:
        raise ImportError(
            "boto3 is not installed. "
            "Install it with: pip install 'novafabric[bedrock-agentcore]'"
        )

    import os
    resolved = data_dir or Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"
    return _WrappedBedrockClient(client, resolved)
