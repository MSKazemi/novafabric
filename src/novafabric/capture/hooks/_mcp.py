"""MCP (Model Context Protocol) capture hook.

Wraps `mcp.client.session.ClientSession.call_tool` to record every tool
invocation as a `tool-calls.jsonl` entry with `transport=mcp` and a synthesized
JSON-RPC envelope. Mirrors the OpenAI/Anthropic hook pattern (graceful no-op
if the `mcp` SDK is not installed).

Design (per ADR-0015): client-wrapper is the primary capture position. Envelope
fields are built from the boundary inputs (`name`, `arguments`); fully-verbatim
wire capture is the proxy path's job and lives outside this hook.
"""
from __future__ import annotations

import functools
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _serialize_response(response: Any) -> dict[str, Any]:
    """Best-effort JSON-serializable form of an MCP CallToolResult."""
    out: dict[str, Any] = {}
    content_items = getattr(response, "content", None)
    if isinstance(content_items, list):
        out["content"] = []
        for item in content_items:
            entry: dict[str, Any] = {"type": getattr(item, "type", "text")}
            text = getattr(item, "text", None)
            if text is not None:
                entry["text"] = text
            data = getattr(item, "data", None)
            if data is not None:
                entry["data"] = data
            out["content"].append(entry)
    is_error = getattr(response, "isError", None)
    if is_error is not None:
        out["isError"] = bool(is_error)
    return out


class MCPHook:
    def __init__(self, writer: "CapsuleWriter", parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._original_call_tool: Any = None
        self._original_initialize: Any = None
        self._patched_class: Any = None

    def install(self) -> None:
        try:
            import mcp.client.session as _mod
            target = getattr(_mod, "ClientSession", None)
            if target is None:
                return
            self._patched_class = target
            self._original_call_tool = target.call_tool
            # initialize() may not exist on stub/fake classes — guard with getattr.
            self._original_initialize = getattr(target, "initialize", None)
            hook_self = self
            original_call_tool = self._original_call_tool
            original_initialize = self._original_initialize

            if original_initialize is not None:
                @functools.wraps(original_initialize)
                async def patched_initialize(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                    result = await original_initialize(inner_self, *args, **kwargs)
                    try:
                        server_info = getattr(result, "serverInfo", None)
                        if server_info is not None:
                            name = getattr(server_info, "name", None)
                            version = getattr(server_info, "version", None)
                            if isinstance(name, str) and name:
                                inner_self._nova_server_name = name
                            if isinstance(version, str) and version:
                                inner_self._nova_server_version = version
                    except Exception:  # noqa: BLE001
                        pass
                    return result

                target.initialize = patched_initialize

            @functools.wraps(original_call_tool)
            async def patched_call_tool(
                inner_self: Any,
                name: str,
                arguments: dict[str, Any] | None = None,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                started = _now()
                t0 = time.monotonic()
                request_id = new_ulid()
                try:
                    response = await original_call_tool(
                        inner_self, name, arguments, *args, **kwargs
                    )
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    hook_self._record_success(
                        inner_self=inner_self,
                        name=name,
                        arguments=arguments,
                        response=response,
                        request_id=request_id,
                        started=started,
                        finished=_now(),
                        duration_ms=duration_ms,
                    )
                    return response
                except Exception as exc:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    hook_self._record_error(
                        inner_self=inner_self,
                        name=name,
                        arguments=arguments,
                        exc=exc,
                        request_id=request_id,
                        started=started,
                        finished=_now(),
                        duration_ms=duration_ms,
                    )
                    raise

            target.call_tool = patched_call_tool
        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        if self._patched_class is None:
            self._original_call_tool = None
            self._original_initialize = None
            return
        try:
            if self._original_call_tool is not None:
                self._patched_class.call_tool = self._original_call_tool
            if self._original_initialize is not None:
                self._patched_class.initialize = self._original_initialize
        finally:
            self._patched_class = None
            self._original_call_tool = None
            self._original_initialize = None

    def _build_envelope(
        self, name: str, arguments: dict[str, Any] | None, request_id: str
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }

    def _build_response_envelope(
        self, request_id: str, response: Any
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": _serialize_response(response),
        }

    @staticmethod
    def _server_attr(inner_self: Any, attr: str, fallback: str) -> str:
        # Prefer attributes stored by patched initialize() (e.g. _nova_server_name).
        nova_val = getattr(inner_self, f"_nova_{attr}", None)
        if isinstance(nova_val, str) and nova_val:
            return nova_val
        value = getattr(inner_self, attr, None)
        if isinstance(value, str) and value:
            return value
        return fallback

    def _base_record(
        self,
        *,
        inner_self: Any,
        name: str,
        arguments: dict[str, Any] | None,
        started: str,
        finished: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "tool_call_id": new_ulid(),
            "parent_span_id": self._parent_span_id,
            "started_at": started,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "tool_name": name,
            "tool_version": "unknown",
            "tool_provider": f"mcp://{self._server_attr(inner_self, 'server_name', 'unknown')}",
            "transport": "mcp",
            "mutates": False,
            "mutation_class": "unknown",
            "arguments": arguments or {},
            "arguments_schema_ref": None,
            "result": None,
            "result_schema_ref": None,
            "status": "success",
            "agent_call_id": None,
        }

    def _record_success(
        self,
        *,
        inner_self: Any,
        name: str,
        arguments: dict[str, Any] | None,
        response: Any,
        request_id: str,
        started: str,
        finished: str,
        duration_ms: int,
    ) -> None:
        record = self._base_record(
            inner_self=inner_self,
            name=name,
            arguments=arguments,
            started=started,
            finished=finished,
            duration_ms=duration_ms,
        )
        result_obj = _serialize_response(response)
        record["result"] = result_obj if result_obj else None
        record["mcp"] = {
            "server_name": self._server_attr(inner_self, "server_name", "unknown"),
            "server_version": self._server_attr(
                inner_self, "server_version", "unknown"
            ),
            "method": "tools/call",
            "request_id": request_id,
            "envelope": self._build_envelope(name, arguments, request_id),
            "response_envelope": self._build_response_envelope(request_id, response),
        }
        self._writer.append_tool_call(record)

    def _record_error(
        self,
        *,
        inner_self: Any,
        name: str,
        arguments: dict[str, Any] | None,
        exc: BaseException,
        request_id: str,
        started: str,
        finished: str,
        duration_ms: int,
    ) -> None:
        record = self._base_record(
            inner_self=inner_self,
            name=name,
            arguments=arguments,
            started=started,
            finished=finished,
            duration_ms=duration_ms,
        )
        record["status"] = "error"
        record["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback_ref": None,
        }
        record["mcp"] = {
            "server_name": self._server_attr(inner_self, "server_name", "unknown"),
            "server_version": self._server_attr(
                inner_self, "server_version", "unknown"
            ),
            "method": "tools/call",
            "request_id": request_id,
            "envelope": self._build_envelope(name, arguments, request_id),
            "response_envelope": None,
        }
        self._writer.append_tool_call(record)
