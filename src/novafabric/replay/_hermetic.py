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

A second backend, **Landlock + seccomp** (``_landlock_exec.py``, ADR-0322
amendment), needs no user namespace -- stock Ubuntu 24.04 restricts those
(``kernel.apparmor_restrict_unprivileged_userns=1``), so bwrap cannot start there.
``--hermetic`` (``--hermetic-backend auto``) tries bwrap's preflight, then
Landlock's; the result's ``hermetic.mechanism`` names the one used and
``enforced`` / ``not_enforced`` list what it does and does not guarantee -- the two
differ (Landlock denies TCP to loopback too and cannot hide ``/tmp`` or ``/run``
from reads), so the choice is reported, never silent. ``--hermetic-backend
bwrap|landlock`` names one; it alone is tried. Neither available: refused
(``check: no-backend``, with every attempt).

Not implemented (ADR-0322 slices to come): counting blocked attempts from outside
the process with strace (``counting: "none"``; the counts are ``null``, never 0) and
a copy-on-write overlay of the working directory (writes there reach the host).
Under bwrap a Unix socket under a path that stays visible (for example in ``$HOME``
or the working directory) remains connectable.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novafabric.replay import _landlock_exec

#: Exit status of ``nova replay --hermetic`` when the sandbox cannot be set up
#: (1-4 and 124 are taken, ``cli/replay.py``).
EXIT_HERMETIC_UNAVAILABLE = 5

MECHANISM = "bwrap"
LANDLOCK = "landlock"

#: ``--hermetic-backend`` values: ``auto`` tries bwrap, then Landlock.
BACKENDS: tuple[str, ...] = ("auto", MECHANISM, LANDLOCK)

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

    def __init__(
        self,
        check: str,
        message: str,
        stderr: str | None = None,
        *,
        mechanism: str | None = None,
        attempts: Sequence[HermeticUnavailableError] = (),
    ) -> None:
        super().__init__(message)
        self.check = check
        self.stderr = stderr
        self.mechanism = mechanism
        self.attempts = tuple(attempts)

    def as_error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.error_type, "message": str(self), "check": self.check}
        if self.mechanism:
            out["mechanism"] = self.mechanism
        if self.stderr:
            out["stderr"] = self.stderr
        if self.attempts:
            out["attempts"] = [
                {"mechanism": a.mechanism, "check": a.check, "message": str(a)}
                for a in self.attempts
            ]
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

    @property
    def mechanism(self) -> str:
        return MECHANISM

    def environment(self, env: Mapping[str, str], tmp_dir: Path) -> dict[str, str]:
        """The replayed process's environment (``/tmp`` is an empty writable tmpfs)."""
        del tmp_dir
        return sandbox_env(env)

    def label(self) -> str:
        return f"bwrap {self.version or '(version unknown)'}"

    def describe(self, writable: Sequence[tuple[str, Path]] = ()) -> dict[str, Any]:
        """The result's ``hermetic`` block (ADR-0322 D8, slice 1); *writable* is
        ``(role, path)`` for each directory bound read-write."""
        return _block(
            MECHANISM, {"bwrap_version": self.version}, writable,
            enforced=[(control, MECHANISM) for control in BWRAP_ENFORCED],
            not_enforced=list(BWRAP_NOT_ENFORCED),
        )


#: What each backend enforces, as stable control names (the result's
#: ``enforced`` / ``not_enforced``; docs/architecture/replay-modes.md explains each).
BWRAP_ENFORCED: tuple[str, ...] = (
    "network.tcp", "network.udp", "network.other_inet", "network.unix_abstract",
    "filesystem.write", "filesystem.read_hidden_dirs", "processes.signal",
    "processes.ptrace", "processes.lifetime",
)
#: A Unix socket under a path that stays visible ($HOME, the working directory).
BWRAP_NOT_ENFORCED: tuple[str, ...] = ("network.unix_visible_paths",)

#: Landlock + seccomp, by the mechanism that enforces each (ABI >= 6 adds signals).
LANDLOCK_ENFORCED: tuple[tuple[str, str], ...] = (
    ("network.tcp", LANDLOCK),  # bind and connect, every address, loopback included
    ("network.udp", "seccomp"),
    ("network.other_inet", "seccomp"),  # raw, ICMP, SCTP, MPTCP; listen(); TFO; io_uring
    ("network.unix", "seccomp"),  # socket(AF_UNIX): pathname and abstract
    ("filesystem.write", LANDLOCK),
    ("processes.ptrace", LANDLOCK),
)
#: Host /tmp, /run, /var/tmp stay readable (Landlock only allows; it cannot mask);
#: an orphaned grandchild may outlive the replay (no pid namespace) -- still confined.
LANDLOCK_NOT_ENFORCED: tuple[str, ...] = ("filesystem.read_hidden_dirs", "processes.lifetime")


