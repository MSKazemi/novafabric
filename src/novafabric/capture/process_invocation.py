"""How the process that ran a framework adapter was started (ADR-0306 Q7, Option 1).

An adapter capsule's ``command`` is a ``@framework:name`` label, not a program,
so ``nova replay`` used to refuse every one of them. Every adapter now also
records the **process invocation** -- the ``process_invocation`` facet -- and
replay re-runs it when every guard in ``novafabric.replay._replayability`` passes.
``command`` and ``capture_mode`` are unchanged, so no existing reader is affected.

What is recorded (and nothing else):

* ``argv`` -- ``[sys.executable, *sys.orig_argv[1:]]``: the interpreter that
  really ran, not the one typed, then the arguments exactly as given. The home
  directory is written as ``~``. The manifest redaction pass (ADR-0009) applies;
  a low-entropy secret in an argument is not detected (the same residual as
  ``nova capture``'s argv).
* ``entry_kind`` / ``entry`` -- ``script`` (path as typed), ``module`` (``-m``
  name), ``code`` (``-c`` source), or ``interactive`` / ``stdin`` / ``unknown``,
  which replay never re-runs.
* ``entry_file`` / ``entry_sha256`` -- the ``__main__`` file and its digest, so a
  replay can refuse a program edited since capture.
* ``cwd``, ``stdin_isatty``.
* ``adapter_run_ordinal`` -- 1 for the first adapter run in this process, 2 for
  the second, ... (replay re-runs ordinal 1 only).
* ``host_modules`` -- names of loaded modules that mark a long-lived or harness
  host (a notebook kernel, a test runner, a server).

No environment variable names or values are recorded here (owner decision O2,
safe default: no).

Under mocked replay (``NOVAFABRIC_REPLAY_MODE`` set by the replay engine) an
adapter run writes **no** capsule; it reports itself to the replay's event log
instead, and a run beyond the recorded one is an ``adapter_run_count_mismatch``
divergence. Never raises into a captured workload: a snapshot that cannot be
taken is recorded as absent, and the capsule is then simply not re-runnable.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FACET_NAME = "process_invocation"
SCHEMA_VERSION = "0.1.0"

EntryKind = Literal["script", "module", "code", "interactive", "stdin", "unknown"]

#: Loaded modules that mark a long-lived or harness host: re-running the
#: process would re-run the host, not the agent (a notebook kernel, a test
#: session or its xdist worker, a Streamlit app). Matched against
#: ``sys.modules`` at each adapter run. Only modules whose presence means the
#: process *is* that host: a server package such as ``uvicorn`` is imported by
#: libraries that never serve (measured: one is loaded after a capsule is
#: finalized), so servers are recognised by their entry point instead
#: (``novafabric.replay._replayability.LONG_LIVED_HOSTS``).
HOST_MODULES: frozenset[str] = frozenset({"ipykernel", "_pytest", "execnet", "streamlit"})

#: Set by the replay engine in a re-run adapter process: how many adapter runs
#: the capsule recorded (the replay re-runs ordinal 1, so ``1``).
ADAPTER_RUNS_ENV = "NOVAFABRIC_REPLAY_ADAPTER_RUNS"
_REPLAY_MODE_ENV = "NOVAFABRIC_REPLAY_MODE"
_EVENTS_ENV = "NOVAFABRIC_REPLAY_EVENTS_PATH"
_POLICY_ENV = "NOVAFABRIC_REPLAY_DIVERGENCE_POLICY"

#: Python options that take a separate argument (``-W ignore``, ``-X dev``).
_OPTIONS_WITH_ARGUMENT = frozenset("WX")
_LONG_OPTIONS_WITH_ARGUMENT = frozenset({"--check-hash-based-pycs"})

#: Largest entry file digested; a bigger one is recorded without a digest.
_MAX_ENTRY_BYTES = 64 * 1024 * 1024


class ProcessInvocation(BaseModel):
    """The ``process_invocation`` facet body."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["0.1.0"] = "0.1.0"  # SCHEMA_VERSION
    argv: list[str] = Field(min_length=1)
    entry_kind: EntryKind
    entry: str | None = None
    entry_file: str | None = None
    entry_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    cwd: str
    stdin_isatty: bool | None = None
    adapter_run_ordinal: int = Field(ge=1)
    host_modules: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ── pure parts ───────────────────────────────────────────────────────────────


