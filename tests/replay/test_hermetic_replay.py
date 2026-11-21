"""ADR-0322 (#55): opt-in hermetic mocked replay -- ``nova replay --hermetic``.

The replayed process tree runs under bubblewrap in a private network namespace (only
its own loopback), with a read-only host filesystem and host socket directories
masked. Served model/tool calls keep working: they are answered in-process, with no
socket. When the sandbox cannot be set up the replay is refused (exit 5,
``error.type: HermeticUnavailable``) -- it never runs un-isolated under a hermetic
label.

The oracle for "nothing left the sandbox" is a listener on the host's 127.0.0.1 in the
test process: the same workload replayed without ``--hermetic`` reaches it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay import _hermetic
from novafabric.replay import _landlock_exec as ll
from novafabric.replay._hermetic import (
    EXIT_HERMETIC_UNAVAILABLE,
    HOST_SOCKET_ENV,
    HermeticUnavailableError,
    build_argv,
    check_writable,
    preflight,
    sandbox_env,
)

REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = [
    json.loads((REPO / d / "replay-result.schema.json").read_text())
    for d in ("schemas", "src/novafabric/schemas")
]

runner = CliRunner()


def _available() -> str | None:
    try:
        preflight()
    except HermeticUnavailableError as exc:
        return str(exc)
    return None


_UNAVAILABLE = _available()
needs_sandbox = pytest.mark.skipif(
    _UNAVAILABLE is not None, reason=f"hermetic sandbox unavailable here: {_UNAVAILABLE}"
)


def _landlock_available() -> str | None:
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


_LANDLOCK_UNAVAILABLE = _landlock_available()
needs_landlock = pytest.mark.skipif(
    _LANDLOCK_UNAVAILABLE is not None,
    reason=f"Landlock backend unavailable here: {_LANDLOCK_UNAVAILABLE}",
)
LANDLOCK = ("--hermetic", "--hermetic-backend", "landlock")


# ── the argv, the environment, the preflight (no namespaces) ─────────────────


def _pairs(argv: list[str]) -> list[tuple[str, ...]]:
    """``--flag a b`` groups up to the ``--`` separator, for order assertions."""
    head = argv[1:argv.index("--")]
    groups: list[tuple[str, ...]] = []
    for arg in head:
        if arg.startswith("--"):
            groups.append((arg,))
        else:
            groups[-1] = (*groups[-1], arg)
    return groups


def test_the_argv_isolates_network_masks_sockets_and_binds_only_the_writable_dirs() -> None:
    argv = build_argv(
        "/usr/bin/bwrap", ["/venv/bin/python", "agent.py"],
        writable=[Path("/tmp/work"), Path("/home/u/replays/R1"), Path("/tmp/nf_replay_x")],
        cwd=Path("/tmp/work"),
        runtime_dir="/run/user/1000", is_dir=lambda _p: True,
    )
    assert argv[0] == "/usr/bin/bwrap"
    assert argv[argv.index("--") + 1:] == ["/venv/bin/python", "agent.py"]
    groups = _pairs(argv)
    for flag in ("--unshare-net", "--unshare-pid", "--unshare-user", "--disable-userns",
                 "--die-with-parent", "--new-session"):
        assert (flag,) in groups
    order = [g for g in groups if g[0] in ("--ro-bind", "--tmpfs", "--bind", "--chdir")]
    assert order == [
        ("--ro-bind", "/", "/"),
        ("--tmpfs", "/run"), ("--tmpfs", "/tmp"), ("--tmpfs", "/var/tmp"),
        # after the masks: the work dir, the result dir and the temp dir, read-write
        ("--bind", "/tmp/work", "/tmp/work"),
        ("--bind", "/home/u/replays/R1", "/home/u/replays/R1"),
        ("--bind", "/tmp/nf_replay_x", "/tmp/nf_replay_x"),
        ("--chdir", "/tmp/work"),
    ]


def test_a_runtime_dir_outside_run_is_masked_and_nothing_unlisted_is_writable() -> None:
    argv = build_argv(
        "bwrap", ["x"], writable=[], cwd=Path("/home/u/project"),
        runtime_dir="/xdg/runtime", is_dir=lambda p: p != "/var/tmp",
    )
    groups = _pairs(argv)
    assert ("--tmpfs", "/xdg/runtime") in groups
    assert ("--tmpfs", "/var/tmp") not in groups  # absent on this host
    assert not any(g[0] == "--bind" for g in groups)
    assert ("--chdir", "/home/u/project") in groups


@pytest.mark.parametrize(
    "path", ["/", "/tmp", "/run/user", "/var", "/tmp/w/../.."],
    ids=["root", "masked-dir", "masked-parent", "ancestor-of-masked", "dotdot-to-root"],
)
def test_a_writable_dir_that_would_unmask_the_host_is_refused(path: str) -> None:
    """Binding ``/`` or ``/var`` read-write would undo the read-only host or the socket
    masks (``/var`` holds ``/var/tmp``): refused, never silently narrowed."""
    with pytest.raises(HermeticUnavailableError) as info:
        check_writable([Path("/home/u/project"), Path(path)], runtime_dir="/run/user/1000",
                       is_dir=lambda _p: True)
    assert info.value.as_error()["check"] == "writable-path"
    assert info.value.exit_code == 5


def test_ordinary_writable_dirs_pass() -> None:
    check_writable(
        [Path("/home/u/project"), Path("/tmp/pytest-1/work"), Path("/home/u/replays/R1")],
        runtime_dir="/run/user/1000", is_dir=lambda _p: True,
    )


def test_host_socket_variables_are_not_passed_in() -> None:
    env = {"PATH": "/bin", **dict.fromkeys(HOST_SOCKET_ENV, "/run/x.sock")}
    inside = sandbox_env(env)
    assert inside == {"PATH": "/bin"}
    assert set(HOST_SOCKET_ENV) <= set(env)  # the caller's mapping is untouched
    assert {"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "DOCKER_HOST"} <= set(HOST_SOCKET_ENV)


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.mark.parametrize(
    ("kwargs", "check"),
    [
        ({"platform": "darwin"}, "platform"),
        ({"which": lambda _name: None}, "bwrap-missing"),
        ({"which": lambda _name: "/usr/bin/bwrap",
          "run": lambda argv, **_kw: _Proc(1, stderr="bwrap: No permissions to create new "
                                                     "namespace")}, "preflight"),
    ],
    ids=["not-linux", "no-bwrap", "namespaces-denied"],
)
def test_every_failed_precondition_refuses(kwargs: dict[str, Any], check: str) -> None:
    kwargs.setdefault("platform", "linux")
    with pytest.raises(HermeticUnavailableError) as info:
        preflight(**kwargs)
    error = info.value.as_error()
    assert error["type"] == "HermeticUnavailable"
    assert error["check"] == check
    assert info.value.exit_code == EXIT_HERMETIC_UNAVAILABLE == 5
    if check == "preflight":
        assert "No permissions to create new namespace" in error["stderr"]
    # never a privilege workaround
    for advice in ("sudo", "setuid", "--privileged"):
        assert advice not in error["message"]


def test_a_passing_preflight_reports_the_bwrap_version() -> None:
    def run(argv: list[str], **_kw: Any) -> _Proc:
        return _Proc(0, stdout="bubblewrap 0.9.0\n" if "--version" in argv else "")

    sandbox = preflight(which=lambda _n: "/usr/bin/bwrap", run=run, platform="linux")
    writable = [("working_directory", Path("/w")), ("replay_result_dir", Path("/r"))]
    assert sandbox.describe(writable) == {
        "enabled": True, "mechanism": "bwrap", "bwrap_version": "0.9.0", "network": "none",
        "filesystem": "read-only",
        "writable_paths": [{"role": "working_directory", "path": "/w"},
                           {"role": "replay_result_dir", "path": "/r"}],
        "enforced": [{"control": c, "by": "bwrap"} for c in (
            "network.tcp", "network.udp", "network.other_inet", "network.unix_abstract",
            "filesystem.write", "filesystem.read_hidden_dirs", "processes.signal",
            "processes.ptrace", "processes.lifetime")],
        "not_enforced": ["network.unix_visible_paths"],
        "counting": "none", "connections_blocked": None,
        "unix_connects_outside": None, "writes_blocked": None,
    }


# ── end to end ───────────────────────────────────────────────────────────────

AGENT = r'''
import errno, json, os, socket, subprocess, sys
PORT = int(os.environ["LISTENER_PORT"])
STEPS = os.environ["STEPS"].split(",")
out = {}

def connect():
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=5).close()
        return "connected"
    except OSError as e:
        return type(e).__name__

if "openai" in STEPS:
    import openai
    client = openai.OpenAI(api_key="sk-test-not-a-key",
                           base_url=os.environ["MODEL_URL"], max_retries=0)
    r = client.chat.completions.create(model="gpt-test",
                                       messages=[{"role": "user", "content": "hi"}])
    out["openai"] = r.choices[0].message.content
if "root" in STEPS:
    out["root"] = connect()
if "child" in STEPS:
    code = ("import socket,sys\n"
            "try:\n socket.create_connection(('127.0.0.1', %d), timeout=5).close()\n"
            "except OSError: sys.exit(3)\n" % PORT)
    out["child"] = subprocess.run([sys.executable, "-c", code],
                                  env={"PATH": os.environ.get("PATH", "")}).returncode
if "write-cwd" in STEPS:
    os.makedirs("output", exist_ok=True)
    with open(os.path.join("output", "result.json"), "w") as f:
        f.write('{"answer": 42}')
    out["write-cwd"] = "ok"
if "write-outside" in STEPS:
    try:
        with open(os.environ["OUTSIDE_PATH"], "w") as f:
            f.write("side effect")
        out["write-outside"] = "ok"
    except OSError as e:
        out["write-outside"] = errno.errorcode.get(e.errno, str(e.errno))
if "bash" in STEPS:
    out["bash"] = subprocess.run(
        ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/%d" % PORT],
        stderr=subprocess.DEVNULL).returncode
# stdout, not a file: the work dir is read-only inside the sandbox (ADR-0322 slice 1)
print(json.dumps(out))
'''


class _Model(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 -- http.server API
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "capture-answer"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


class Host:
    """Services on the HOST's loopback: a counting listener and a fake model server."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self._accepted += 1
            conn.close()

    def take(self) -> int:
        with self._lock:
            n, self._accepted = self._accepted, 0
        return n

    def close(self) -> None:
        self._sock.close()
        self.model.shutdown()


