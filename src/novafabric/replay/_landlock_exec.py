"""The Landlock + seccomp launcher of ``nova replay --hermetic`` (ADR-0322 amendment).

A second hermetic backend that needs **no user namespace**: stock Ubuntu 24.04 sets
``kernel.apparmor_restrict_unprivileged_userns=1``, so the bubblewrap backend cannot
start there, while Landlock (an unprivileged Linux security module, ABI >= 4 for TCP
rules) and seccomp are available to any process.

Run as ``python -I -S _landlock_exec.py [--write DIR]... [--chdir DIR] -- CMD...``.
The launcher restricts *itself* and then ``execvp``\\ s *CMD*; Landlock domains and
seccomp filters are inherited across ``fork``/``exec`` and can never be lifted, so
the whole replayed process tree is confined. It is stdlib-only and imports nothing
from ``novafabric`` (``-I -S``: no ``PYTHONPATH``, no ``sitecustomize``, so the
replay's own site bootstrap runs in *CMD*, not here).

What it applies, in order (any failure exits 125 before *CMD* runs -- never an
unconfined exec):

1. ``setsid()`` -- no controlling terminal, so ``TIOCSTI`` cannot type into the
   operator's shell (bwrap's ``--new-session``);
2. ``PR_SET_NO_NEW_PRIVS`` -- required by both, and no setuid escape;
3. a Landlock domain: every filesystem right the ABI knows is handled; ``/`` is
   granted read + execute only, each ``--write`` directory every right, the
   ``/dev`` sink/source files read + write; TCP ``bind`` and ``connect`` are handled
   with **no** port allowed (loopback included); from ABI 6 abstract Unix sockets
   and signals are scoped to the domain;
4. a seccomp filter for what Landlock does not cover (spike, ADR text): only
   ``AF_UNIX``-free socket creation -- ``socket()`` is allowed for ``AF_NETLINK``
   and for ``AF_INET``/``AF_INET6`` *TCP* (``SOCK_STREAM``, protocol 0 or
   ``IPPROTO_TCP``) only, so UDP/DNS, ICMP, raw, SCTP, MPTCP (which Landlock's TCP
   rules miss) and pathname/abstract Unix sockets fail with ``EACCES``;
   ``listen()`` fails (an unbound TCP socket's ``listen`` autobinds past Landlock's
   bind rule); ``MSG_FASTOPEN`` sends fail; ``io_uring_setup`` fails (io_uring
   creates sockets without the ``socket`` syscall); a syscall of another ABI
   (i386, x32) kills the process. ``socketpair`` stays allowed (asyncio's self-pipe).

``--self-test DIR`` applies the same confinement and, instead of exec'ing, checks
that each control is enforced; it prints one JSON object and exits 0 only if all
are. The preflight runs it, so a kernel that accepts the syscalls but does not
enforce them is refused, not trusted.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

#: The lowest Landlock ABI with TCP rules (Linux 6.7).
MIN_ABI = 4

#: Exit status of the launcher when it cannot confine itself (nothing ran).
EXIT_LAUNCH_FAILED = 125

STDERR_PREFIX = "nova-hermetic(landlock): "

# ── Landlock UAPI (include/uapi/linux/landlock.h) ────────────────────────────

# The landlock_* syscalls are numbered from the generic table on every arch.
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5

NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1  # ABI 6

#: Rights a rule on a non-directory may carry (anything else is EINVAL).
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

#: The device files every program expects to open read-write (bwrap's ``--dev``).
DEV_FILES: tuple[str, ...] = ("/dev/null", "/dev/zero", "/dev/full", "/dev/random",
                              "/dev/urandom")


def fs_rights(abi: int) -> int:
    """Every filesystem right Landlock ABI *abi* knows (all are handled)."""
    rights = (1 << 13) - 1  # ABI 1: EXECUTE .. MAKE_SYM
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


@dataclass(frozen=True)
class Ruleset:
    """A Landlock domain to apply (pure data; :func:`build_ruleset` builds it)."""

    abi: int
    handled_fs: int
    handled_net: int
    scoped: int
    #: ``(path, allowed_access)`` in the order the rules are added.
    rules: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def build_ruleset(
    abi: int, writable: Sequence[str], *, dev_files: Sequence[str] = DEV_FILES
) -> Ruleset:
    """The domain for *abi*: ``/`` read + execute, *writable* everything, the
    device files read + write, TCP bind/connect handled with no port allowed."""
    if abi < MIN_ABI:
        raise ValueError(f"Landlock ABI {abi} has no TCP rules (need >= {MIN_ABI})")
    handled = fs_rights(abi)
    read_exec = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
    dev = (FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE) & handled
    rules: list[tuple[str, int]] = [("/", read_exec)]
    rules += [(str(path), handled) for path in writable]
    rules += [(path, dev) for path in dev_files]
    scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0
    return Ruleset(abi=abi, handled_fs=handled, handled_net=NET_BIND_TCP | NET_CONNECT_TCP,
                   scoped=scoped, rules=tuple(rules))


# ── seccomp (include/uapi/linux/{seccomp,filter,bpf_common,audit}.h) ─────────

BPF_LD_W_ABS = 0x20
BPF_ALU_AND_K = 0x54
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_JSET_K = 0x45
BPF_RET_K = 0x06

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000

#: ``struct seccomp_data`` offsets: nr, arch, then u64 args from 16 (low word first:
#: x86_64 and aarch64 are little-endian; the kernel reads int arguments' low word).
OFF_NR, OFF_ARCH = 0, 4


def _arg(i: int) -> int:
    return 16 + 8 * i


AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
SOCK_STREAM, SOCK_TYPE_MASK = 1, 0xF
IPPROTO_TCP = 6
MSG_FASTOPEN = 0x20000000
X32_SYSCALL_BIT = 0x40000000


@dataclass(frozen=True)
class Arch:
    """Syscall numbers the filter names, for one architecture."""

    machine: str
    audit_arch: int
    socket: int
    listen: int
    sendto: int
    sendmsg: int
    sendmmsg: int
    io_uring_setup: int
    has_x32: bool


ARCHES: dict[str, Arch] = {
    "x86_64": Arch("x86_64", 0xC000003E, socket=41, listen=50, sendto=44, sendmsg=46,
                   sendmmsg=307, io_uring_setup=425, has_x32=True),
    "aarch64": Arch("aarch64", 0xC00000B7, socket=198, listen=201, sendto=206, sendmsg=211,
                    sendmmsg=269, io_uring_setup=425, has_x32=False),
}

#: One BPF instruction: ``(code, jt, jf, k)``.
Insn = tuple[int, int, int, int]


def _assemble(program: Sequence[tuple[object, ...]]) -> list[Insn]:
    """Resolve ``("label", name)`` markers and symbolic jump targets to offsets."""
    labels: dict[str, int] = {}
    pc = 0
    for item in program:
        if item[0] == "label":
            labels[str(item[1])] = pc
        else:
            pc += 1
    out: list[Insn] = []
    for item in program:
        if item[0] == "label":
            continue
        code, jt, jf, k = item
        pos = len(out)

        def rel(target: object, pos: int = pos) -> int:
            if isinstance(target, int):
                return target
            off = labels[str(target)] - pos - 1
            if not 0 <= off <= 255:
                raise ValueError(f"jump to {target} out of range ({off})")
            return off

        out.append((int(code), rel(jt), rel(jf), int(k)))  # type: ignore[call-overload]
    return out


def seccomp_program(arch: Arch) -> list[Insn]:
    """The filter (pure): see the module docstring for what it denies."""
    eacces = SECCOMP_RET_ERRNO | errno.EACCES
    eperm = SECCOMP_RET_ERRNO | errno.EPERM
    p: list[tuple[object, ...]] = [
        (BPF_LD_W_ABS, 0, 0, OFF_ARCH),
        (BPF_JEQ_K, 0, "kill", arch.audit_arch),
        (BPF_LD_W_ABS, 0, 0, OFF_NR),
    ]
    if arch.has_x32:
        p.append((BPF_JGE_K, "kill", 0, X32_SYSCALL_BIT))
    p += [
        (BPF_JEQ_K, "socket", 0, arch.socket),
        (BPF_JEQ_K, "eacces", 0, arch.listen),
        (BPF_JEQ_K, "eperm", 0, arch.io_uring_setup),
        (BPF_JEQ_K, "flags3", 0, arch.sendto),
        (BPF_JEQ_K, "flags3", 0, arch.sendmmsg),
        (BPF_JEQ_K, "flags2", 0, arch.sendmsg),
        (BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        ("label", "flags3"),
        (BPF_LD_W_ABS, 0, 0, _arg(3)),
        (BPF_JSET_K, "eacces", "allow", MSG_FASTOPEN),
        ("label", "flags2"),
        (BPF_LD_W_ABS, 0, 0, _arg(2)),
        (BPF_JSET_K, "eacces", "allow", MSG_FASTOPEN),
        ("label", "socket"),
        (BPF_LD_W_ABS, 0, 0, _arg(0)),
        (BPF_JEQ_K, "allow", 0, AF_NETLINK),
        (BPF_JEQ_K, "inet", 0, AF_INET),
        (BPF_JEQ_K, "inet", "eacces", AF_INET6),  # AF_UNIX and every other family
        ("label", "inet"),
        (BPF_LD_W_ABS, 0, 0, _arg(1)),
        (BPF_ALU_AND_K, 0, 0, SOCK_TYPE_MASK),
        (BPF_JEQ_K, 0, "eacces", SOCK_STREAM),
        (BPF_LD_W_ABS, 0, 0, _arg(2)),
        (BPF_JEQ_K, "allow", 0, 0),
        (BPF_JEQ_K, "allow", "eacces", IPPROTO_TCP),
        ("label", "allow"),
        (BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        ("label", "eacces"),
        (BPF_RET_K, 0, 0, eacces),
        ("label", "eperm"),
        (BPF_RET_K, 0, 0, eperm),
        ("label", "kill"),
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
    ]
    return _assemble(p)


# ── applying it (Linux only; nothing here runs at import) ────────────────────


class LaunchError(Exception):
    """The launcher could not confine itself; *CMD* is not run."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_MODE_FILTER = 2


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def probe_abi() -> int:
    """The running kernel's Landlock ABI; 0 if Landlock is absent or disabled."""
    if not sys.platform.startswith("linux"):
        return 0
    libc = _libc()
    abi = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0),
                       ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    return max(int(abi), 0)


