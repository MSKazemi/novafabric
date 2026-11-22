"""Does a capsule record a command that ``nova replay`` can run again?

The one place that answers it. ``mocked`` and ``intervention`` re-run a
capsule's program; a capsule can only be re-run if it records a real argv:

* ``capture_mode: cli-wrapper`` (``nova capture``) -- ``capsule.yaml:command``.
* ``capture_mode: sdk-decorator`` -- written by a framework adapter
  (``novafabric.adapters``) or the ``novafabric.sdk.agent`` decorator from *inside*
  a framework call. Its ``command`` is a label such as ``@langgraph:demo``, not
  a program. Since ADR-0306 Q7 (Option 1) the adapter also records how its
  process was started (the ``process_invocation`` facet,
  ``novafabric.capture.process_invocation``); such a capsule is re-run from that
  invocation, in the recorded working directory, **only when every guard in
  :data:`GUARDS` passes**. A capsule without the facet (every adapter capsule
  written before it) is refused exactly as before.
* ``capture_mode: otel-import`` -- built from OpenTelemetry spans; ``command`` is
  empty by design. Never re-run.
* any other capsule whose ``command`` is empty or whose first element is a
  ``@``-prefixed label. Never re-run.

A refusal happens up front, truthfully, instead of failing as a launch error.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Literal

from pydantic import ValidationError

from novafabric.replay._errors import CapsuleNotReplayableError

#: ``capture_mode`` values whose capsules never record a re-runnable argv in
#: ``command``.
NON_REEXECUTABLE_CAPTURE_MODES: dict[str, str] = {
    "sdk-decorator": "was captured inside a framework call, by a framework adapter "
    "or the novafabric.sdk.agent decorator",
    "otel-import": "was imported from OpenTelemetry spans",
}

#: The capture mode whose capsules may carry a re-runnable process invocation.
ADAPTER_CAPTURE_MODE = "sdk-decorator"

#: A ``command`` whose first element starts with this is a label, not a program.
PLACEHOLDER_COMMAND_PREFIX = "@"

#: Modes that never spawn the command, so they work on every capsule.
#: ``exact`` is a read-only eligibility report; on such a capsule it reports
#: ``exact_eligible: false`` with the reason below.
MODES_WITHOUT_REEXECUTION: tuple[str, ...] = ("forensic", "semantic", "exact")

#: Modes that re-run ``capsule.yaml:command``.
MODES_THAT_REEXECUTE: tuple[str, ...] = ("mocked", "intervention")

#: Entry points that are a long-lived or harness host: re-running the process
#: re-runs the host (a notebook kernel, a server, a test session), not the
#: agent. Matched against a ``script`` entry's file stem and a ``module``
#: entry's top-level name, case-insensitively. Owned here, extended by tests.
LONG_LIVED_HOSTS: frozenset[str] = frozenset({
    "ipykernel_launcher", "ipykernel", "jupyter", "jupyter-notebook", "jupyter-lab",
    "jupyter-execute", "ipython", "pytest", "py.test", "uvicorn", "gunicorn",
    "hypercorn", "streamlit", "celery",
})

#: Every guard an adapter capsule's process invocation must pass, in the order
#: they are checked. Each name is the stable ``error.guard`` of its refusal.
GUARDS: tuple[str, ...] = (
    "facet_malformed",
    "entry_not_reproducible",
    "long_lived_host",
    "argv_redacted",
    "not_first_adapter_run",
    "entry_file_missing",
    "entry_changed",
    "entry_not_verifiable",
)

#: The marker the secret scanner leaves in a redacted string (ADR-0009).
_REDACTION_MARKER = "[REDACTED:"
_REEXECUTABLE_ENTRY_KINDS = frozenset({"script", "module", "code"})


@dataclass(frozen=True)
class ReexecutionPlan:
    """What a re-executing replay runs, and where."""

    command: list[str]
    #: Working directory to run in; ``None`` = the invoking shell's (as for
    #: every ``nova capture`` capsule -- owner decision O4: unchanged).
    cwd: Path | None = None
    #: Adapter runs the re-run process may make (``None``: not checked).
    adapter_runs_expected: int | None = None
    source: Literal["command", "process_invocation"] = "command"


@dataclass(frozen=True)
class _GuardFailure:
    guard: str
    detail: str


def _command(manifest: dict[str, Any]) -> list[str]:
    raw = manifest.get("command") or []
    if isinstance(raw, str):
        return [raw]
    return [str(part) for part in raw]


def _facet(manifest: dict[str, Any]) -> Any:
    facets = manifest.get("facets")
    if not isinstance(facets, dict):
        return None
    from novafabric.capture.process_invocation import FACET_NAME

    return facets.get(FACET_NAME)


def _expand_home(value: str, home: Path) -> str:
    if value == "~":
        return str(home)
    if value.startswith("~/"):
        return str(home / value[2:])
    return value


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except OSError:
        return None


def _host_name(entry_kind: str, entry: str | None) -> str | None:
    if not entry:
        return None
    if entry_kind == "module":
        name = entry.split(".", 1)[0]
    elif entry_kind == "script":
        name = PurePath(entry).name
        name = name[:-3] if name.endswith(".py") else name
    else:
        return None
    return name if name.lower() in LONG_LIVED_HOSTS else None


def _check_invocation(raw: Any, *, home: Path) -> ReexecutionPlan | _GuardFailure:
    """Every guard, in :data:`GUARDS` order; the first failure wins."""
    from novafabric.capture.process_invocation import ProcessInvocation

    try:
        inv = ProcessInvocation.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        where, why = "the facet", str(exc)
        if errors:
            where = ".".join(str(p) for p in errors[0]["loc"]) or where
            why = errors[0]["msg"]
        return _GuardFailure(
            "facet_malformed", f"the process_invocation facet is malformed ({where}: {why})"
        )
    if inv.entry_kind not in _REEXECUTABLE_ENTRY_KINDS:
        return _GuardFailure(
            "entry_not_reproducible",
            f"the process was started as {inv.entry_kind!r} (only a script, a -m module "
            "or -c code can be started again from its argv)",
        )
    host = _host_name(inv.entry_kind, inv.entry)
    if inv.host_modules or host:
        named = host or ", ".join(inv.host_modules)
        return _GuardFailure(
            "long_lived_host",
            f"the adapter ran inside a long-lived or harness host ({named}); re-running "
            "the process would re-run the host, not the agent",
        )
    if any(_REDACTION_MARKER in part for part in inv.argv):
        return _GuardFailure(
            "argv_redacted",
            "an argument was redacted as a secret at capture, so the invocation cannot "
            "be reconstructed",
        )
    if inv.adapter_run_ordinal != 1:
        return _GuardFailure(
            "not_first_adapter_run",
            f"this was adapter run #{inv.adapter_run_ordinal} of its process; re-running "
            "the process would replay every earlier run against this capsule (only the "
            "first run of a process is re-run)",
        )
    cwd = Path(_expand_home(inv.cwd, home))
    if inv.entry_kind in ("script", "module"):
        if inv.entry_file is None or inv.entry_sha256 is None:
            return _GuardFailure(
                "entry_not_verifiable",
                "the capsule records no digest of the entry file, so an edited program "
                "could not be detected",
            )
        entry_file = Path(_expand_home(inv.entry_file, home))
        if not entry_file.is_file():
            return _GuardFailure(
                "entry_file_missing",
                f"the entry file {inv.entry_file} does not exist on this host",
            )
        if _sha256(entry_file) != inv.entry_sha256:
            return _GuardFailure(
                "entry_changed",
                f"the entry file {inv.entry_file} changed since capture (sha256 differs), "
                "so the program re-run would not be the one recorded",
            )
    return ReexecutionPlan(
        command=[_expand_home(part, home) for part in inv.argv],
        cwd=cwd,
        adapter_runs_expected=inv.adapter_run_ordinal,
        source="process_invocation",
    )


def _assess(manifest: dict[str, Any]) -> ReexecutionPlan | _GuardFailure | str:
    """A plan, a failed guard (adapter capsule with a facet), or why there is no
    command at all."""
    capture_mode = manifest.get("capture_mode")
    command = _command(manifest)
    label = f" ({command[0]!r} is a label, not a program)" if command else ""
    if isinstance(capture_mode, str) and capture_mode in NON_REEXECUTABLE_CAPTURE_MODES:
        facet = _facet(manifest)
        if capture_mode == ADAPTER_CAPTURE_MODE and facet is not None:
            return _check_invocation(facet, home=Path(os.path.expanduser("~")))
        how = NON_REEXECUTABLE_CAPTURE_MODES[capture_mode]
        return (
            f"this capsule {how} (capture_mode: {capture_mode}) and records no "
            f"command to re-run{label}"
        )
    if not command:
        return "this capsule records no command to re-run (capsule.yaml command is empty)"
    if command[0].startswith(PLACEHOLDER_COMMAND_PREFIX):
        return f"this capsule records no command to re-run{label}"
    return ReexecutionPlan(command=command)


def _guard_reason(manifest: dict[str, Any], failure: _GuardFailure) -> str:
    command = _command(manifest)
    label = f" ({command[0]!r} is a label, not a program)" if command else ""
    how = NON_REEXECUTABLE_CAPTURE_MODES[ADAPTER_CAPTURE_MODE]
    return (
        f"this capsule {how} (capture_mode: {ADAPTER_CAPTURE_MODE}){label}, and the "
        f"process invocation it records cannot be re-run: {failure.detail} "
        f"(guard: {failure.guard})"
    )


def not_reexecutable_reason(manifest: dict[str, Any]) -> str | None:
    """Why *manifest* records no command a replay can re-run, or ``None``."""
    assessed = _assess(manifest)
    if isinstance(assessed, ReexecutionPlan):
        return None
    if isinstance(assessed, _GuardFailure):
        return _guard_reason(manifest, assessed)
    return assessed


def _message(
    manifest: dict[str, Any], mode: str, assessed: _GuardFailure | str
) -> tuple[str, str | None]:
    modes = (
        "Modes that work on it: forensic (inspect the capsule) and semantic (score the "
        "recorded responses); --mode exact reports it not eligible."
    )
    if isinstance(assessed, _GuardFailure):
        return (
            f"--mode {mode} cannot replay it: {_guard_reason(manifest, assessed)}. {modes} "
            "An adapter capsule is re-run only when its recorded process invocation "
            "passes every guard (ADR-0306 Q7).",
            assessed.guard,
        )
    return (
        f"--mode {mode} cannot replay it: {assessed}. {modes} Re-running such a "
        "capsule is not supported (ADR-0306, open question 7).",
        None,
    )


def refusal_message(manifest: dict[str, Any], mode: str) -> str | None:
    """The full message for refusing *mode* on *manifest*, or ``None``."""
    assessed = _assess(manifest)
    if isinstance(assessed, ReexecutionPlan):
        return None
    return _message(manifest, mode, assessed)[0]


def require_reexecution_plan(manifest: dict[str, Any], mode: str) -> ReexecutionPlan:
    """Return what to re-run, or raise :class:`CapsuleNotReplayableError`."""
    assessed = _assess(manifest)
    if isinstance(assessed, ReexecutionPlan):
        return assessed
    message, guard = _message(manifest, mode, assessed)
    capture_mode = manifest.get("capture_mode")
    raise CapsuleNotReplayableError(
        message,
        capture_mode=capture_mode if isinstance(capture_mode, str) else None,
        command=_command(manifest),
        guard=guard,
    )


def require_reexecutable_command(manifest: dict[str, Any], mode: str) -> list[str]:
    """Return the command to re-run, or raise :class:`CapsuleNotReplayableError`."""
    return require_reexecution_plan(manifest, mode).command