@pytest.fixture
def host() -> Iterator[Host]:
    h = Host()
    yield h
    h.close()


class Workload:
    def __init__(self, root: Path, mp: pytest.MonkeyPatch, host: Host) -> None:
        self.root, self._mp, self.host = root, mp, host
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.runs, self.replays = root / "runs", root / "replays"
        self._last: Path | None = None
        mp.setenv("LISTENER_PORT", str(host.port))
        mp.setenv("MODEL_URL", f"http://127.0.0.1:{host.model.server_address[1]}/v1")
        mp.setenv("COLUMNS", "400")
        mp.chdir(root)  # the work dir: visible read-only inside the sandbox

    def capture(self, steps: str) -> Path:
        self._mp.setenv("STEPS", steps)
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        self.host.take()
        return capsule

    def replay(self, capsule: Path, *flags: str) -> tuple[Any, dict[str, Any]]:
        result = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        self._last = sorted(self.replays.iterdir())[-1]
        doc = yaml.safe_load((self._last / "replay_result.yaml").read_text())
        for schema in _SCHEMAS:
            jsonschema.validate(doc, schema)
        return result, doc

    @property
    def last_dir(self) -> Path:
        assert self._last is not None
        return self._last

    @property
    def observed(self) -> dict[str, Any] | None:
        """What the last replayed workload printed (the replay capsule's stdout)."""
        stdout = None if self._last is None else self._last / "outputs" / "stdout.txt"
        if stdout is None or not stdout.exists() or not stdout.read_text().strip():
            return None
        return json.loads(stdout.read_text().strip().splitlines()[-1])


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host) -> Workload:
    return Workload(tmp_path, monkeypatch, host)