def current_arch() -> Arch | None:
    return ARCHES.get(platform.machine())


def _fail(libc_call: str) -> LaunchError:
    err = ctypes.get_errno()
    return LaunchError(f"{libc_call} failed: {errno.errorcode.get(err, err)} ({os.strerror(err)})")


def apply_landlock(ruleset: Ruleset) -> None:
    libc = _libc()
    attr = _RulesetAttr(ruleset.handled_fs, ruleset.handled_net, ruleset.scoped)
    fd = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
                      ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd < 0:
        raise _fail("landlock_create_ruleset")
    try:
        for path, access in ruleset.rules:
            try:
                parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                if path in DEV_FILES:
                    continue  # an absent device file needs no rule
                raise LaunchError(f"writable path {path} does not exist") from None
            try:
                allowed = access if os.path.isdir(path) else access & FILE_RIGHTS
                rule = _PathBeneath(allowed, parent)
                if libc.syscall(ctypes.c_long(SYS_LANDLOCK_ADD_RULE), ctypes.c_int(fd),
                                ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule),
                                ctypes.c_uint32(0)) != 0:
                    raise _fail(f"landlock_add_rule({path})")
            finally:
                os.close(parent)
        if libc.syscall(ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(fd),
                        ctypes.c_uint32(0)) != 0:
            raise _fail("landlock_restrict_self")
    finally:
        os.close(fd)


