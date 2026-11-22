"""ADR-0322 amendment: the Landlock + seccomp backend of ``nova replay --hermetic``.

Stock Ubuntu 24.04 restricts unprivileged user namespaces, so the bubblewrap backend
cannot start there; Landlock (ABI >= 4: TCP rules) and seccomp need no privilege.
The backend is chosen by preflight -- bwrap first, then Landlock -- and never silently:
the result names the mechanism and what it does and does not enforce; when neither
can be set up the replay is refused (exit 5).

Unit tests here are pure (the ruleset, the seccomp program run through a small BPF
interpreter, the launcher argv, the backend choice). The real-kernel tests run the
launcher and are skipped, with the reason, where Landlock is unavailable. End-to-end
replays under this backend are in ``test_hermetic_replay.py``.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric.replay import _landlock_exec as ll
from novafabric.replay._hermetic import (
    EXIT_HERMETIC_UNAVAILABLE,
    HermeticSandbox,
    HermeticUnavailableError,
    LandlockSandbox,
    landlock_preflight,
    select_sandbox,
)

# ── the Landlock ruleset (pure) ──────────────────────────────────────────────

READ_EXEC = ll.FS_EXECUTE | ll.FS_READ_FILE | ll.FS_READ_DIR


def test_the_ruleset_makes_the_host_read_only_and_only_the_listed_dirs_writable() -> None:
    rs = ll.build_ruleset(8, ["/home/u/work", "/home/u/replays/R1"])
    assert rs.handled_fs == (1 << 16) - 1  # every filesystem right ABI 8 knows
    assert rs.handled_net == ll.NET_BIND_TCP | ll.NET_CONNECT_TCP
    assert rs.scoped == ll.SCOPE_ABSTRACT_UNIX_SOCKET | ll.SCOPE_SIGNAL
    rules = dict(rs.rules)
    assert rules["/"] == READ_EXEC  # read + execute, no write right of any kind
    assert rules["/home/u/work"] == rules["/home/u/replays/R1"] == rs.handled_fs
    assert rules["/dev/null"] == ll.FS_READ_FILE | ll.FS_WRITE_FILE | ll.FS_TRUNCATE
    assert set(rules) == {"/", "/home/u/work", "/home/u/replays/R1", *ll.DEV_FILES}
    # no port rule exists: every TCP bind and connect, loopback included, is denied
    assert not any(path.startswith("tcp:") for path in rules)


@pytest.mark.parametrize(
    ("abi", "ioctl", "scoped"),
    [(4, False, 0), (5, True, 0), (6, True, ll.SCOPE_ABSTRACT_UNIX_SOCKET | ll.SCOPE_SIGNAL)],
)
def test_the_handled_rights_follow_the_abi(abi: int, ioctl: bool, scoped: int) -> None:
    rs = ll.build_ruleset(abi, [])
    assert bool(rs.handled_fs & ll.FS_IOCTL_DEV) is ioctl
    assert rs.handled_fs & ll.FS_TRUNCATE and rs.handled_fs & ll.FS_REFER
    assert rs.scoped == scoped


def test_an_abi_without_tcp_rules_is_rejected() -> None:
    with pytest.raises(ValueError, match="no TCP rules"):
        ll.build_ruleset(3, [])


# ── the seccomp program, run through a BPF interpreter (pure) ────────────────


def _run_bpf(program: list[ll.Insn], nr: int, arch: int, args: tuple[int, ...] = ()) -> int:
    """Evaluate a classic-BPF seccomp program over one ``seccomp_data``."""
    full = (list(args) + [0] * 6)[:6]

    def load(off: int) -> int:
        if off == ll.OFF_NR:
            return nr
        if off == ll.OFF_ARCH:
            return arch
        i, word = divmod(off - 16, 8)
        return (full[i] >> (32 * (word // 4))) & 0xFFFFFFFF

    acc, pc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == ll.BPF_LD_W_ABS:
            acc = load(k)
        elif code == ll.BPF_ALU_AND_K:
            acc &= k
        elif code in (ll.BPF_JEQ_K, ll.BPF_JGE_K, ll.BPF_JSET_K):
            taken = {ll.BPF_JEQ_K: acc == k, ll.BPF_JGE_K: acc >= k,
                     ll.BPF_JSET_K: bool(acc & k)}[code]
            pc += jt if taken else jf
        elif code == ll.BPF_RET_K:
            return k
        else:  # pragma: no cover -- the program uses only the opcodes above
            raise AssertionError(f"opcode {code:#x}")
        pc += 1


ALLOW = ll.SECCOMP_RET_ALLOW
EACCES = ll.SECCOMP_RET_ERRNO | errno.EACCES
EPERM = ll.SECCOMP_RET_ERRNO | errno.EPERM
KILL = ll.SECCOMP_RET_KILL_PROCESS
SOCK_DGRAM, SOCK_RAW, SOCK_CLOEXEC, SOCK_NONBLOCK = 2, 3, 0o2000000, 0o4000
AF_PACKET, AF_VSOCK = 17, 40


def _socket_cases() -> list[tuple[str, tuple[int, int, int], int]]:
    return [
        ("tcp4", (ll.AF_INET, ll.SOCK_STREAM, 0), ALLOW),
        ("tcp6-flags", (ll.AF_INET6, ll.SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 6), ALLOW),
        ("udp4", (ll.AF_INET, SOCK_DGRAM, 0), EACCES),
        ("udp6", (ll.AF_INET6, SOCK_DGRAM | SOCK_CLOEXEC, 17), EACCES),
        ("icmp", (ll.AF_INET, SOCK_DGRAM, 1), EACCES),
        ("raw", (ll.AF_INET, SOCK_RAW, 255), EACCES),
        ("mptcp", (ll.AF_INET, ll.SOCK_STREAM, 262), EACCES),
        ("sctp", (ll.AF_INET6, ll.SOCK_STREAM, 132), EACCES),
        ("unix", (ll.AF_UNIX, ll.SOCK_STREAM, 0), EACCES),
        ("netlink", (ll.AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, 0), ALLOW),
        ("packet", (AF_PACKET, SOCK_RAW, 0), EACCES),
        ("vsock", (AF_VSOCK, ll.SOCK_STREAM, 0), EACCES),
        # the kernel reads an int argument's low word: garbage above it changes nothing
        ("high-bits", ((1 << 32) | ll.AF_INET, SOCK_DGRAM, 0), EACCES),
    ]


@pytest.mark.parametrize("machine", sorted(ll.ARCHES))
@pytest.mark.parametrize(("case", "args", "expected"), _socket_cases(),
                         ids=[c[0] for c in _socket_cases()])
def test_socket_creation_is_limited_to_tcp_and_netlink(
    machine: str, case: str, args: tuple[int, int, int], expected: int
) -> None:
    arch = ll.ARCHES[machine]
    assert _run_bpf(ll.seccomp_program(arch), arch.socket, arch.audit_arch, args) == expected


@pytest.mark.parametrize("machine", sorted(ll.ARCHES))
def test_listen_fastopen_and_io_uring_are_denied_and_the_rest_allowed(machine: str) -> None:
    arch = ll.ARCHES[machine]
    prog = ll.seccomp_program(arch)
    run = lambda nr, *a: _run_bpf(prog, nr, arch.audit_arch, a)  # noqa: E731
    assert run(arch.listen, 3, 16) == EACCES  # autobind would bypass Landlock's bind rule
    assert run(arch.io_uring_setup, 8, 0) == EPERM  # IORING_OP_SOCKET skips socket()
    assert run(arch.sendto, 3, 0, 1, ll.MSG_FASTOPEN) == EACCES
    assert run(arch.sendmmsg, 3, 0, 1, ll.MSG_FASTOPEN | 0x40) == EACCES
    assert run(arch.sendmsg, 3, 0, ll.MSG_FASTOPEN) == EACCES
    assert run(arch.sendto, 3, 0, 1, 0x40) == ALLOW  # MSG_DONTWAIT: an ordinary send
    assert run(arch.sendmsg, 3, 0, 0) == ALLOW
    assert run(0, 0, 0, 0) == ALLOW  # read (x86_64) / io_setup (aarch64): untouched


def test_a_syscall_of_another_abi_kills_the_process() -> None:
    x86 = ll.ARCHES["x86_64"]
    prog = ll.seccomp_program(x86)
    assert _run_bpf(prog, 359, 0x40000003, (ll.AF_INET, SOCK_DGRAM, 0)) == KILL  # i386
    assert _run_bpf(prog, ll.X32_SYSCALL_BIT | 41, x86.audit_arch,
                    (ll.AF_INET, SOCK_DGRAM, 0)) == KILL  # x32 socket()
    arm = ll.ARCHES["aarch64"]
    assert _run_bpf(ll.seccomp_program(arm), 198, 0x40000028, ()) == KILL  # arm32


# ── the launcher argv and environment (pure) ─────────────────────────────────


def test_the_launcher_argv_isolates_python_and_lists_only_the_writable_dirs() -> None:
    sandbox = LandlockSandbox(python="/venv/bin/python", launcher="/pkg/_landlock_exec.py",
                              abi=6)
    argv = sandbox.wrap(["/venv/bin/python", "agent.py"],
                        writable=[Path("/w"), Path("/r")], cwd=Path("/w"))
    assert argv == [
        "/venv/bin/python", "-I", "-S", "/pkg/_landlock_exec.py",
        "--write", "/w", "--write", "/r", "--chdir", "/w", "--", "/venv/bin/python", "agent.py",
    ]
    assert ll.parse_args(argv[4:]) == (["/w", "/r"], "/w", None, ["/venv/bin/python",
                                                                   "agent.py"])


@pytest.mark.parametrize("argv", [["--write"], ["--bogus", "--", "x"], ["--write", "/w"]],
                         ids=["dangling", "unknown", "no-command"])
def test_a_malformed_launcher_argv_is_an_error_not_an_unconfined_exec(argv: list[str]) -> None:
    with pytest.raises(ll.LaunchError):
        ll.parse_args(argv)


def test_the_environment_drops_host_sockets_and_points_tmpdir_into_the_writable_set(
    tmp_path: Path,
) -> None:
    sandbox = LandlockSandbox(python="p", launcher="l", abi=8)
    env = sandbox.environment({"PATH": "/bin", "SSH_AUTH_SOCK": "/run/a", "TMPDIR": "/tmp"},
                              tmp_path)
    assert "SSH_AUTH_SOCK" not in env
    assert env["TMPDIR"] == str(tmp_path / "tmp")
    assert (tmp_path / "tmp").is_dir()
    bwrap = HermeticSandbox(bwrap="/usr/bin/bwrap", version="0.9.0")
    assert bwrap.environment({"PATH": "/bin", "DOCKER_HOST": "x"}, tmp_path) == {"PATH": "/bin"}


def test_the_result_block_names_what_landlock_enforces_and_what_it_does_not() -> None:
    writable = [("working_directory", Path("/w")), ("replay_result_dir", Path("/r"))]
    block = LandlockSandbox(python="p", launcher="l", abi=8).describe(writable)
    assert block["mechanism"] == "landlock"
    assert block["landlock_abi"] == 8
    assert block["network"] == "none" and block["filesystem"] == "read-only"
    assert block["writable_paths"] == [{"role": "working_directory", "path": "/w"},
                                       {"role": "replay_result_dir", "path": "/r"}]
    enforced = {e["control"]: e["by"] for e in block["enforced"]}
    assert enforced["network.tcp"] == "landlock"
    assert enforced["network.udp"] == enforced["network.unix"] == "seccomp"
    assert enforced["filesystem.write"] == "landlock"
    assert enforced["processes.signal"] == "landlock"
    assert set(block["not_enforced"]) == {"filesystem.read_hidden_dirs",
                                          "processes.lifetime"}
    assert block["counting"] == "none"
    assert block["connections_blocked"] is None  # not measured is never 0
    # below ABI 6 signals to host processes are not scoped
    older = LandlockSandbox(python="p", launcher="l", abi=5).describe([])
    assert "processes.signal" in older["not_enforced"]
    assert "processes.signal" not in {e["control"] for e in older["enforced"]}


def test_the_bwrap_block_states_its_own_enforcement() -> None:
    block = HermeticSandbox(bwrap="/usr/bin/bwrap", version="0.9.0").describe([])
    enforced = {e["control"]: e["by"] for e in block["enforced"]}
    assert enforced["network.tcp"] == enforced["network.udp"] == "bwrap"
    assert block["not_enforced"] == ["network.unix_visible_paths"]


# ── the Landlock preflight and the backend choice (injected, no kernel) ──────


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _ok_run(argv: list[str], **_kw: Any) -> _Proc:
    checks = dict.fromkeys(("network.tcp", "network.udp", "filesystem.write"), "enforced")
    return _Proc(0, stdout=json.dumps({"abi": 8, "checks": checks}))


@pytest.mark.parametrize(
    ("kwargs", "check"),
    [
        ({"platform": "darwin"}, "platform"),
        ({"machine": "riscv64"}, "platform"),
        ({"probe_abi": lambda: 0}, "landlock-abi"),
        ({"probe_abi": lambda: 3}, "landlock-abi"),
        ({"run": lambda argv, **_kw: _Proc(
            1, stdout=json.dumps({"abi": 8, "checks": {"network.udp": "NOT ENFORCED: allowed"}}))},
         "preflight"),
        ({"run": lambda argv, **_kw: _Proc(125, stderr="nova-hermetic(landlock): prctl failed")},
         "preflight"),
    ],
    ids=["not-linux", "no-seccomp-table", "landlock-absent", "abi-too-old", "self-test-fails",
         "launcher-fails"],
)
def test_every_failed_landlock_precondition_refuses(kwargs: dict[str, Any], check: str) -> None:
    kwargs = {"platform": "linux", "machine": "x86_64", "probe_abi": lambda: 8,
              "run": _ok_run, **kwargs}
    with pytest.raises(HermeticUnavailableError) as info:
        landlock_preflight(**kwargs)
    error = info.value.as_error()
    assert error["check"] == check
    assert error["mechanism"] == "landlock"
    assert info.value.exit_code == EXIT_HERMETIC_UNAVAILABLE
    if check == "preflight":
        assert "stderr" in error
    for advice in ("sudo", "setuid", "--privileged"):
        assert advice not in error["message"]


def test_a_passing_landlock_preflight_runs_the_self_test_and_reports_the_abi() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> _Proc:
        seen.append(argv)
        return _ok_run(argv, **kw)

    sandbox = landlock_preflight(platform="linux", machine="aarch64", probe_abi=lambda: 7,
                                 run=run, python="/venv/bin/python")
    assert sandbox.abi == 7
    (argv,) = seen
    assert argv[:4] == ["/venv/bin/python", "-I", "-S", sandbox.launcher]
    assert argv[4] == "--self-test"
    assert Path(sandbox.launcher).name == "_landlock_exec.py"


def _bwrap_fails() -> HermeticSandbox:
    raise HermeticUnavailableError("bwrap-missing", "no bwrap", mechanism="bwrap")


def _landlock_fails() -> LandlockSandbox:
    raise HermeticUnavailableError("landlock-abi", "no landlock", mechanism="landlock")


_BWRAP = HermeticSandbox(bwrap="/usr/bin/bwrap", version="0.9.0")
_LANDLOCK = LandlockSandbox(python="p", launcher="l", abi=8)


def test_auto_prefers_bwrap_then_landlock_and_refuses_when_neither_works() -> None:
    assert select_sandbox("auto", bwrap=lambda: _BWRAP, landlock=lambda: _LANDLOCK) is _BWRAP
    assert select_sandbox("auto", bwrap=_bwrap_fails, landlock=lambda: _LANDLOCK) is _LANDLOCK
    with pytest.raises(HermeticUnavailableError) as info:
        select_sandbox("auto", bwrap=_bwrap_fails, landlock=_landlock_fails)
    error = info.value.as_error()
    assert error["check"] == "no-backend"
    assert [(a["mechanism"], a["check"]) for a in error["attempts"]] == [
        ("bwrap", "bwrap-missing"), ("landlock", "landlock-abi")]
    assert info.value.exit_code == 5


def test_an_explicit_backend_is_the_only_one_tried() -> None:
    def never() -> Any:
        raise AssertionError("the other backend was tried")

    assert select_sandbox("landlock", bwrap=never, landlock=lambda: _LANDLOCK) is _LANDLOCK
    assert select_sandbox("bwrap", bwrap=lambda: _BWRAP, landlock=never) is _BWRAP
    with pytest.raises(HermeticUnavailableError) as info:
        select_sandbox("bwrap", bwrap=_bwrap_fails, landlock=never)
    assert info.value.as_error()["check"] == "bwrap-missing"  # no fallback when named
    with pytest.raises(ValueError, match="unknown hermetic backend"):
        select_sandbox("firejail")


# ── the real kernel ──────────────────────────────────────────────────────────


def _landlock_unavailable() -> str | None:
    """Why this kernel cannot run the backend at all -- not whether the preflight
    passes: a self-test that fails where Landlock exists must fail these tests, never
    skip them."""
    if not sys.platform.startswith("linux"):
        return f"not Linux ({sys.platform})"
    if ll.current_arch() is None:
        return "no seccomp syscall table for this machine"
    abi = ll.probe_abi()
    if abi < ll.MIN_ABI:
        return f"Landlock ABI {abi} (< {ll.MIN_ABI})"
    return None


_UNAVAILABLE = _landlock_unavailable()
needs_landlock = pytest.mark.skipif(
    _UNAVAILABLE is not None, reason=f"Landlock backend unavailable here: {_UNAVAILABLE}"
)

PROBE = r'''
import errno, json, os, socket, sys
port, upath, outside, inside = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def tcp():
    socket.create_connection(("127.0.0.1", port), timeout=5).close()
def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"x", ("192.0.2.1", 53))
def unix():
    s = socket.socket(socket.AF_UNIX)
    s.connect(upath)
def write(path):
    with open(path, "w") as f:
        f.write("x")
print(json.dumps({"tcp": attempt(tcp), "udp": attempt(udp), "unix": attempt(unix),
                  "outside": attempt(lambda: write(outside)),
                  "inside": attempt(lambda: write(inside))}))
'''


class _Listener:
    """A TCP listener and a pathname Unix socket on the HOST, counting accepts."""

    def __init__(self, directory: Path) -> None:
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(16)
        self.port = self.tcp.getsockname()[1]
        self.unix_path = str(directory / "host.sock")
        self.unix = socket.socket(socket.AF_UNIX)
        self.unix.bind(self.unix_path)
        self.unix.listen(16)
        self.accepted = 0
        for sock in (self.tcp, self.unix):
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self.tcp.close()
        self.unix.close()


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[_Listener]:
    sock_dir = tmp_path / "sock"
    sock_dir.mkdir()
    lst = _Listener(sock_dir)
    yield lst
    lst.close()


def _probe(tmp_path: Path, listener: _Listener, *, confined: bool) -> dict[str, str]:
    writable = tmp_path / "writable"
    outside = tmp_path / "outside"
    writable.mkdir(exist_ok=True)
    outside.mkdir(exist_ok=True)
    tag = "c" if confined else "u"
    cmd = [sys.executable, "-I", "-c", PROBE, str(listener.port), listener.unix_path,
           str(outside / f"{tag}.txt"), str(writable / f"{tag}.txt")]
    if confined:
        sandbox = landlock_preflight()
        cmd = sandbox.wrap(cmd, writable=[writable], cwd=writable)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    result: dict[str, str] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


@needs_landlock
def test_the_kernel_denies_tcp_udp_unix_and_writes_outside_the_writable_set(
    tmp_path: Path, listener: _Listener
) -> None:
    # Control: unconfined, the same probe reaches both host sockets and writes anywhere.
    control = _probe(tmp_path, listener, confined=False)
    assert control["tcp"] == control["unix"] == control["outside"] == "ok"
    assert listener.accepted == 2
    listener.accepted = 0

    confined = _probe(tmp_path, listener, confined=True)
    assert confined == {"tcp": "EACCES", "udp": "EACCES", "unix": "EACCES",
                        "outside": "EACCES", "inside": "ok"}
    assert listener.accepted == 0  # nothing reached the host
    assert not (tmp_path / "outside" / "c.txt").exists()
    assert (tmp_path / "writable" / "c.txt").read_text() == "x"


@needs_landlock
def test_the_confinement_is_inherited_by_every_child(tmp_path: Path, listener: _Listener) -> None:
    sandbox = landlock_preflight()
    grandchild = (
        "import socket,subprocess,sys\n"
        "code='import socket\\ntry:\\n socket.create_connection((\"127.0.0.1\", %d),"
        " timeout=5)\\nexcept PermissionError: raise SystemExit(13)'\n"
        "sys.exit(subprocess.run([sys.executable,'-c',code]).returncode)\n" % listener.port
    )
    cmd = sandbox.wrap(["sh", "-c", f'exec "{sys.executable}" -I -c "$0"', grandchild],
                       writable=[tmp_path], cwd=tmp_path)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 13, proc.stderr
    assert listener.accepted == 0


@needs_landlock
def test_the_launcher_exits_125_and_runs_nothing_when_it_cannot_confine(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    sandbox = landlock_preflight()
    cmd = sandbox.wrap(["touch", str(marker)], writable=[tmp_path / "missing"], cwd=tmp_path)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == ll.EXIT_LAUNCH_FAILED
    assert "nothing was run" in proc.stderr
    assert not marker.exists()


def test_the_launcher_imports_nothing_from_novafabric() -> None:
    """It runs under ``python -I -S`` (no site-packages): stdlib only."""
    source = Path(ll.__file__).read_text()
    assert "import novafabric" not in source and "from novafabric" not in source


# ── the launcher's fail-closed paths, in-process with a fake libc ────────────
# (never the real syscalls: confining the test process could not be undone)


class _FakeLibc:
    def __init__(self, *, fail: str | None = None, abi: int = 8) -> None:
        self.fail, self.abi = fail, abi
        self.calls: list[str] = []
        self.syscall = self._syscall

    def _syscall(self, nr: Any, *args: Any) -> int:
        nr = getattr(nr, "value", nr)
        name = {ll.SYS_LANDLOCK_CREATE_RULESET: "create", ll.SYS_LANDLOCK_ADD_RULE: "add",
                ll.SYS_LANDLOCK_RESTRICT_SELF: "restrict"}[nr]
        if name == "create" and args[0] is None:
            return self.abi  # the version query
        self.calls.append(name)
        if name == self.fail:
            return -1
        return 99 if name == "create" else 0

    def prctl(self, option: int, *args: Any) -> int:
        name = {ll.PR_SET_NO_NEW_PRIVS: "no_new_privs", ll.PR_SET_SECCOMP: "seccomp",
                ll.PR_SET_PDEATHSIG: "pdeathsig"}[option]
        self.calls.append(name)
        return -1 if name == self.fail else 0


@pytest.fixture
def fake_kernel(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Fake libc, no setsid, no real close of the fake ruleset fd, no exec."""
    state: list[Any] = []
    monkeypatch.setattr(ll.os, "setsid", lambda: None)
    real_close = os.close
    monkeypatch.setattr(ll.os, "close", lambda fd: None if fd == 99 else real_close(fd))
    monkeypatch.setattr(ll.os, "execvp", lambda f, a: state.append(("exec", f, list(a))))
    monkeypatch.setattr(ll.os, "chdir", lambda d: state.append(("chdir", d)))
    monkeypatch.setattr(ll, "current_arch", lambda: ll.ARCHES["x86_64"])
    monkeypatch.setattr(ll.sys, "platform", "linux")
    yield state