def _steps() -> str:
    return "root,child" + (",bash" if shutil.which("bash") else "")


@needs_sandbox
def test_hermetic_replay_reaches_no_host_service_from_any_process(work: Workload) -> None:
    capsule = work.capture(_steps())

    # Control: without --hermetic the same replay reaches the host listener.
    result, _doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.host.take() == len(_steps().split(","))

    result, doc = work.replay(capsule, "--hermetic")
    assert result.exit_code == 0, result.output
    assert work.host.take() == 0  # nothing reached the host
    observed = work.observed
    assert observed is not None
    assert observed["root"] == "ConnectionRefusedError"  # the sandbox's own empty loopback
    assert observed["child"] == 3
    if "bash" in observed:
        assert observed["bash"] != 0
    assert doc["status"] == "success"
    assert doc["policy_flags_used"] == ["--hermetic"]
    hermetic = doc["hermetic"]
    assert hermetic["enabled"] is True
    assert hermetic["mechanism"] == "bwrap"
    assert hermetic["network"] == "none"
    assert hermetic["filesystem"] == "read-only"
    roles = [w["role"] for w in hermetic["writable_paths"]]
    assert roles == ["working_directory", "replay_result_dir"]
    assert hermetic["writable_paths"][0]["path"] == str(work.root.resolve())
    assert hermetic["writable_paths"][1]["path"] == str(work.last_dir.resolve())
    assert hermetic["counting"] == "none"
    assert hermetic["connections_blocked"] is None  # not measured is never 0
    # The in-process observers still report the attempts they saw.
    assert doc["replay_contract"]["network_connections_live"] == 2
    assert "hermetic: network none" in " ".join(result.output.split())


