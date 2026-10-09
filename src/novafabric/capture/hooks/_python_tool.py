"""Capture sink for ``novafabric.capture.record.tool`` (ADR-0306 slice 1, experimental).

Installed by ``install_all`` like ``MCPHook``, keyed on the always-importable
``novafabric.capture.record`` module. While installed, every call of a function
decorated with ``record.tool`` runs its body and then writes one
``tool-calls.jsonl`` record with ``transport: "python"`` (in-force schema, extra
information under ``io.novafabric.*`` extension keys only).

Payloads follow the façade's default-path rule (``record._payloads_enabled``;
ADR-0306 open question 2, chosen 2026-10-09): arguments and the result are kept
only at the ``forensic``/``air_gapped`` capture level. Otherwise the record keeps
the tool name, an argument digest (so replay can still match the call) and a
result digest, and is marked not servable with a re-capture hint.

Recording never raises into the workload; exceptions from the body propagate
unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture import _tool_codec as codec
from novafabric.capture import _tool_scope
from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import get_current_writer

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.record import ToolSpec

#: Why a record is not servable when payload capture was off.
PAYLOADS_OFF_REASON = (
    "arguments and result were not recorded: payload capture was off at capture "
    "time (capture level below forensic) -- re-capture with payload capture enabled "
    "(NOVA_CAPTURE_LEVEL=forensic) to make this tool servable"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Pending:
    """One decorated call between its start and its record."""

    __slots__ = ("spec", "call", "tool_call_id", "started", "t0", "boundary", "token")

    def __init__(self, spec: ToolSpec, call: codec.CanonicalCall) -> None:
        self.spec = spec
        self.call = call
        self.tool_call_id = new_ulid()
        self.started = _now()
        self.t0 = time.monotonic()
        self.boundary, self.token = _tool_scope.enter(self.tool_call_id)

    def close(self) -> int:
        _tool_scope.leave(self.token)
        return self.boundary.nested


class PythonToolHook:
    """Registers the capture sink for ``record.tool`` while installed."""

    def __init__(self, writer: CapsuleWriter, parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._previous: Any = None
        self._installed = False

    def install(self) -> None:
        from novafabric.capture import record

        self._previous = record._set_tool_handler(self)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        from novafabric.capture import record

        if record._get_tool_handler() is self:
            record._set_tool_handler(self._previous)
        self._previous = None
        self._installed = False

    # ── handler protocol ────────────────────────────────────────────────────

    def call(
        self, spec: ToolSpec, fn: Callable[..., Any],
        args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> Any:
        pending = self._start(spec, args, kwargs)
        if pending is None:
            return fn(*args, **kwargs)
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:
            self._finish(pending, pending.close(), error=exc)
            raise
        except BaseException:
            pending.close()
            raise
        self._finish(pending, pending.close(), value=value)
        return value

    async def call_async(
        self, spec: ToolSpec, fn: Callable[..., Any],
        args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> Any:
        pending = self._start(spec, args, kwargs)
        if pending is None:
            return await fn(*args, **kwargs)
        try:
            value = await fn(*args, **kwargs)
        except Exception as exc:
            self._finish(pending, pending.close(), error=exc)
            raise
        except BaseException:
            pending.close()
            raise
        self._finish(pending, pending.close(), value=value)
        return value

    # ── recording ───────────────────────────────────────────────────────────

    @staticmethod
    def _start(
        spec: ToolSpec, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> _Pending | None:
        """``None`` when the call does not bind: the function raises the same
        ``TypeError`` before its body runs, and nothing is recorded."""
        try:
            call = codec.canonical_call(spec.signature, spec.ignore, args, kwargs)
        except TypeError:
            return None
        except Exception:  # noqa: BLE001 -- capture must never block the workload
            call = codec.CanonicalCall(arguments=None, not_canonical="arguments could not be bound")
        try:
            return _Pending(spec, call)
        except Exception:  # noqa: BLE001
            return None

    def _finish(
        self, pending: _Pending, nested: int, *,
        value: Any = None, error: BaseException | None = None,
    ) -> None:
        try:
            record = self.build_record(pending, nested, value=value, error=error)
            get_current_writer(self._writer).append_tool_call(record)
        except Exception:  # noqa: BLE001 -- recording never raises into the workload
            pass

    def build_record(
        self, pending: _Pending, nested: int, *,
        value: Any = None, error: BaseException | None = None,
    ) -> dict[str, Any]:
        from novafabric.capture.record import _payloads_enabled

        spec = pending.spec
        payloads = _payloads_enabled()
        ext: dict[str, Any] = {
            codec.TOOL_SURFACE_EXT: codec.TOOL_SURFACE_PYTHON_FUNCTION,
            codec.QUALNAME_EXT: f"{spec.module}:{spec.qualname}",
        }
        if spec.ignore:
            ext[codec.IGNORED_ARGUMENTS_EXT] = sorted(spec.ignore)
        if nested:
            ext[codec.NESTED_RECORDS_EXT] = nested

        reasons: list[str] = []
        call = pending.call
        arguments: dict[str, Any] = {}
        if call.not_canonical is not None:
            reasons.append(call.not_canonical)
        elif payloads:
            arguments = call.arguments or {}
        else:
            ext[codec.ARGUMENTS_DIGEST_EXT] = call.digest
        if not payloads:
            reasons.append(PAYLOADS_OFF_REASON)
        # Nesting is not a reason (ADR-0306 slice 4): replay serves the boundary
        # and consumes the records marked `within_tool_call_id` as covered.

        finished = _now()
        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "tool_call_id": pending.tool_call_id,
            "parent_span_id": self._parent_span_id,
            "started_at": pending.started,
            "finished_at": finished,
            "duration_ms": max(0, int((time.monotonic() - pending.t0) * 1000)),
            "tool_name": spec.name,
            "tool_version": spec.version,
            "tool_provider": f"python://{spec.module}",
            "transport": "python",
            "mutates": spec.mutation_class not in ("none", "read-only"),
            "mutation_class": spec.mutation_class,
            "arguments": arguments,
            "arguments_schema_ref": None,
            "result": None,
            "result_schema_ref": None,
            "status": "success",
            "agent_call_id": None,
        }
        if error is not None:
            record["status"] = "error"
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error) if payloads else "(not recorded at this capture level)",
                "traceback_ref": None,
            }
            if type(error).__module__ == "builtins":
                ext[codec.EXCEPTION_BUILTIN_EXT] = True
        else:
            encoded = codec.encode_result(value, keep_value=payloads)
            if encoded.reason:
                reasons.append(encoded.reason)
            if encoded.digest:
                ext[codec.RESULT_DIGEST_EXT] = encoded.digest
            # Kept whenever capture may keep it -- even on a record that is not
            # servable for another reason -- so the value is evidence either way.
            record["result"] = encoded.result
        if reasons:
            ext[codec.RESULT_CODEC_EXT] = codec.CODEC_NOT_SERVABLE
            ext[codec.NOT_SERVABLE_REASON_EXT] = "; ".join(reasons)
        else:
            ext[codec.RESULT_CODEC_EXT] = codec.CODEC_JSON
        record["extensions"] = ext
        return record
