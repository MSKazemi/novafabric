"""Named failures of a mocked replay (ADR-0300).

Raised *inside* the replayed process by the replay dispatchers when the run leaves
the recorded path. Under the default ``fail`` divergence policy they stop the call
that diverged instead of letting it reach the network or a live tool; the engine
then marks the replay ``failure`` from the dispatcher's event log, so a workload
that catches the exception still cannot turn the replay into a success.
"""

from __future__ import annotations

from typing import Any


class ReplayDivergenceError(Exception):
    """The replayed run did something the capsule has no recorded answer for."""

    #: Stable machine-readable divergence kind (also written to the event log).
    kind = "divergence"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": str(self), **self.details}


class ReplayQueueExhaustedError(ReplayDivergenceError):
    """A model call was made after every recorded response for it was served.

    ``details`` carries ``provider``, ``call_index`` (0-based, per provider) and
    ``recorded_queue_length``.
    """

    kind = "model_queue_exhausted"


class ReplayProviderMismatchError(ReplayQueueExhaustedError):
    """A call to a provider with no recorded response left while another provider
    still had unconsumed recorded responses."""

    kind = "provider_mismatch"


class ReplayOrderMismatchError(ReplayDivergenceError):
    """Calls reached the providers in a different order than they were recorded."""

    kind = "order_mismatch"


class ReplayUnsupportedSurfaceError(ReplayDivergenceError):
    """A model API surface mocked replay does not serve (async, streaming,
    Responses API, ...). Strict replay refuses it rather than let it go live."""

    kind = "unsupported_surface"


class ReplayRecordMalformedError(ReplayDivergenceError):
    """A recorded response cannot be rebuilt faithfully (e.g. a tool-call entry
    without a ``name``)."""

    kind = "malformed_recorded_response"


class ReplayWireRequestMismatchError(ReplayDivergenceError):
    """A request to a registry model host differs from the generic-wire record at the
    head of its queue (ADR-0324 D3): another method, ``gen_ai.system``, path, or
    canonical query/body. Strict replay refuses it; nothing reaches the network."""

    kind = "wire_request_mismatch"


class ReplayToolUnmatchedError(ReplayDivergenceError):
    """A tool call on an intercepted surface had no unconsumed recorded result.

    Strict replay refuses it: the live tool is never executed.
    """

    kind = "tool_call_unmatched"


class AdapterRunCountMismatchError(ReplayDivergenceError):
    """A re-run adapter process started more framework-adapter runs than the
    capsule recorded (ADR-0306 Q7): the extra run's calls were never recorded.

    Raised in the replayed process by
    :func:`novafabric.capture.process_invocation.begin_adapter_run` under the
    default fail-closed policy; the engine also reports it when the process
    made fewer runs than recorded.
    """

    kind = "adapter_run_count_mismatch"