@needs_sandbox
def test_served_model_calls_keep_working_inside_the_sandbox(work: Workload) -> None:
    pytest.importorskip("openai")
    capsule = work.capture("openai")
    work.host.model.shutdown()  # the model server is gone: only the capsule can answer
    result, doc = work.replay(capsule, "--hermetic")
    assert result.exit_code == 0, result.output
    assert work.observed == {"openai": "capture-answer"}
    assert doc["model_calls_mocked"] == 1
    assert doc["replay_contract"]["network_connections_live"] == 0


@needs_sandbox
def test_a_workload_that_writes_under_its_cwd_replays_faithfully(work: Workload) -> None:
    """The mode cuts off the network, not the workload's outputs (owner, 2026-11-20):
    the working directory and the replay result dir are writable, the host is not."""
    capsule = work.capture("write-cwd")
    output = work.root / "output" / "result.json"
    output.unlink()
    result, doc = work.replay(capsule, "--hermetic")
    assert result.exit_code == 0, result.output
    assert output.read_text() == '{"answer": 42}'  # written through to the host
    assert work.observed == {"write-cwd": "ok"}
    assert doc["fidelity"]["verdict"] == "faithful", doc["fidelity"]


@needs_sandbox
def test_a_write_outside_the_writable_dirs_fails_inside_the_sandbox(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host path that is neither masked nor writable: the interpreter's own prefix.
    outside = Path(sys.prefix) / f"nf-hermetic-probe-{work.root.name}"
    monkeypatch.setenv("OUTSIDE_PATH", str(outside))
    capsule = work.capture("write-cwd")  # never written outside the sandbox
    monkeypatch.setenv("STEPS", "write-cwd,write-outside")
    try:
        result, doc = work.replay(capsule, "--hermetic")
        assert work.observed == {"write-cwd": "ok", "write-outside": "EROFS"}
        assert not outside.exists()  # the host path was never written
        assert doc["fidelity"]["verdict"] != "faithful"  # stdout differs from the capture
    finally:
        outside.unlink(missing_ok=True)


@needs_sandbox
def test_dry_run_runs_the_preflight(work: Workload) -> None:
    capsule = work.capture("root")
    result, doc = work.replay(capsule, "--dry-run", "--hermetic")
    assert result.exit_code == 0, result.output
    assert "Hermetic (ADR-0322): bwrap" in result.output
    assert "error" not in doc
    assert work.observed is None


def _fake_bwrap(directory: Path, script: str) -> None:
    directory.mkdir()
    path = directory / "bwrap"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux refusal paths")
@pytest.mark.parametrize("bwrap", ["missing", "denied"])
def test_refused_never_run_unisolated(
    work: Workload, monkeypatch: pytest.MonkeyPatch, bwrap: str
) -> None:
    capsule = work.capture("root")
    bin_dir = work.root / "bin"
    if bwrap == "missing":
        bin_dir.mkdir()
    else:
        _fake_bwrap(bin_dir, "echo 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", str(bin_dir))

    result, doc = work.replay(capsule, "--hermetic", "--hermetic-backend", "bwrap")
    assert result.exit_code == 5, result.output
    assert work.observed is None  # nothing ran
    assert work.host.take() == 0
    assert doc["status"] == "aborted"
    assert doc["error"]["type"] == "HermeticUnavailable"
    assert doc["error"]["check"] == ("bwrap-missing" if bwrap == "missing" else "preflight")
    assert doc["error"]["mechanism"] == "bwrap"
    assert "exit_code" not in doc
    assert doc["fidelity"]["verdict"] == "failed"
    assert "HermeticUnavailable" in result.output
    if bwrap == "denied":
        assert "No permissions to create new namespace" in doc["error"]["stderr"]

    result, doc = work.replay(capsule, "--hermetic", "--hermetic-backend", "bwrap", "--dry-run")
    assert result.exit_code == 5, result.output
    assert result.output.startswith("REFUSED: ")
    assert doc["error"]["type"] == "HermeticUnavailable"


def test_hermetic_applies_to_mocked_mode_only(work: Workload) -> None:
    capsule = work.capture("root")
    for mode in ("forensic", "exact", "semantic"):
        result = runner.invoke(app, ["replay", str(capsule), "--mode", mode, "--hermetic"])
        assert result.exit_code == 1
        assert "--hermetic only applies to --mode mocked" in result.output
    assert os.environ.get("STEPS") == "root"


def test_hermetic_backend_needs_hermetic(work: Workload) -> None:
    capsule = work.capture("root")
    result = runner.invoke(app, ["replay", str(capsule), "--hermetic-backend", "landlock"])
    assert result.exit_code == 1
    assert "--hermetic-backend only applies with --hermetic" in result.output


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux refusal paths")
def test_auto_with_no_usable_backend_refuses_and_lists_every_attempt(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = work.capture("root")
    bin_dir = work.root / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))  # no bwrap
    monkeypatch.setattr(_hermetic._landlock_exec, "probe_abi", lambda: 0)  # no Landlock
    result, doc = work.replay(capsule, "--hermetic")
    assert result.exit_code == 5, result.output
    assert work.observed is None and work.host.take() == 0  # nothing ran
    error = doc["error"]
    assert (error["type"], error["check"]) == ("HermeticUnavailable", "no-backend")
    assert [(a["mechanism"], a["check"]) for a in error["attempts"]] == [
        ("bwrap", "bwrap-missing"), ("landlock", "landlock-abi")]
    assert "hermetic" not in doc


