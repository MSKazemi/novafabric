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