def apply_seccomp(program: Sequence[Insn]) -> None:
    libc = _libc()
    insns = (_SockFilter * len(program))(*[_SockFilter(*i) for i in program])
    prog = _SockFprog(len(program), insns)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise _fail("prctl(PR_SET_SECCOMP)")


PR_SET_PDEATHSIG = 1


def _new_session() -> None:
    """``setsid()``; a process-group leader (started from an interactive shell)
    cannot, so it forks: the parent only waits and relays the exit status, the
    child -- killed if the parent dies -- carries on into the new session."""
    try:
        os.setsid()
        return
    except PermissionError:
        pass
    pid = os.fork()
    if pid == 0:
        _libc().prctl(PR_SET_PDEATHSIG, 9, 0, 0, 0)
        try:
            os.setsid()
        except OSError as exc:
            sys.stderr.write(f"{STDERR_PREFIX}setsid failed: {exc}; nothing was run\n")
            os._exit(EXIT_LAUNCH_FAILED)
        return
    _, status = os.waitpid(pid, 0)
    os._exit(os.waitstatus_to_exitcode(status) if not os.WIFSIGNALED(status)
             else 128 + os.WTERMSIG(status))


def confine(writable: Sequence[str]) -> int:
    """Confine the calling (single-threaded) process; return the ABI applied."""
    arch = current_arch()
    if arch is None:
        raise LaunchError(f"no seccomp syscall table for {platform.machine()}")
    abi = probe_abi()
    if abi < MIN_ABI:
        raise LaunchError(f"Landlock ABI {abi} (need >= {MIN_ABI}: TCP rules)")
    _new_session()
    if _libc().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise _fail("prctl(PR_SET_NO_NEW_PRIVS)")
    apply_landlock(build_ruleset(abi, writable))
    apply_seccomp(seccomp_program(arch))
    return abi