# ── the Landlock + seccomp backend (no user namespace) ───────────────────────


@needs_landlock
@pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
def test_auto_falls_back_to_landlock_when_bwrap_cannot_start_and_says_so(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stock Ubuntu 24.04: bwrap's namespaces are denied. ``--hermetic`` still
    isolates -- with Landlock -- and the result names it; never a silent downgrade."""
    capsule = work.capture("root")
    _fake_bwrap(work.root / "bin",
                "echo 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", f"{work.root / 'bin'}:{os.environ.get('PATH', '')}")
    result, doc = work.replay(capsule, "--hermetic")
    assert result.exit_code == 0, result.output
    assert work.host.take() == 0
    assert work.observed == {"root": "PermissionError"}  # Landlock: EACCES, even to loopback
    assert doc["hermetic"]["mechanism"] == "landlock"
    assert doc["policy_flags_used"] == ["--hermetic"]
    assert "landlock ABI" in " ".join(result.output.split())


@needs_landlock
def test_landlock_replay_reaches_no_host_service_from_any_process(work: Workload) -> None:
    capsule = work.capture(_steps())
    result, _doc = work.replay(capsule)  # control: un-isolated, every step reaches the host
    assert result.exit_code == 0, result.output
    assert work.host.take() == len(_steps().split(","))

    result, doc = work.replay(capsule, *LANDLOCK)
    assert result.exit_code == 0, result.output
    assert work.host.take() == 0  # nothing reached the host
    observed = work.observed
    assert observed is not None
    assert observed["root"] == "PermissionError"  # EACCES: denied, not refused
    assert observed["child"] == 3
    if "bash" in observed:
        assert observed["bash"] != 0  # a non-Python child is confined too
    assert doc["status"] == "success"
    assert doc["policy_flags_used"] == ["--hermetic", "--hermetic-backend=landlock"]
    hermetic = doc["hermetic"]
    assert hermetic["mechanism"] == "landlock"
    assert hermetic["landlock_abi"] >= 4
    assert "bwrap_version" not in hermetic
    assert [w["path"] for w in hermetic["writable_paths"]] == [
        str(work.root.resolve()), str(work.last_dir.resolve())]
    by = {e["control"]: e["by"] for e in hermetic["enforced"]}
    assert by["network.tcp"] == "landlock" and by["network.udp"] == "seccomp"
    assert "filesystem.read_hidden_dirs" in hermetic["not_enforced"]
    assert hermetic["connections_blocked"] is None  # not measured is never 0
    assert doc["replay_contract"]["network_connections_live"] == 2  # in-process observers
    line = " ".join(result.output.split())
    assert "hermetic: network none no TCP even to loopback" in line
    assert "not enforced: filesystem.read_hidden_dirs" in line


@needs_landlock
def test_landlock_serves_a_recorded_openai_call_faithfully(work: Workload) -> None:
    pytest.importorskip("openai")
    capsule = work.capture("openai")
    work.host.model.shutdown()  # the model server is gone: only the capsule can answer
    result, doc = work.replay(capsule, *LANDLOCK)
    assert result.exit_code == 0, result.output
    assert work.observed == {"openai": "capture-answer"}
    assert doc["model_calls_mocked"] == 1
    assert doc["replay_contract"]["network_connections_live"] == 0
    assert doc["fidelity"]["verdict"] == "faithful", doc["fidelity"]


@needs_landlock
def test_landlock_lets_the_workload_write_its_cwd_and_nothing_else(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = Path(sys.prefix) / f"nf-landlock-probe-{work.root.name}"
    monkeypatch.setenv("OUTSIDE_PATH", str(outside))
    capsule = work.capture("write-cwd")
    output = work.root / "output" / "result.json"
    output.unlink()
    result, doc = work.replay(capsule, *LANDLOCK)
    assert result.exit_code == 0, result.output
    assert output.read_text() == '{"answer": 42}'
    assert doc["fidelity"]["verdict"] == "faithful", doc["fidelity"]

    monkeypatch.setenv("STEPS", "write-cwd,write-outside")
    try:
        result, doc = work.replay(capsule, *LANDLOCK)
        assert work.observed == {"write-cwd": "ok", "write-outside": "EACCES"}
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@needs_landlock
def test_landlock_dry_run_runs_the_preflight(work: Workload) -> None:
    capsule = work.capture("root")
    result, doc = work.replay(capsule, "--dry-run", *LANDLOCK)
    assert result.exit_code == 0, result.output
    assert "Hermetic (ADR-0322): landlock ABI" in result.output
    assert "error" not in doc
    assert work.observed is None
