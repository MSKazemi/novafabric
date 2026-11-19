"""Report -- never block -- child processes the replayed process starts (ADR-0319).

``NetworkObserver`` sees only the replayed **Python** process's sockets
(``network_observation_scope: python-process``). A workload that runs ``ssh``,
``curl`` or ``git`` reaches the network from a child the observer never sees, so
the replay result says how many children were started and what they were.

Installed with :func:`sys.addaudithook` on CPython's process-creation audit
events (verified on CPython 3.12):

* ``subprocess.Popen`` (``executable, args, cwd, env``) -- also behind
  ``subprocess.run``, ``os.popen`` and ``asyncio`` subprocesses;
* ``os.system`` (``command``);
* ``os.posix_spawn`` (``path, argv, env``) -- also ``os.posix_spawnp``; skipped
  when it is the spawn of the ``subprocess.Popen`` just reported (``Popen`` uses
  it when ``close_fds=False``);
* ``os.fork`` / ``os.forkpty`` -- also behind ``os.spawn*`` and ``pty.spawn``;
* ``os.exec`` (``path, args, env``) -- counted when the replayed process itself
  becomes another program; in a fork child it only names the program the
  counted fork started.

Only the basename of ``argv[0]`` is logged -- never arguments or environment,
which may carry secrets. A process started through ``ctypes``/C code that
bypasses these calls is not seen. An audit hook cannot be removed: after
:meth:`ProcessObserver.uninstall` it stays registered and does nothing.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any

from novafabric.replay._contract import ReplayEventLog

#: Most ``child_process`` events one replayed process writes; beyond it a single
#: ``child_process_capped`` event says the count is a lower bound.
CHILD_PROCESS_EVENT_CAP = 10_000

#: Audit events that start a process (counted).
_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec",
})

#: A program name safe to log: anything else is reported as ``?``.
_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]{1,64}")


def program_name(argv0: Any) -> str:
    """Basename of ``argv[0]`` if it looks like a program name, else ``"?"``."""
    if isinstance(argv0, bytes):
        argv0 = argv0.decode("utf-8", "replace")
    elif isinstance(argv0, os.PathLike):
        argv0 = os.fspath(argv0)
        if isinstance(argv0, bytes):
            argv0 = argv0.decode("utf-8", "replace")
    if not isinstance(argv0, str) or not argv0:
        return "?"
    name = argv0.replace("\\", "/").rsplit("/", 1)[-1]
    return name if _SAFE_NAME.fullmatch(name) else "?"


def _first_arg(args: Any) -> Any:
    if isinstance(args, (list, tuple)):
        return args[0] if args else None
    if isinstance(args, (str, bytes)):
        return _shell_program(args)
    return args


def _shell_program(command: Any) -> str | None:
    """The program a shell command line runs: its first word that is not a
    ``NAME=value`` assignment (an assignment may carry a secret)."""
    if isinstance(command, bytes):
        command = command.decode("utf-8", "replace")
    if not isinstance(command, str):
        return None
    for word in command.split():
        if "=" not in word:
            return word
    return None


class ProcessObserver:
    """Log one ``child_process`` event per process the replayed process starts."""

    def __init__(
        self, events: ReplayEventLog, *, cap: int = CHILD_PROCESS_EVENT_CAP
    ) -> None:
        self._events = events
        self._cap = cap
        self._count = 0
        self._active = False
        self._pid = os.getpid()
        self._last_popen = threading.local()

    def install(self) -> bool:
        try:
            sys.addaudithook(self._hook)
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            return False
        self._pid = os.getpid()
        self._active = True
        return True

    def uninstall(self) -> None:
        """Stop observing (the audit hook itself cannot be removed)."""
        self._active = False

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if not self._active or event not in _SPAWN_EVENTS:
            return
        try:
            self._seen(event, args)
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            pass

    def _seen(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "subprocess.Popen":
            executable, argv = (list(args) + [None, None])[:2]
            name = program_name(executable if executable is not None else _first_arg(argv))
            self._last_popen.argv = argv
        elif event == "os.posix_spawn":
            argv = args[1] if len(args) > 1 else None
            if argv is not None and getattr(self._last_popen, "argv", None) == argv:
                self._last_popen.argv = None  # the Popen above, already counted
                return
            name = program_name(args[0] if args else None)
        elif event == "os.system":
            name = program_name(_shell_program(args[0] if args else None))
        elif event == "os.exec":
            name = program_name(args[0] if args else None)
            if os.getpid() != self._pid:
                # A fork child becoming a program: the fork was counted.
                self._events.emit("child_process_exec", argv0=name)
                return
        else:  # os.fork / os.forkpty: a copy of this process, no program yet
            name = None
        self._count += 1
        if self._count <= self._cap:
            self._events.emit(
                "child_process", audit_event=event,
                **({"argv0": name} if name is not None else {}),
            )
        elif self._count == self._cap + 1:
            self._events.emit("child_process_capped", cap=self._cap)
