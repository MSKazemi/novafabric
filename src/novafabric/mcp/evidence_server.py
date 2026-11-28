"""Read-only MCP evidence server over stdio (issue #244). **Experimental.**

Run it as::

    python -m novafabric.mcp.evidence_server --scope ./runs [--scope DIR ...]

An MCP client (an IDE assistant, an agent host) starts this process and talks
newline-delimited JSON-RPC 2.0 over its stdin/stdout, the MCP stdio transport.
It offers three tools, all implemented in :mod:`novafabric.mcp.evidence_tools`:

* ``list_capsules`` — the capsules directly under the scope directories, paged;
* ``get_capsule_summary`` — allowlisted manifest fields of one capsule;
* ``verify_capsule`` — offline seal verification of one capsule, from the
  capsule alone.

This is **not** ``nova mcp-proxy`` (a recording proxy between a client and
another MCP server) and it is not advertised by the SEP-1649 Server Card that
``nova serve`` publishes (that card describes the HTTP surface). It is a
separate, local, read-only process: no network listener, no subprocess, no
writes, no prompt or response content. Logs go to stderr only; stdout carries
protocol messages and nothing else.

Protocol scope: ``initialize``, ``ping``, ``tools/list`` and ``tools/call``.
Protocol revisions 2025-11-25, 2025-06-18 and 2025-03-26 are accepted; any other
requested revision is answered with 2025-11-25, which is how MCP version
negotiation works (the client decides whether to continue). JSON-RPC batches are
refused, as MCP 2025-06-18 removed them. Messages larger than
:data:`MAX_MESSAGE_BYTES` and tool results larger than
:data:`MAX_RESPONSE_BYTES` are refused, never truncated silently. An unexpected
failure is logged by exception type and code location only, never by its
message, which could quote capsule content (:func:`_log_failure`).

The protocol, privacy, adversarial and offline contract is pinned by
``tests/mcp_evidence_server/test_conformance_gate.py`` (issue #245).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from typing import Any, BinaryIO, Final

from novafabric.mcp.evidence_tools import (
    VERIFICATION_LEVEL,
    EvidenceScope,
    EvidenceToolError,
    ScopeError,
)

__all__ = [
    "MAX_MESSAGE_BYTES",
    "MAX_RESPONSE_BYTES",
    "SERVER_NAME",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOLS",
    "EvidenceServer",
    "main",
    "serve",
]

_log = logging.getLogger(__name__)

SERVER_NAME: Final[str] = "novafabric-evidence"

#: Accepted MCP revisions, newest first. The first is offered when the client
#: asks for one not listed.
SUPPORTED_PROTOCOL_VERSIONS: Final[tuple[str, ...]] = ("2025-11-25", "2025-06-18", "2025-03-26")

#: ``structuredContent`` exists from this revision on.
_STRUCTURED_FROM: Final[str] = "2025-06-18"

#: Largest accepted inbound message (one line), in bytes.
MAX_MESSAGE_BYTES: Final[int] = 1024 * 1024
#: Largest tool result returned, in bytes of its JSON text.
MAX_RESPONSE_BYTES: Final[int] = 512 * 1024

PARSE_ERROR: Final[int] = -32700
INVALID_REQUEST: Final[int] = -32600
METHOD_NOT_FOUND: Final[int] = -32601
INVALID_PARAMS: Final[int] = -32602
INTERNAL_ERROR: Final[int] = -32603
#: Server-defined: a request other than ``initialize``/``ping`` before ``initialize``.
SERVER_NOT_INITIALIZED: Final[int] = -32002

RESPONSE_TOO_LARGE: Final[str] = "response_too_large"
TOOL_INTERNAL_ERROR: Final[str] = "internal_error"

_READ_ONLY: Final[dict[str, bool]] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_CAPSULE_ARGS: Final[dict[str, Any]] = {
    "capsule": {
        "type": "string",
        "description": "Capsule directory name, as list_capsules returns it (one path segment).",
        "minLength": 1,
        "maxLength": 255,
    },
    "root": {
        "type": "integer",
        "minimum": 0,
        "description": "Scope index; needed only when two scopes hold the same capsule name.",
    },
}

#: The ``tools/list`` result. Every tool is read-only and closed-world.
TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "list_capsules",
        "title": "List capsules in scope",
        "description": (
            "List the Run Capsule directories directly under the server's scope "
            "directories, one page at a time. Returns scope index, capsule name and "
            "whether a seal is present (not whether it verifies)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": _CAPSULE_ARGS["root"],
                "cursor": {"type": "string", "description": "next_cursor from a previous page."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_capsule_summary",
        "title": "Summarize one capsule",
        "description": (
            "Return allowlisted manifest fields of one capsule (run id, status, times, "
            "call counts, usage totals) and the names of evidence files. Never returns "
            "prompts, responses, tool arguments, the command line, host or paths; "
            "withheld fields are named. Does not check the seal: use verify_capsule."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _CAPSULE_ARGS,
            "required": ["capsule"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "verify_capsule",
        "title": "Verify one capsule offline",
        "description": (
            "Verify one capsule's NovaSeal seal. Scope of the check: "
            + VERIFICATION_LEVEL
            + ". valid is true only when every check passed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _CAPSULE_ARGS,
            "required": ["capsule"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
]

_TOOL_ARGS: Final[dict[str, frozenset[str]]] = {
    "list_capsules": frozenset({"root", "cursor", "limit"}),
    "get_capsule_summary": frozenset({"capsule", "root"}),
    "verify_capsule": frozenset({"capsule", "root"}),
}

_INSTRUCTIONS: Final[str] = (
    "Read-only access to NovaFabric Run Capsules in the directories the operator "
    "allowed. 'root' is the index of a scope directory in the order the operator "
    "gave them. No tool returns prompt or response content. A summary is not a "
    "verification; only verify_capsule with valid=true says the seal checked out."
)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(req_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _valid_id(value: object) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


class EvidenceServer:
    """The MCP request handler for one stdio session. No I/O of its own."""

    def __init__(self, scope: EvidenceScope) -> None:
        self._scope = scope
        self._protocol_version: str | None = None

    @property
    def protocol_version(self) -> str | None:
        """The negotiated revision, or None before ``initialize``."""
        return self._protocol_version

    def handle_line(self, raw: bytes) -> dict[str, Any] | None:
        """Handle one inbound line; the response to write, or None for none."""
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError):
            return _error(None, PARSE_ERROR, "parse error: not a JSON value")
        except RecursionError:
            return _error(None, PARSE_ERROR, "parse error: JSON nested too deeply")
        return self.handle_message(message)

    def handle_message(self, message: object) -> dict[str, Any] | None:
        """Handle one parsed JSON-RPC message."""
        if isinstance(message, list):
            return _error(None, INVALID_REQUEST, "JSON-RPC batches are not supported")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if not isinstance(method, str):
            if "id" in message and ("result" in message or "error" in message):
                return None  # a response; this server sends no requests, so ignore it
            return _error(None, INVALID_REQUEST, "missing method")
        if "id" not in message:
            return None  # a notification (initialized, cancelled, ...): nothing to answer
        req_id = message["id"]
        if not _valid_id(req_id):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(req_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except _RpcError as exc:
            return _error(req_id, exc.code, exc.message)
        except Exception as exc:  # never let one request end the session
            _log_failure("mcp_evidence_request_failed", exc)
            return _error(req_id, INTERNAL_ERROR, f"internal error ({type(exc).__name__})")
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if self._protocol_version is None:
            raise _RpcError(SERVER_NOT_INITIALIZED, "server not initialized: send initialize first")
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call(params)
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method[:64]}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        from novafabric import __version__  # noqa: PLC0415

        requested = params.get("protocolVersion")
        negotiated = SUPPORTED_PROTOCOL_VERSIONS[0]
        if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
            negotiated = requested
        self._protocol_version = negotiated
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": _INSTRUCTIONS,
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in _TOOL_ARGS:
            raise _RpcError(INVALID_PARAMS, "unknown tool")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        unknown = sorted(str(key)[:64] for key in arguments if key not in _TOOL_ARGS[name])
        if unknown:
            return self._tool_error(
                "invalid_argument", f"unknown argument(s) for {name}: {unknown[:10]}"
            )
        try:
            if name == "list_capsules":
                payload = self._scope.list_capsules(
                    arguments.get("root"), arguments.get("cursor"), arguments.get("limit")
                )
            elif name == "get_capsule_summary":
                payload = self._scope.get_capsule_summary(
                    arguments.get("capsule"), arguments.get("root")
                )
            else:
                payload = self._scope.verify_capsule(
                    arguments.get("capsule"), arguments.get("root")
                )
        except EvidenceToolError as exc:
            return self._tool_error(exc.code, exc.message)
        except Exception as exc:  # fail closed: an error, never a partial "valid" result
            _log_failure("mcp_evidence_tool_failed", exc, tool=name)
            return self._tool_error(
                TOOL_INTERNAL_ERROR, f"the tool failed ({type(exc).__name__}); see server log"
            )
        return self._tool_result(payload, is_error=False)

    def _tool_error(self, code: str, message: str) -> dict[str, Any]:
        return self._tool_result({"error": {"code": code, "message": message}}, is_error=True)

    def _tool_result(self, payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
        # ensure_ascii: a lone surrogate (a non-UTF-8 file name, a YAML escape)
        # is written as a \u escape instead of failing to encode.
        text = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            payload = {
                "error": {
                    "code": RESPONSE_TOO_LARGE,
                    "message": f"the result exceeds {MAX_RESPONSE_BYTES} bytes; ask for a "
                    "smaller page",
                }
            }
            text = json.dumps(payload, sort_keys=True)
            is_error = True
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if (self._protocol_version or "") >= _STRUCTURED_FROM:
            result["structuredContent"] = payload
        return result


#: Innermost frames named when a failure is logged.
_LOGGED_FRAMES: Final[int] = 8


def _log_failure(event: str, exc: BaseException, *, tool: str | None = None) -> None:
    """Log an unexpected failure by type and location, never by message.

    An exception's text can quote what it failed on: a capsule's content, a
    secret, an absolute path. An MCP host commonly keeps a server's stderr in
    its own logs, so the server logs the exception type and the innermost
    frames as ``file.py:line:function`` (base names, so no install path
    either), which is enough to find the fault and carries no data.
    """
    frames = [
        f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)[-_LOGGED_FRAMES:]
    ]
    _log.error(
        "%s: %s%s at %s",
        event,
        type(exc).__name__,
        f" in tool {tool}" if tool else "",
        " > ".join(frames) or "?",
    )


def _drain_line(stream: BinaryIO) -> None:
    """Discard the rest of an over-long line, in bounded chunks."""
    while True:
        chunk = stream.readline(MAX_MESSAGE_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return


def serve(scope: EvidenceScope, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Answer newline-delimited JSON-RPC from *stdin* on *stdout* until EOF. Returns 0."""
    server = EvidenceServer(scope)
    while True:
        line = stdin.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_MESSAGE_BYTES and not line.endswith(b"\n"):
            _drain_line(stdin)
            response: dict[str, Any] | None = _error(
                None, INVALID_REQUEST, f"message larger than {MAX_MESSAGE_BYTES} bytes"
            )
        elif not line.strip():
            continue
        else:
            response = _handle_safely(server, line)
        if response is not None:
            stdout.write(_encode(response))
            stdout.flush()