def _parse_orig_argv(orig_argv: Sequence[str]) -> tuple[EntryKind, str | None]:
    """What ``python [options] <entry> ...`` ran, from ``sys.orig_argv``."""
    i = 1
    while i < len(orig_argv):
        token = orig_argv[i]
        if token == "-":
            return "stdin", None
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            i += 2 if name in _LONG_OPTIONS_WITH_ARGUMENT and "=" not in token else 1
            continue
        if token.startswith("-") and len(token) > 1:
            flags = token[1:]
            for j, flag in enumerate(flags):
                rest = flags[j + 1:]
                if flag in "cm":
                    value = rest or (orig_argv[i + 1] if i + 1 < len(orig_argv) else None)
                    if not value:
                        return "unknown", None
                    return ("code" if flag == "c" else "module"), value
                if flag in _OPTIONS_WITH_ARGUMENT:
                    if not rest:
                        i += 1  # the argument is the next token
                    break
            i += 1
            continue
        return "script", token
    return "interactive", None


def classify_entry(
    orig_argv: Sequence[str], argv: Sequence[str], *, interactive: bool, frozen: bool
) -> tuple[EntryKind, str | None]:
    """``(entry_kind, entry)`` of a process, cross-checked against ``sys.argv``.

    A process whose ``sys.argv`` disagrees with what ``sys.orig_argv`` says it
    ran (a program that rewrote ``sys.argv``, an embedded or frozen
    interpreter) is ``unknown``: its argv cannot be trusted to start it again.
    """
    if frozen or not orig_argv:
        return "unknown", None
    if interactive:
        return "interactive", None
    kind, entry = _parse_orig_argv(orig_argv)
    first = argv[0] if argv else None
    if kind == "script" and first != entry:
        return "unknown", None
    if kind == "code" and first != "-c":
        return "unknown", None
    if kind == "module" and (first is None or first in ("-c", "-m") or not first.endswith(".py")):
        return "unknown", None
    return kind, entry


def home_relative(value: str, home: Path) -> str:
    """*value* with a leading home directory written as ``~``."""
    prefix = str(home)
    if prefix and prefix != os.sep:
        if value == prefix:
            return "~"
        if value.startswith(prefix + os.sep):
            return "~" + value[len(prefix):]
    return value


