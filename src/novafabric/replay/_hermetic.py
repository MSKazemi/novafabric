"""Opt-in hermetic mocked replay: ``nova replay --hermetic`` (ADR-0322, experimental).

The replayed command -- and every process it starts -- runs under bubblewrap
(``bwrap``, an optional system program, never a Python dependency) with:

* **network**: a private network namespace holding only its own loopback (``lo``
  up); nothing outside the sandbox is reachable, host loopback services included.
  Served model/tool calls are unaffected: they are answered in-process, with no
  socket;
* **filesystem**: the host tree bound read-only; ``/run``, ``/tmp``, ``/var/tmp``
  and ``$XDG_RUNTIME_DIR`` masked with empty tmpfs (host Unix sockets such as the
  Docker and D-Bus sockets live there); writable -- bound read-write after the
  masks -- are exactly the replayed process's working directory (a workload writes
  its outputs there) and the replay result directory, which also holds the
  replay's temp dir (dispatcher queues and event log) during the run. A writable
  directory that would undo the read-only host or a mask (``/``, a masked
  directory or one of its ancestors, or anything under ``/run`` /
  ``$XDG_RUNTIME_DIR``) is refused (``check: writable-path``). There is no flag
  to add more;
* **processes**: new pid/ipc/uts/user namespaces, ``--disable-userns`` (no nested
  namespace from inside), ``--new-session``, ``--die-with-parent``;
* **environment**: variables naming host sockets (``SSH_AUTH_SOCK``,
  ``DBUS_SESSION_BUS_ADDRESS``, ``DOCKER_HOST``) are not passed in.

No silent downgrade: if bwrap is missing, the platform is not Linux, or a
preflight ``bwrap <same args> -- true`` fails (an AppArmor userns restriction,
``kernel.unprivileged_userns_clone=0``, a container seccomp profile), the replay is
refused with ``error.type: HermeticUnavailable`` (exit 5) before anything runs.

Not implemented (ADR-0322 slices to come): counting blocked attempts from outside
the process with strace (``counting: "none"``; the counts are ``null``, never 0),
a copy-on-write overlay of the working directory (writes there reach the host),
and a second backend (``unshare``/Landlock). A Unix socket under a path that stays
visible (for example in ``$HOME`` or the working directory) remains connectable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Exit status of ``nova replay --hermetic`` when the sandbox cannot be set up
#: (1-4 and 124 are taken, ``cli/replay.py``).
EXIT_HERMETIC_UNAVAILABLE = 5

MECHANISM = "bwrap"

#: Namespaces and process controls, in the order bwrap receives them.
NAMESPACE_ARGS: tuple[str, ...] = (
    "--unshare-user", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--disable-userns", "--new-session", "--die-with-parent",
)

#: Directories masked with an empty tmpfs: where host Unix sockets live.
MASKED_DIRS: tuple[str, ...] = ("/run", "/tmp", "/var/tmp")

#: Environment variables naming host sockets: not passed into the sandbox.
HOST_SOCKET_ENV: tuple[str, ...] = ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "DOCKER_HOST")

#: Seconds the preflight ``bwrap ... -- true`` may take.
PREFLIGHT_TIMEOUT_S = 20

#: Most characters of a failed preflight's stderr kept on the error.
_STDERR_CAP = 2000

_VERSION = re.compile(r"(\d+(?:\.\d+)+)")


class HermeticUnavailableError(Exception):
    """``--hermetic`` was asked for and the sandbox cannot be set up here.

    Raised before anything runs; the engine records ``status: aborted`` with
    ``error.type: HermeticUnavailable`` and the failed ``check``
    (``platform`` / ``bwrap-missing`` / ``preflight``). The replay never runs
    un-isolated under a hermetic label.
    """

    error_type = "HermeticUnavailable"
    exit_code = EXIT_HERMETIC_UNAVAILABLE

    def __init__(self, check: str, message: str, stderr: str | None = None) -> None:
        super().__init__(message)
        self.check = check
        self.stderr = stderr

    def as_error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.error_type, "message": str(self), "check": self.check}
        if self.stderr:
            out["stderr"] = self.stderr
        return out


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _masked_dirs(runtime_dir: str | None, is_dir: Callable[[str], bool]) -> list[str]:
    masked = [d for d in MASKED_DIRS if is_dir(d)]
    if runtime_dir and not any(_under(runtime_dir, d) for d in masked) and is_dir(runtime_dir):
        masked.append(runtime_dir)
    return masked


def check_writable(
    paths: Sequence[Path],
    *,
    runtime_dir: str | None,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> None:
    """Refuse a writable directory that would undo the read-only host or a mask.

    ``/`` would make the host writable; a masked directory or an ancestor of one
    (``/tmp``, ``/var``, ``/run/user``) would bring its host sockets back. Raised
    before anything runs, never silently narrowed.
    """
    masked = _masked_dirs(runtime_dir, is_dir)
    # Socket directories: nothing under them is ever made visible again.
    socket_roots = [d for d in ("/run", runtime_dir) if d]
    for path in paths:
        norm = os.path.normpath(str(path))
        clashes = [d for d in masked if _under(d, norm)]
        clashes += [d for d in socket_roots if _under(norm, d) and d not in clashes]
        if norm == "/" or clashes:
            what = "the host root" if norm == "/" else f"{', '.join(clashes)} (masked)"
            raise _refusal(
                "writable-path",
                f"the directory it must make writable, {norm}, would expose {what}; "
                "run the replay from the workload's own directory",
            )


def build_argv(
    bwrap: str,
    command: Sequence[str],
    *,
    writable: Sequence[Path],
    cwd: Path,
    runtime_dir: str | None,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> list[str]:
    """The ``bwrap`` command line that runs *command* hermetically (pure)."""
    argv = [bwrap, *NAMESPACE_ARGS, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for directory in _masked_dirs(runtime_dir, is_dir):
        argv += ["--tmpfs", directory]
    # After the masks, so a writable dir under a masked /tmp is visible again.
    for path in writable:
        argv += ["--bind", str(path), str(path)]
    argv += ["--chdir", str(cwd), "--", *command]
    return argv


def sandbox_env(env: Mapping[str, str]) -> dict[str, str]:
    """*env* without the variables that name host sockets (a new dict)."""
    return {k: v for k, v in env.items() if k not in HOST_SOCKET_ENV}


@dataclass(frozen=True)
class HermeticSandbox:
    """A preflighted bwrap: wraps a command and describes the isolation."""

    bwrap: str
    version: str | None

    def wrap(self, command: Sequence[str], *, writable: Sequence[Path], cwd: Path) -> list[str]:
        return build_argv(
            self.bwrap, command, writable=writable, cwd=cwd,
            runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
        )

    def describe(self, writable: Sequence[tuple[str, Path]] = ()) -> dict[str, Any]:
        """The result's ``hermetic`` block (ADR-0322 D8, slice 1); *writable* is
        ``(role, path)`` for each directory bound read-write."""
        return {
            "enabled": True,
            "mechanism": MECHANISM,
            "bwrap_version": self.version,
            "network": "none",
            "filesystem": "read-only",
            "writable_paths": [{"role": role, "path": str(path)} for role, path in writable],
            # Counting from outside the process (strace) is not implemented:
            # not measured is null, never 0.
            "counting": "none",
            "connections_blocked": None,
            "unix_connects_outside": None,
            "writes_blocked": None,
        }


def _refusal(check: str, detail: str, stderr: str | None = None) -> HermeticUnavailableError:
    return HermeticUnavailableError(
        check,
        f"--hermetic cannot isolate this replay: {detail}. Nothing was run; the replay "
        "is never run without isolation under --hermetic (ADR-0322). Run it without "
        "--hermetic to replay un-isolated, or on a host that permits unprivileged "
        "user namespaces (an administrator decision; on Ubuntu, the distribution's "
        "AppArmor profile route).",
        stderr,
    )


def preflight(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    platform: str | None = None,
) -> HermeticSandbox:
    """Check that the sandbox can be built here, or raise
    :class:`HermeticUnavailableError` naming the failed check."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        raise _refusal("platform", f"hermetic replay needs Linux namespaces (this is {platform})")
    bwrap = which(MECHANISM)
    if bwrap is None:
        raise _refusal("bwrap-missing", "bubblewrap (`bwrap`) is not on PATH")
    probe = build_argv(
        bwrap, ["true"], writable=[], cwd=Path("/"),
        runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
    )
    try:
        proc = run(probe, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _refusal("preflight", f"the preflight `bwrap ... -- true` could not run ({exc})") \
            from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:_STDERR_CAP] or None
        raise _refusal(
            "preflight",
            f"the preflight `bwrap ... -- true` exited {proc.returncode}"
            + (f" ({stderr.splitlines()[0]})" if stderr else ""),
            stderr,
        )
    version: str | None = None
    try:
        out = run([bwrap, "--version"], capture_output=True, text=True,
                  timeout=PREFLIGHT_TIMEOUT_S)
        match = _VERSION.search(out.stdout or "")
        version = match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        version = None
    return HermeticSandbox(bwrap=bwrap, version=version)