# ── the self-test (the preflight's proof that the kernel enforces it) ────────


def _expect_denied(action: object) -> str:
    try:
        action()  # type: ignore[operator]
    except PermissionError:
        return "enforced"
    except OSError as exc:
        return f"NOT ENFORCED: {errno.errorcode.get(exc.errno or 0, exc.errno)}"
    return "NOT ENFORCED: allowed"


def self_test(probe_dir: str) -> dict[str, str]:
    """After :func:`confine`: is every control actually enforced?"""

    def tcp_connect() -> None:
        with socket.socket() as s:  # port 9 (discard): refused, not EACCES, if unconfined
            s.settimeout(2)
            s.connect(("127.0.0.1", 9))

    def udp() -> None:
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close()

    def unix() -> None:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()

    def mptcp() -> None:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262).close()

    def listen() -> None:
        with socket.socket() as s:
            s.listen(1)

    def write() -> None:
        with open(os.path.join(probe_dir, "probe"), "w") as f:
            f.write("x")

    checks = {"network.tcp": tcp_connect, "network.udp": udp, "network.unix": unix,
              "network.mptcp": mptcp, "network.listen": listen, "filesystem.write": write}
    out = {name: _expect_denied(fn) for name, fn in checks.items()}
    try:
        with open("/dev/null", "w") as f:
            f.write("x")
        out["dev.null"] = "enforced"  # writable as it must be
    except OSError as exc:
        out["dev.null"] = f"NOT ENFORCED: /dev/null unwritable ({exc})"
    return out


def parse_args(argv: Sequence[str]) -> tuple[list[str], str | None, str | None, list[str]]:
    """``(writable, chdir, self_test_dir, command)`` from the launcher's argv."""
    writable: list[str] = []
    chdir: str | None = None
    probe: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return writable, chdir, probe, list(argv[i + 1:])
        if arg in ("--write", "--chdir", "--self-test") and i + 1 < len(argv):
            value = argv[i + 1]
            if arg == "--write":
                writable.append(value)
            elif arg == "--chdir":
                chdir = value
            else:
                probe = value
            i += 2
            continue
        raise LaunchError(f"unexpected argument {arg!r}")
    if probe is not None:
        return writable, chdir, probe, []
    raise LaunchError("missing `-- COMMAND`")


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        writable, chdir, probe, command = parse_args(args)
        if probe is None and not command:
            raise LaunchError("empty command")
        abi = confine(writable)
        if probe is not None:
            results = self_test(probe)
            print(json.dumps({"abi": abi, "checks": results}))
            return 0 if all(v == "enforced" for v in results.values()) else 1
        if chdir is not None:
            os.chdir(chdir)
        os.execvp(command[0], command)
    except (LaunchError, OSError) as exc:
        sys.stderr.write(f"{STDERR_PREFIX}{exc}; nothing was run\n")
        return EXIT_LAUNCH_FAILED
    return EXIT_LAUNCH_FAILED  # pragma: no cover -- execvp does not return


if __name__ == "__main__":
    sys.exit(main())
