"""Observe the process tree of a mocked replay (ADR-0319, #52 parts 1 and 2).

**Standard library only -- this module must never import novafabric.** Its source
is also written into the replay's site directory as ``_nf_child_observer.py`` and
loaded by the generated ``sitecustomize.py`` in Python child processes whose
environment no longer carries the replay's dispatcher variables -- an MCP stdio
client, for example, starts its server with a short allowlisted environment. Such
a child runs this observer (sockets and child processes, reported, never blocked)
and serves nothing; a child that inherits the full environment loads the replay
dispatcher as before.

Pieces:

* :class:`ProcessObserver` -- a :func:`sys.addaudithook` hook on CPython's
  process-creation audit events (verified on CPython 3.12):

  * ``subprocess.Popen`` (``executable, args, cwd, env``) -- also behind
    ``subprocess.run``, ``os.popen`` and ``asyncio`` subprocesses;
  * ``os.system`` (``command``);
  * ``os.posix_spawn`` (``path, argv, env``) -- also ``os.posix_spawnp``; skipped
    when it is the spawn of the ``subprocess.Popen`` just reported (``Popen`` uses
    it when ``close_fds=False``);
  * ``os.fork`` / ``os.forkpty`` -- also behind ``os.spawn*`` and ``pty.spawn``;
  * ``os.exec`` (``path, args, env``) -- counted when the observed process itself
    becomes another program; in a fork child it only names the program the
    counted fork started.

  Only the basename of ``argv[0]`` is logged -- never arguments or environment,
  which may carry secrets. A process started through ``ctypes``/C code that
  bypasses these calls is not seen. An audit hook cannot be removed: after
  :meth:`ProcessObserver.uninstall` it stays registered and does nothing.

* :class:`SocketObserver` -- ``socket.socket.connect``/``connect_ex`` attempts to
  IPv4/IPv6 addresses (host and port only), for the child bootstrap; the replayed
  process itself uses the dispatcher's ``NetworkObserver``.

* :func:`start_tree_observation` -- one ``observer_started`` event per observed
  process (``role``: ``root`` / ``child`` / ``fork``, with ``ppid``), the
  ``subprocess.Popen`` environment propagation (:func:`child_env`) and a fork
  hook. The engine folds these into ``child_processes_network_observed`` /
  ``child_processes_unobserved`` (``_contract.summarize``).
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
import threading
from typing import Any

#: Path of the replay's event log; set for every observed process.
OBSERVE_ENV = "NOVAFABRIC_REPLAY_OBSERVE_EVENTS_PATH"
#: The replay's site directory (``sitecustomize.py`` + this module's copy).
SITE_ENV = "NOVAFABRIC_REPLAY_SITE_DIR"
#: ``root`` for the process the engine started, ``child`` for its descendants.
ROLE_ENV = "NOVAFABRIC_REPLAY_OBSERVER_ROLE"
#: Present only where the full replay dispatcher activates (``_engine``).
QUEUE_ENV = "NOVAFABRIC_REPLAY_QUEUE_PATH"

#: Most ``child_process`` events one observed process writes; beyond it a single
#: ``child_process_capped`` event says the count is a lower bound.
CHILD_PROCESS_EVENT_CAP = 10_000
#: Most ``network_live`` events one observed process writes (as ``NetworkObserver``).
NETWORK_EVENT_CAP = 10_000

#: Audit events that start a process (counted).
_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec",
})

#: A program name safe to log: anything else is reported as ``?``.
_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]{1,64}")

#: ``depth > 0`` while this thread is inside ``subprocess.Popen.__init__``: a fork
#: there (``preexec_fn``) is Popen's own, already counted, and execs at once.
_IN_POPEN = threading.local()


class EventLog:
    """Append one JSON line per event to the replay's event log; never raises."""

    def __init__(self, path: str | None) -> None:
        self._path = path or None

    def emit(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        line = json.dumps({"event": event, "pid": os.getpid(), **fields}, default=str)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


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
    """Log one ``child_process`` event per process the observed process starts."""

    def __init__(self, events: Any, *, cap: int = CHILD_PROCESS_EVENT_CAP) -> None:
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
                # A fork child becoming a program: the fork was counted; this
                # ends the fork child's observation (summarize revokes it).
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


class SocketObserver:
    """Report -- never block -- IPv4/IPv6 connection attempts of this process."""

    def __init__(self, events: Any, *, cap: int = NETWORK_EVENT_CAP) -> None:
        self._events = events
        self._cap = cap
        self._count = 0

    def install(self) -> bool:
        try:
            import socket

            for name in ("connect", "connect_ex"):
                original = getattr(socket.socket, name)
                setattr(socket.socket, name, self._wrap(original))
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            return False
        return True

    def _wrap(self, original: Any) -> Any:
        observer = self

        @functools.wraps(original)
        def observed(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
            observer.seen(sock, address)
            return original(sock, address, *args, **kwargs)

        return observed

    def seen(self, sock: Any, address: Any) -> None:
        try:
            import socket

            if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
                return
            if not isinstance(address, tuple) or len(address) < 2:
                return
            self._count += 1
            if self._count <= self._cap:
                self._events.emit("network_live", host=str(address[0]), port=int(address[1]))
            elif self._count == self._cap + 1:
                self._events.emit("network_live_capped", cap=self._cap)
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            pass


# ── environment propagation ──────────────────────────────────────────────────


def child_env(env: Any) -> dict[str, str] | None:
    """The environment a child should be started with so that it reports in.

    ``None`` means "leave the call as it is": not in a replay, the mapping is not
    plain ``str -> str`` (``bytes`` keys are left alone), or it already carries
    everything. Otherwise a **new** dict -- the caller's mapping is never
    modified: the replay's event log, site dir and ``role: child`` set, and the
    site dir prepended to ``PYTHONPATH`` (the child's own entries keep their
    order after it).
    """
    events = os.environ.get(OBSERVE_ENV)
    site = os.environ.get(SITE_ENV)
    if not events or not site:
        return None
    base = os.environ if env is None else env
    try:
        items = list(base.items())
    except Exception:  # noqa: BLE001 -- not a mapping: leave it to Popen
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in items):
        return None
    new = dict(items)
    new[OBSERVE_ENV] = events
    new[SITE_ENV] = site
    new[ROLE_ENV] = "child"
    entries = [p for p in new.get("PYTHONPATH", "").split(os.pathsep) if p]
    if site not in entries:
        new["PYTHONPATH"] = os.pathsep.join([site, *entries])
    if new == dict(items):
        return None
    return new


#: Position of ``env`` in ``subprocess.Popen.__init__``'s parameters after
#: ``self`` (stable since Python 3.0: args, bufsize, executable, stdin, stdout,
#: stderr, preexec_fn, close_fds, shell, cwd, env).
_POPEN_ENV_POSITION = 10


def install_env_propagation() -> bool:
    """Wrap ``subprocess.Popen.__init__`` so children get :func:`child_env`."""
    try:
        import subprocess

        original = subprocess.Popen.__init__
        if getattr(original, "_nf_propagates_env", False):
            return True

        @functools.wraps(original)
        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:  # noqa: N807
            try:
                if len(args) > _POPEN_ENV_POSITION:
                    new = child_env(args[_POPEN_ENV_POSITION])
                    if new is not None:
                        args = (
                            *args[:_POPEN_ENV_POSITION], new, *args[_POPEN_ENV_POSITION + 1:]
                        )
                else:
                    new = child_env(kwargs.get("env"))
                    if new is not None:
                        kwargs["env"] = new
            except Exception:  # noqa: BLE001 -- never break the workload's spawn
                pass
            _IN_POPEN.depth = getattr(_IN_POPEN, "depth", 0) + 1
            try:
                original(self, *args, **kwargs)
            finally:
                _IN_POPEN.depth -= 1

        __init__._nf_propagates_env = True  # type: ignore[attr-defined]
        subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    except Exception:  # noqa: BLE001 -- observation must never break the workload
        return False
    return True


def start_tree_observation(events: Any, *, observer: str) -> None:
    """Record this process as observed and make its children report in too.

    Emits ``observer_started`` (``role`` from the environment: ``root`` for the
    process the engine started, else ``child``), then marks the environment so
    every descendant is a ``child``, wraps ``Popen`` (:func:`child_env`) and
    registers a fork hook: a fork child that stays Python is observed by the
    observers it inherited and says so (``role: fork``); one that then execs is
    revoked by its ``child_process_exec`` event.
    """
    role = "root" if os.environ.get(ROLE_ENV) == "root" else "child"
    events.emit("observer_started", role=role, ppid=os.getppid(), observer=observer)
    os.environ[ROLE_ENV] = "child"
    install_env_propagation()

    def _after_fork_in_child() -> None:
        if getattr(_IN_POPEN, "depth", 0):
            return  # Popen's own fork (preexec_fn): it execs at once, counted already
        events.emit("observer_started", role="fork", ppid=os.getppid(), observer=observer)

    try:
        os.register_at_fork(after_in_child=_after_fork_in_child)
    except Exception:  # noqa: BLE001 -- e.g. a platform without fork
        pass


def _chain_shadowed_sitecustomize(own_dir: str) -> None:
    """Run the ``sitecustomize`` this replay's site dir shadows (e.g. a tracer's
    bootstrap on the child's own ``PYTHONPATH``), as Python would have."""
    import importlib.machinery
    import importlib.util

    own = os.path.realpath(own_dir)
    paths = [p for p in sys.path if os.path.realpath(p or os.curdir) != own]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def bootstrap_child(site_file: str) -> None:
    """Entry point of a child whose environment lost the replay dispatcher.

    Observes only -- sockets and child processes -- and serves nothing: the
    capsule's recorded answers belong to the processes that recorded them.
    Writes nothing to stdout (an MCP stdio server speaks its protocol there).
    """
    events = EventLog(os.environ.get(OBSERVE_ENV))
    if SocketObserver(events).install():
        events.emit("network_observer_installed")
    if ProcessObserver(events).install():
        events.emit("process_observer_installed")
    start_tree_observation(events, observer="child-bootstrap")
    try:
        _chain_shadowed_sitecustomize(os.path.dirname(os.path.abspath(site_file)))
    except Exception as exc:  # noqa: BLE001 -- the shadowed one failed, as it would have
        print(f"[novafabric] sitecustomize shadowed by the replay failed: {exc}", file=sys.stderr)
