"""Does a capsule record a command that ``nova replay`` can run again?

The one place that answers it. ``mocked`` and ``intervention`` re-run
``capsule.yaml:command``; a capsule can only be re-run if that command is a real
argv. Three kinds of capsule record none:

* ``capture_mode: sdk-decorator`` — written by a framework adapter
  (``novafabric.adapters``) or the ``novafabric.sdk.agent`` decorator from *inside*
  a framework call. Its ``command`` is a label such as ``@langgraph:demo``, not
  a program; spawning it fails with ``No such file or directory``.
* ``capture_mode: otel-import`` — built from OpenTelemetry spans; ``command`` is
  empty by design.
* any capsule whose ``command`` is empty or whose first element is a
  ``@``-prefixed label.

Making such a capsule replayable needs its own design (ADR-0306, open question
7). Until then the replay is refused up front, truthfully, instead of failing
as a launch error.
"""

from __future__ import annotations

from typing import Any

from novafabric.replay._errors import CapsuleNotReplayableError

#: ``capture_mode`` values whose capsules never record a re-runnable argv.
NON_REEXECUTABLE_CAPTURE_MODES: dict[str, str] = {
    "sdk-decorator": "was captured inside a framework call, by a framework adapter "
    "or the novafabric.sdk.agent decorator",
    "otel-import": "was imported from OpenTelemetry spans",
}

#: A ``command`` whose first element starts with this is a label, not a program.
PLACEHOLDER_COMMAND_PREFIX = "@"

#: Modes that never spawn the command, so they work on every capsule.
#: ``exact`` is a read-only eligibility report; on such a capsule it reports
#: ``exact_eligible: false`` with the reason below.
MODES_WITHOUT_REEXECUTION: tuple[str, ...] = ("forensic", "semantic", "exact")

#: Modes that re-run ``capsule.yaml:command``.
MODES_THAT_REEXECUTE: tuple[str, ...] = ("mocked", "intervention")


def _command(manifest: dict[str, Any]) -> list[str]:
    raw = manifest.get("command") or []
    if isinstance(raw, str):
        return [raw]
    return [str(part) for part in raw]


def not_reexecutable_reason(manifest: dict[str, Any]) -> str | None:
    """Why *manifest* records no command a replay can re-run, or ``None``."""
    capture_mode = manifest.get("capture_mode")
    command = _command(manifest)
    label = f" ({command[0]!r} is a label, not a program)" if command else ""
    if isinstance(capture_mode, str) and capture_mode in NON_REEXECUTABLE_CAPTURE_MODES:
        how = NON_REEXECUTABLE_CAPTURE_MODES[capture_mode]
        return (
            f"this capsule {how} (capture_mode: {capture_mode}) and records no "
            f"command to re-run{label}"
        )
    if not command:
        return "this capsule records no command to re-run (capsule.yaml command is empty)"
    if command[0].startswith(PLACEHOLDER_COMMAND_PREFIX):
        return f"this capsule records no command to re-run{label}"
    return None


def refusal_message(manifest: dict[str, Any], mode: str) -> str | None:
    """The full message for refusing *mode* on *manifest*, or ``None``."""
    reason = not_reexecutable_reason(manifest)
    if reason is None:
        return None
    return (
        f"--mode {mode} cannot replay it: {reason}. Modes that work on it: "
        "forensic (inspect the capsule) and semantic (score the recorded "
        "responses); --mode exact reports it not eligible. Re-running such a "
        "capsule is not supported (ADR-0306, open question 7)."
    )


def require_reexecutable_command(manifest: dict[str, Any], mode: str) -> list[str]:
    """Return the command to re-run, or raise :class:`CapsuleNotReplayableError`."""
    message = refusal_message(manifest, mode)
    if message is not None:
        capture_mode = manifest.get("capture_mode")
        raise CapsuleNotReplayableError(
            message,
            capture_mode=capture_mode if isinstance(capture_mode, str) else None,
            command=_command(manifest),
        )
    return _command(manifest)