def _block(
    mechanism: str,
    extra: Mapping[str, Any],
    writable: Sequence[tuple[str, Path]],
    *,
    enforced: Sequence[tuple[str, str]],
    not_enforced: Sequence[str],
) -> dict[str, Any]:
    return {
        "enabled": True,
        "mechanism": mechanism,
        **extra,
        "network": "none",
        "filesystem": "read-only",
        "writable_paths": [{"role": role, "path": str(path)} for role, path in writable],
        "enforced": [{"control": control, "by": by} for control, by in enforced],
        "not_enforced": list(not_enforced),
        # Counting from outside the process (strace) is not implemented:
        # not measured is null, never 0.
        "counting": "none",
        "connections_blocked": None,
        "unix_connects_outside": None,
        "writes_blocked": None,
    }


@dataclass(frozen=True)
class LandlockSandbox:
    """A preflighted Landlock + seccomp launcher (no user namespace needed)."""

    python: str
    launcher: str
    abi: int

    @property
    def mechanism(self) -> str:
        return LANDLOCK

    def wrap(self, command: Sequence[str], *, writable: Sequence[Path], cwd: Path) -> list[str]:
        """``python -I -S _landlock_exec.py --write D... --chdir CWD -- CMD`` (pure)."""
        argv = [self.python, "-I", "-S", self.launcher]
        for path in writable:
            argv += ["--write", str(path)]
        return [*argv, "--chdir", str(cwd), "--", *command]

    def environment(self, env: Mapping[str, str], tmp_dir: Path) -> dict[str, str]:
        """Host socket variables dropped; ``TMPDIR`` inside the writable temp dir
        (Landlock cannot give the tree a private writable ``/tmp``)."""
        out = sandbox_env(env)
        tmp = tmp_dir / "tmp"
        tmp.mkdir(exist_ok=True)
        out["TMPDIR"] = str(tmp)
        return out

    def label(self) -> str:
        return f"landlock ABI {self.abi} + seccomp"

    def describe(self, writable: Sequence[tuple[str, Path]] = ()) -> dict[str, Any]:
        enforced = list(LANDLOCK_ENFORCED)
        not_enforced = list(LANDLOCK_NOT_ENFORCED)
        if self.abi >= 6:  # LANDLOCK_SCOPE_SIGNAL
            enforced.append(("processes.signal", LANDLOCK))
        else:
            not_enforced.append("processes.signal")
        return _block(LANDLOCK, {"landlock_abi": self.abi}, writable,
                      enforced=enforced, not_enforced=not_enforced)


#: Either backend: ``wrap``, ``environment``, ``describe``, ``label``, ``mechanism``.
Sandbox = HermeticSandbox | LandlockSandbox