@pytest.mark.parametrize("fail", ["create", "add", "restrict", "no_new_privs", "seccomp"])
def test_any_failed_confinement_step_exits_125_and_never_execs(
    fail: str, fake_kernel: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    libc = _FakeLibc(fail=fail)
    monkeypatch.setattr(ll, "_libc", lambda: libc)
    code = ll.main(["--write", str(tmp_path), "--chdir", str(tmp_path), "--", "agent"])
    assert code == ll.EXIT_LAUNCH_FAILED
    assert not [c for c in fake_kernel if c[0] == "exec"]  # nothing was run
    assert "nothing was run" in capsys.readouterr().err


def test_confinement_applies_every_step_in_order_then_execs(
    fake_kernel: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    libc = _FakeLibc()
    monkeypatch.setattr(ll, "_libc", lambda: libc)
    ll.main(["--write", str(tmp_path), "--chdir", str(tmp_path), "--", "agent", "-x"])
    # no_new_privs, the ruleset (one rule per path), restrict, then the seccomp filter
    assert libc.calls[0] == "no_new_privs"
    assert libc.calls[1] == "create"
    assert libc.calls[-2:] == ["restrict", "seccomp"]
    assert libc.calls.count("add") == 2 + sum(os.path.exists(d) for d in ll.DEV_FILES)
    assert fake_kernel == [("chdir", str(tmp_path)), ("exec", "agent", ["agent", "-x"])]


def test_an_old_or_absent_landlock_is_refused_by_the_launcher_itself(
    fake_kernel: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ll, "_libc", lambda: _FakeLibc(abi=3))
    assert ll.main(["--", "agent"]) == ll.EXIT_LAUNCH_FAILED
    monkeypatch.setattr(ll, "current_arch", lambda: None)
    assert ll.main(["--", "agent"]) == ll.EXIT_LAUNCH_FAILED
    assert not fake_kernel


def test_a_missing_writable_dir_is_an_error_not_a_narrower_sandbox(
    fake_kernel: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(ll, "_libc", lambda: _FakeLibc())
    assert ll.main(["--write", str(tmp_path / "gone"), "--", "agent"]) == ll.EXIT_LAUNCH_FAILED
    assert not fake_kernel


def test_the_self_test_reports_not_enforced_when_nothing_is_confined(tmp_path: Path) -> None:
    """Run unconfined, the preflight's self-test must fail every check: it can tell a
    kernel that does not enforce from one that does."""
    results = ll.self_test(str(tmp_path))
    for name in ("network.udp", "network.unix", "network.listen", "filesystem.write"):
        assert results[name].startswith("NOT ENFORCED"), (name, results[name])
    assert results["network.tcp"].startswith("NOT ENFORCED")  # refused, not EACCES
    assert results["dev.null"] == "enforced"
    assert (tmp_path / "probe").read_text() == "x"