def _handle_safely(server: EvidenceServer, line: bytes) -> dict[str, Any] | None:
    """``server.handle_line``, but no input line can end the session."""
    try:
        return server.handle_line(line)
    except Exception as exc:  # last resort; handle_message already catches per request
        _log.error("mcp_evidence_line_failed", extra={"error": type(exc).__name__})
        return _error(None, INTERNAL_ERROR, f"internal error ({type(exc).__name__})")


def _encode(response: dict[str, Any]) -> bytes:
    """One response line. ASCII-only JSON, so a lone surrogate echoed from the
    request (a ``method`` name, say) cannot raise while encoding."""
    try:
        data = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as exc:
        _log.error("mcp_evidence_encode_failed", extra={"error": type(exc).__name__})
        data = json.dumps(
            _error(None, INTERNAL_ERROR, "internal error (response not encodable)"),
            separators=(",", ":"),
        )
    return data.encode("ascii") + b"\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m novafabric.mcp.evidence_server",
        description=(
            "Read-only MCP evidence server over stdio (experimental). Lists, summarizes "
            "and verifies the Run Capsules directly under the --scope directories. "
            "Never writes, never runs a subprocess, never opens the network, never "
            "returns prompt or response content."
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        required=True,
        metavar="DIR",
        help=(
            "A directory whose direct child directories are capsules (for example "
            "~/.novafabric/capsules, where nova capture writes by default). Repeat for "
            "more than one (at most 16). Required: there is no default scope."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m novafabric.mcp.evidence_server``."""
    args = _parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        scope = EvidenceScope.from_paths(args.scope)
    except ScopeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    try:
        return serve(scope, sys.stdin.buffer, sys.stdout.buffer)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess test
    raise SystemExit(main())