def _refusal(
    check: str, detail: str, stderr: str | None = None, *, mechanism: str | None = None
) -> HermeticUnavailableError:
    return HermeticUnavailableError(
        check,
        f"--hermetic cannot isolate this replay: {detail}. Nothing was run; the replay "
        "is never run without isolation under --hermetic (ADR-0322). Run it without "
        "--hermetic to replay un-isolated, or on a host that permits unprivileged "
        "user namespaces (an administrator decision; on Ubuntu, the distribution's "
        "AppArmor profile route) or has Landlock ABI >= 4 (Linux >= 6.7).",
        stderr,
        mechanism=mechanism,
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
        raise _refusal("platform", f"hermetic replay needs Linux namespaces (this is {platform})",
                       mechanism=MECHANISM)
    bwrap = which(MECHANISM)
    if bwrap is None:
        raise _refusal("bwrap-missing", "bubblewrap (`bwrap`) is not on PATH",
                       mechanism=MECHANISM)
    probe = build_argv(
        bwrap, ["true"], writable=[], cwd=Path("/"),
        runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
    )
    try:
        proc = run(probe, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _refusal("preflight", f"the preflight `bwrap ... -- true` could not run ({exc})",
                       mechanism=MECHANISM) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:_STDERR_CAP] or None
        raise _refusal(
            "preflight",
            f"the preflight `bwrap ... -- true` exited {proc.returncode}"
            + (f" ({stderr.splitlines()[0]})" if stderr else ""),
            stderr,
            mechanism=MECHANISM,
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


def landlock_preflight(
    *,
    probe_abi: Callable[[], int] | None = None,
    run: Callable[..., Any] = subprocess.run,
    platform: str | None = None,
    machine: str | None = None,
    python: str | None = None,
) -> LandlockSandbox:
    """Check that the Landlock backend can confine *and enforces* here, or raise
    :class:`HermeticUnavailableError` (``platform`` / ``landlock-abi`` / ``preflight``).

    The preflight runs the launcher's ``--self-test``: the same confinement, then a
    TCP connect, UDP and Unix socket creation, ``listen()`` and a write outside the
    writable set must each be denied -- a kernel that accepts the syscalls without
    enforcing them is refused, not trusted.
    """
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        raise _refusal("platform", f"Landlock is a Linux security module (this is {platform})",
                       mechanism=LANDLOCK)
    machine = _platform.machine() if machine is None else machine
    if machine not in _landlock_exec.ARCHES:
        raise _refusal("platform", f"no seccomp syscall table for {machine} "
                       f"(supported: {', '.join(sorted(_landlock_exec.ARCHES))})",
                       mechanism=LANDLOCK)
    abi = (probe_abi or _landlock_exec.probe_abi)()
    if abi < _landlock_exec.MIN_ABI:
        found = "Landlock is not enabled in this kernel" if abi <= 0 else f"Landlock ABI {abi}"
        raise _refusal(
            "landlock-abi",
            f"{found}; TCP rules need ABI >= {_landlock_exec.MIN_ABI} (Linux >= 6.7, "
            "with landlock in the lsm= list)",
            mechanism=LANDLOCK,
        )
    python = python or sys.executable
    launcher = str(Path(_landlock_exec.__file__).resolve())
    with tempfile.TemporaryDirectory(prefix="nf_landlock_probe_") as probe:
        argv = [python, "-I", "-S", launcher, "--self-test", probe]
        try:
            proc = run(argv, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _refusal("preflight", f"the Landlock self-test could not run ({exc})",
                           mechanism=LANDLOCK) from exc
    failed = _self_test_failures(proc.stdout or "")
    if proc.returncode != 0 or failed is None or failed:
        detail = ((proc.stderr or "").strip() + "\n" + (proc.stdout or "").strip()).strip()
        stderr = detail[:_STDERR_CAP] or f"exit {proc.returncode}"
        what = ", ".join(failed) if failed else stderr.splitlines()[0]
        raise _refusal(
            "preflight",
            f"the Landlock self-test exited {proc.returncode} ({what})",
            stderr,
            mechanism=LANDLOCK,
        )
    return LandlockSandbox(python=python, launcher=launcher, abi=abi)


def _self_test_failures(stdout: str) -> list[str] | None:
    """The controls the self-test found NOT enforced; ``None`` if it said nothing."""
    try:
        report = json.loads(stdout.strip().splitlines()[-1])
        checks = report["checks"]
    except (IndexError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(checks, dict) or not checks:
        return None
    return [f"{name}: {state}" for name, state in checks.items() if state != "enforced"]


def select_sandbox(
    backend: str = "auto",
    *,
    bwrap: Callable[[], HermeticSandbox] | None = None,
    landlock: Callable[[], LandlockSandbox] | None = None,
) -> Sandbox:
    """The sandbox ``--hermetic-backend`` asks for: ``auto`` tries bwrap, then
    Landlock, and refuses (``check: no-backend``, every attempt listed) if neither
    works; a named backend is the only one tried. Never a silent downgrade: the
    result names the mechanism and its ``enforced`` / ``not_enforced`` controls."""
    bwrap = bwrap or preflight
    landlock = landlock or landlock_preflight
    if backend == MECHANISM:
        return bwrap()
    if backend == LANDLOCK:
        return landlock()
    if backend != "auto":
        raise ValueError(f"unknown hermetic backend {backend!r} (one of {', '.join(BACKENDS)})")
    attempts: list[HermeticUnavailableError] = []
    for attempt in (bwrap, landlock):
        try:
            return attempt()
        except HermeticUnavailableError as exc:
            attempts.append(exc)
    reasons = "; ".join(f"{a.mechanism}: {a.check}" for a in attempts)
    raise HermeticUnavailableError(
        "no-backend",
        f"--hermetic cannot isolate this replay: no sandbox backend is available here "
        f"({reasons}). Nothing was run; the replay is never run without isolation under "
        "--hermetic (ADR-0322). bwrap needs unprivileged user namespaces; Landlock needs "
        "Linux >= 6.7 with landlock enabled. Run it without --hermetic to replay "
        "un-isolated.",
        attempts=attempts,
    )