def _digest(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ENTRY_BYTES:
            return None
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except OSError:
        return None


def build_invocation(
    *,
    ordinal: int,
    orig_argv: Sequence[str],
    argv: Sequence[str],
    executable: str,
    cwd: Path,
    home: Path,
    main_file: str | None,
    loaded_modules: Iterable[str],
    stdin_isatty: bool | None,
    interactive: bool,
    frozen: bool,
) -> ProcessInvocation:
    """The facet for a process described by the arguments (no ``sys`` reads)."""
    kind, entry = classify_entry(orig_argv, argv, interactive=interactive, frozen=frozen)
    entry_file: Path | None = None
    if kind in ("script", "module") and main_file:
        entry_file = Path(main_file)
        if not entry_file.is_absolute():
            entry_file = cwd / entry_file
    if kind == "script" and entry is not None and entry_file is not None:
        # The recorded cwd must find the script again: a program that changed
        # directory before the adapter ran would be re-run from the wrong place.
        typed = Path(entry) if Path(entry).is_absolute() else cwd / entry
        try:
            same = typed.resolve() == entry_file.resolve()
        except OSError:
            same = False
        if not same:
            kind, entry, entry_file = "unknown", None, None
    real_argv = [executable or (orig_argv[0] if orig_argv else ""), *orig_argv[1:]]
    return ProcessInvocation(
        argv=[home_relative(a, home) for a in real_argv] or [""],
        entry_kind=kind,
        entry=entry,
        entry_file=home_relative(str(entry_file), home) if entry_file is not None else None,
        entry_sha256=_digest(entry_file) if entry_file is not None else None,
        cwd=home_relative(str(cwd), home),
        stdin_isatty=stdin_isatty,
        adapter_run_ordinal=ordinal,
        host_modules=sorted(set(loaded_modules) & HOST_MODULES),
    )


def current_invocation(ordinal: int) -> ProcessInvocation | None:
    """The facet for *this* process; ``None`` if it cannot be taken (never raises)."""
    try:
        main = sys.modules.get("__main__")
        try:
            isatty: bool | None = bool(sys.stdin is not None and sys.stdin.isatty())
        except (OSError, ValueError):
            isatty = None
        return build_invocation(
            ordinal=ordinal,
            orig_argv=list(getattr(sys, "orig_argv", [])),
            argv=list(sys.argv),
            executable=sys.executable,
            cwd=Path.cwd(),
            home=Path.home(),
            main_file=getattr(main, "__file__", None),
            loaded_modules=list(sys.modules),
            stdin_isatty=isatty,
            interactive=bool(sys.flags.interactive) or hasattr(sys, "ps1"),
            frozen=bool(getattr(sys, "frozen", False)),
        )
    except Exception as exc:  # capture never blocks the workload
        from novafabric.capture.hooks._fail_open import note_capture_failure

        note_capture_failure("adapter", "process-invocation", exc)
        return None


def attach_facet(manifest: dict[str, Any], facet: dict[str, Any] | None) -> None:
    """Add the facet to *manifest* (additively); ``None`` adds nothing."""
    if facet is not None:
        manifest.setdefault("facets", {})[FACET_NAME] = dict(facet)


# ── the per-process run ordinal ──────────────────────────────────────────────


class RunCounter:
    """Counts top-level adapter runs in one process.

    Process state by nature (the ordinal *is* "how many runs this process has
    made"), so it lives in one instance; a forked child starts again at 1.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._count = 0

    def next(self) -> int:
        with self._lock:
            pid = os.getpid()
            if pid != self._pid:
                self._pid, self._count = pid, 0
            self._count += 1
            return self._count


_COUNTER = RunCounter()


# ── the run lifecycle every adapter calls ────────────────────────────────────


@dataclass(frozen=True)
class AdapterRun:
    """What :func:`begin_adapter_run` decided for one top-level adapter run.

    ``replaying``: the process is being re-run by mocked replay -- open **no**
    capsule. ``facet``: the invocation to attach to the capsule (capture only).
    """

    framework: str
    ordinal: int
    replaying: bool
    facet: dict[str, Any] | None = None

    def replay_finished(self, exit_code: int, status: str) -> None:
        """Report how a replayed adapter run ended (no-op when capturing)."""
        if not self.replaying:
            return
        _events().emit(
            "adapter_run_end", framework=self.framework, ordinal=self.ordinal,
            exit_code=exit_code, status=status,
        )


def _events() -> Any:
    from novafabric.replay._contract import ReplayEventLog

    return ReplayEventLog(os.environ.get(_EVENTS_ENV) or None)


def _expected_runs() -> int | None:
    raw = os.environ.get(ADAPTER_RUNS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def begin_adapter_run(framework: str) -> AdapterRun:
    """Count one top-level adapter run; snapshot the invocation, or report it to
    a replay.

    Raises only under mocked replay with the default fail-closed policy, for a
    run beyond the ones the capsule recorded (:class:`AdapterRunCountMismatchError`
    -- the same divergence path the replay dispatchers use).
    """
    ordinal = _COUNTER.next()
    if not os.environ.get(_REPLAY_MODE_ENV):
        snapshot = current_invocation(ordinal)
        return AdapterRun(framework, ordinal, False, snapshot.as_dict() if snapshot else None)

    events = _events()
    events.emit("adapter_run", framework=framework, ordinal=ordinal)
    expected = _expected_runs()
    if expected is not None and ordinal > expected:
        from novafabric.replay._errors import AdapterRunCountMismatchError

        policy = os.environ.get(_POLICY_ENV, "fail")
        error = AdapterRunCountMismatchError(
            f"the replayed process started adapter run #{ordinal} ({framework}); the "
            f"capsule recorded {expected} -- its model and tool calls were never "
            "recorded, so they cannot be served",
            framework=framework, ordinal=ordinal, recorded=expected,
        )
        events.emit("divergence", policy=policy, **error.as_dict())
        if policy != "warn":
            raise error
        print(f"[novafabric] mocked replay (permissive): {error}", file=sys.stderr)
    return AdapterRun(framework, ordinal, True)