class ReplayRecordedToolError(Exception):
    """The recorded tool call itself failed; replay re-raises that failure.

    Not a divergence: it is the faithful replay of a recorded error.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}" if error_type else message)
        self.error_type = error_type


class CapsuleNotReplayableError(Exception):
    """The capsule records no command a replay could re-run.

    Not a divergence: nothing was re-run. Raised *before* anything is spawned by
    :func:`novafabric.replay._replayability.require_reexecutable_command` for a
    capsule captured inside a framework call (``capture_mode: sdk-decorator``,
    whose ``command`` is a ``@framework:name`` label), imported from
    OpenTelemetry spans (``otel-import``), or with an empty ``command``. The
    engine records it as ``status: aborted`` with ``error.type:
    CapsuleNotReplayable``.
    """

    #: Stable ``error.type`` written to ``replay_result.yaml``.
    error_type = "CapsuleNotReplayable"

    def __init__(
        self,
        message: str,
        *,
        capture_mode: str | None,
        command: list[str],
        guard: str | None = None,
    ) -> None:
        super().__init__(message)
        self.capture_mode = capture_mode
        self.command = command
        #: ADR-0306 Q7: the process-invocation guard that refused an adapter
        #: capsule that records one; ``None`` when it records none at all.
        self.guard = guard

    def as_error(self) -> dict[str, str]:
        error = {"type": self.error_type, "message": str(self)}
        if self.guard is not None:
            error["guard"] = self.guard
        return error


class ModelSurfaceNotServableError(Exception):
    """A strict mocked replay refused to start: the capsule records model calls
    mocked replay cannot serve (ADR-0319) -- an unserved surface (Ollama, Hugging
    Face, other providers' raw HTTP) or a served surface whose response was never
    recorded. Run anyway, each would reach the network under a "mocked" label.

    Not a divergence: nothing was re-run. Raised *before* anything is spawned;
    the engine records it as ``status: aborted`` with ``error.type:
    ModelSurfaceNotServable``; ``nova replay`` exits 1 (also under
    ``--dry-run``). Under ``--permissive`` the replay starts instead and reports
    a ``model_surface_not_servable`` divergence (a warning) and the counters
    ``model_calls_not_servable`` / ``replay_contract.model_calls_not_servable_by_system``.
    """

    #: Stable ``error.type`` written to ``replay_result.yaml``.
    error_type = "ModelSurfaceNotServable"
    #: Divergence kind reported instead under ``--permissive``.
    kind = "model_surface_not_servable"

    def __init__(self, by_system: dict[str, dict[str, Any]]) -> None:
        self.by_system = {k: dict(v) for k, v in by_system.items()}
        super().__init__(self.describe(self.by_system))

    @property
    def count(self) -> int:
        return sum(int(v.get("count", 0)) for v in self.by_system.values())

    @staticmethod
    def describe(by_system: dict[str, dict[str, Any]]) -> str:
        from novafabric.replay._servability import describe_by_system

        total = sum(int(v.get("count", 0)) for v in by_system.values())
        return (
            f"the capsule records {total} model call{'s' if total != 1 else ''} "
            f"mocked replay cannot serve -- {describe_by_system(by_system)} -- "
            "(no recorded response on a surface replay serves); replayed, "
            f"{'they' if total != 1 else 'it'} would run live. A strict replay "
            "refuses to start; pass --permissive to run it anyway and report "
            f"{'them' if total != 1 else 'it'} (ADR-0319)"
        )

    def as_error(self) -> dict[str, Any]:
        return {
            "type": self.error_type,
            "message": str(self),
            "model_calls_not_servable_by_system": {
                k: dict(v) for k, v in self.by_system.items()
            },
        }


class ToolSurfaceNotServedError(Exception):
    """A strict mocked replay refused to start: the capsule records tool calls
    mocked replay does not serve by policy -- LangChain tools the workload did
    not pass through ``wrap_tools`` (ADR-0306 D1 serves declared tools only).
    Run anyway, each would re-execute live under a "mocked" label.

    The tool-side twin of :class:`ModelSurfaceNotServableError`: raised *before*
    anything is spawned, recorded as ``status: aborted`` with ``error.type:
    ToolSurfaceNotServed``; ``nova replay`` exits 1 (also under ``--dry-run``).
    Under ``--permissive`` the replay starts instead and reports a
    ``tool_surface_not_served`` divergence (a warning); each call it makes is
    counted in ``tool_calls_live``.
    """

    error_type = "ToolSurfaceNotServed"
    kind = "tool_surface_not_served"

    def __init__(self, by_surface: dict[str, dict[str, Any]]) -> None:
        self.by_surface = {k: dict(v) for k, v in by_surface.items()}
        super().__init__(self.describe(self.by_surface))

    @property
    def count(self) -> int:
        return sum(int(v.get("count", 0)) for v in self.by_surface.values())

    @staticmethod
    def describe(by_surface: dict[str, dict[str, Any]]) -> str:
        from novafabric.replay._contract import describe_not_served

        total = sum(int(v.get("count", 0)) for v in by_surface.values())
        many = total != 1
        return (
            f"the capsule records {total} tool call{'s' if many else ''} mocked replay "
            f"does not serve -- {describe_not_served(by_surface)} -- replayed, "
            f"{'they' if many else 'it'} would re-execute live. A strict replay refuses "
            "to start; pass the tools through "
            "novafabric.adapters.langchain_tools.wrap_tools and capture again to have "
            f"them served, or pass --permissive to run {'them' if many else 'it'} live "
            "and count each in tool_calls_live (ADR-0306 D1)"
        )

    def as_error(self) -> dict[str, Any]:
        return {
            "type": self.error_type,
            "message": str(self),
            "tool_calls_not_served_by_surface": {
                k: dict(v) for k, v in self.by_surface.items()
            },
        }


class ReplayRecordedErrorUnreconstructableError(ReplayDivergenceError):
    """A recorded model call failed, and mocked replay cannot raise that failure
    faithfully: the exception class is not on the per-SDK allow-list, its HTTP
    status or body was not recorded, or the capsule predates recorded error
    details. Strict replay refuses the call rather than raise a different error
    or hand it another call's response.

    ``details`` carries ``provider``, ``call_index``, ``model_call_id``,
    ``error_type`` and ``reason``.
    """

    kind = "recorded_error_unreconstructable"


class ReplayRecordedModelError(Exception):
    """Permissive stand-in for a recorded model error replay could not rebuild.

    Raised only under ``--permissive`` after the divergence is recorded: the
    recorded call failed, so the replayed call fails too, with the recorded type
    and message -- but not as the SDK's own exception class.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}" if error_type else message)
        self.error_type = error_type


class ReplayToolRecordNotServableError(ReplayDivergenceError):
    """A ``record.tool`` call matched a recorded call whose result cannot be
    served faithfully (ADR-0306 D7): payloads were not recorded, the result was
    not JSON-native or over the size cap, an argument was not JSON-representable,
    or the boundary wrote nested model/tool records. Strict replay refuses the
    call before the function body runs.

    ``details`` carries ``tool_name``, ``arguments_hash`` (never the values),
    ``surface``, ``reason`` and ``consumed`` (whether a record was consumed).
    Counted as a *tool* divergence (``tool_calls_unmatched``), never as a model
    one.
    """

    kind = "tool_result_not_servable"


class ReplayOverrideUnenforceableError(Exception):
    """A strict mocked replay refused to start: a ``replay.yaml`` ``allow: false``
    override names a tool the capsule recorded on a transport replay cannot
    intercept (not MCP ``tools/call``, not ``record.tool``), so replay could not
    stop it running live (ADR-0306 D8, owner decision Q5 2026-10-09).

    Not a divergence: nothing was re-run. The engine records it as ``status:
    aborted`` with ``error.type: ToolOverrideUnenforceable``; ``nova replay``
    exits :attr:`exit_code`. Under ``--permissive`` the replay starts instead
    and reports an ``override_unenforceable`` divergence (a warning).
    """

    #: Stable ``error.type`` written to ``replay_result.yaml``.
    error_type = "ToolOverrideUnenforceable"
    #: ``nova replay`` exit status for this refusal (also under ``--dry-run``).
    exit_code = 3
    #: Divergence kind reported instead under ``--permissive``.
    kind = "override_unenforceable"

    def __init__(self, unenforceable: list[dict[str, Any]]) -> None:
        self.unenforceable = list(unenforceable)
        super().__init__(self.describe(self.unenforceable))

    @staticmethod
    def describe(unenforceable: list[dict[str, Any]]) -> str:
        detail = "; ".join(
            f"{u['tool_name']!r} (transport {', '.join(u['transports'])})"
            for u in unenforceable
        )
        return (
            "replay.yaml tool_overrides `allow: false` cannot be enforced for "
            f"{detail}: replay does not intercept that transport, so the tool would "
            "run live. A strict replay refuses to start; pass --permissive to run "
            "it anyway and report this"
        )

    def as_error(self) -> dict[str, str]:
        return {"type": self.error_type, "message": str(self)}
