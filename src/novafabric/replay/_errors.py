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


class ReplayToolUnmatchedError(ReplayDivergenceError):
    """A tool call on an intercepted surface had no unconsumed recorded result.

    Strict replay refuses it: the live tool is never executed.
    """

    kind = "tool_call_unmatched"


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
        self, message: str, *, capture_mode: str | None, command: list[str]
    ) -> None:
        super().__init__(message)
        self.capture_mode = capture_mode
        self.command = command

    def as_error(self) -> dict[str, str]:
        return {"type": self.error_type, "message": str(self)}


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
