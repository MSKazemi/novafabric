"""ADR-0319 amendment (#52 part 2): network from child processes of a mocked replay.

Found 2026-11-20 (AWS real-workload experiment, workload ``log_mcp``): a LangChain
agent that starts an MCP stdio server as a child (``{"command": sys.executable,
"args": [server.py]}``) replayed with ``child_processes_spawned: 8,
network_connections_live: 0, network_observed: true`` while strace on the process
tree saw 9 TCP connects to pypi.org. Cause: ``mcp``'s ``stdio_client`` starts the
server with ``get_default_environment()`` -- a short allowlist -- so the child lost
``PYTHONPATH`` and the replay variables and loaded no observer; the report then read
as "0 connections" for a process tree it had not observed.

Every replay here is a real ``nova capture`` + ``nova replay``. The oracle is a
listener on 127.0.0.1 in the test process that counts accepted connections -- it is
independent of the replay's own counters. No network beyond loopback.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture import file_effects
from novafabric.cli.main import app
from novafabric.replay import _child_observer
from novafabric.replay._contract import summarize
from novafabric.replay._engine import write_replay_site

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process tree")

REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = [
    json.loads((REPO / d / "replay-result.schema.json").read_text())
    for d in ("schemas", "src/novafabric/schemas")
]

runner = CliRunner()

AGENT = r'''
import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ["LISTENER_PORT"]
CHILD = [sys.executable, os.path.join(HERE, "child.py"), PORT, os.environ["CHILD_OUT"]]
STEPS = os.environ["STEPS"].split(",")
out = {}
if "stripped" in STEPS:
    # The environment an MCP stdio client hands its server: an allowlist.
    env = {"PATH": os.environ.get("PATH", "")}
    if os.environ.get("CHILD_PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["CHILD_PYTHONPATH"]
    out["stripped"] = subprocess.run(CHILD, env=env).returncode
if "inherited" in STEPS:
    out["inherited"] = subprocess.run(CHILD).returncode
if "log-root" in STEPS:
    with open(os.environ["LOG_PATH"], "a") as f:
        f.write("entry\n")
for step in ("log-child", "log-child-dies"):
    if step in STEPS:  # the appender runs with an MCP-style allowlisted environment
        subprocess.run(
            [sys.executable, os.path.join(HERE, "appender.py"), os.environ["LOG_PATH"],
             "die" if step == "log-child-dies" else "exit"],
            env={"PATH": os.environ.get("PATH", "")}, check=True)
if "bash" in STEPS:
    out["bash"] = subprocess.run(
        ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/" + PORT]).returncode
if "mcp" in STEPS:
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(HERE, "server.py"), os.environ.get("SERVER_PORT", PORT)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("echo", {"text": "hi"})
                out["mcp"] = result.content[0].text

    asyncio.run(main())
with open(os.environ["AGENT_OUT"], "w") as f:
    json.dump(out, f)
'''

CHILD = r'''
import json, socket, sys
port, out = int(sys.argv[1]), sys.argv[2]
connected = True
try:
    socket.create_connection(("127.0.0.1", port), timeout=5).close()
except OSError as e:
    connected = repr(e)
with open(out, "a") as f:
    f.write(json.dumps({
        "connected": connected,
        "novafabric_modules": sorted(m for m in sys.modules if m.split(".")[0] == "novafabric"),
    }) + "\n")
'''

APPENDER = r'''
import os, sys
with open(sys.argv[1], "a") as f:
    f.write("entry\n")
if sys.argv[2] == "die":
    os._exit(0)  # no interpreter exit: the child never reports its writes
'''

SERVER = r'''
import socket, sys
from mcp.server.fastmcp import FastMCP
if int(sys.argv[1]):
    try:  # what fastmcp's update check does at start: a connection of its own
        socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5).close()
    except OSError:
        pass
server = FastMCP("probe")

@server.tool()
def echo(text: str) -> str:
    return text

server.run()
'''


class Listener:
    """Counts connections accepted on 127.0.0.1 -- the oracle."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._accepted = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

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


class Workload:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener) -> None:
        self.root = root
        (root / "agent.py").write_text(AGENT)
        (root / "child.py").write_text(CHILD)
        (root / "server.py").write_text(SERVER)
        (root / "appender.py").write_text(APPENDER)
        self.log = root / "log.txt"
        monkeypatch.setenv("LOG_PATH", str(self.log))
        self.out = root / "out.json"
        self.child_out = root / "child.jsonl"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self.listener = listener
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("CHILD_OUT", str(self.child_out))
        monkeypatch.setenv("LISTENER_PORT", str(listener.port))
        monkeypatch.setenv("COLUMNS", "400")
        for var in ("FORCE_COLOR", "COLORTERM", "CHILD_PYTHONPATH"):
            monkeypatch.delenv(var, raising=False)

    def capture(self, steps: str) -> Path:
        self._mp.setenv("STEPS", steps)
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(self.runs), sys.executable,
             str(self.root / "agent.py")],
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        self.out.unlink()
        if self.child_out.exists():
            self.child_out.unlink()
        self.listener.take()
        return capsule

    def replay(self, capsule: Path, *flags: str) -> tuple[Any, dict[str, Any]]:
        result = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        dirs = sorted(self.replays.iterdir())
        doc = yaml.safe_load((dirs[-1] / "replay_result.yaml").read_text())
        for schema in _SCHEMAS:
            jsonschema.validate(doc, schema)
        return result, doc

    @property
    def observed(self) -> dict[str, Any] | None:
        return json.loads(self.out.read_text()) if self.out.exists() else None

    @property
    def child_reports(self) -> list[dict[str, Any]]:
        if not self.child_out.exists():
            return []
        return [json.loads(line) for line in self.child_out.read_text().splitlines()]


@pytest.fixture
def listener() -> Iterator[Listener]:
    srv = Listener()
    yield srv
    srv.close()


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: Listener) -> Workload:
    return Workload(tmp_path, monkeypatch, listener)


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── Python children ──────────────────────────────────────────────────────────


def test_a_python_child_with_a_stripped_environment_reports_its_connections(
    work: Workload,
) -> None:
    capsule = work.capture("stripped")
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.observed == {"stripped": 0}
    assert work.listener.take() == 1  # the oracle: the child did connect
    (report,) = work.child_reports
    assert report["connected"] is True
    # The child's bootstrap is stdlib only: it never imports novafabric.
    assert report["novafabric_modules"] == []
    contract = doc["replay_contract"]
    assert contract["network_connections_live"] == 1
    assert contract["network_connections_live_children"] == 1
    assert contract["network_destinations"] == [f"127.0.0.1:{work.listener.port}"]
    assert contract["child_processes_spawned"] == 1
    assert contract["child_processes_network_observed"] == 1
    assert contract["child_processes_unobserved"] == 0
    assert contract["network_observation_coverage"] == "complete"
    flat = _flat(result.output)
    assert "1 live connection from the replayed Python process and 1 observed child" in flat
    assert "1 child process started during the replay (python" in flat
    assert "it reported its connections" in flat
    assert "lower bound" not in flat and "network activity is unknown" not in flat


def test_a_child_keeps_its_own_pythonpath_and_sitecustomize(work: Workload) -> None:
    """The bootstrap is prepended to the child's PYTHONPATH (never replaces it) and
    runs the ``sitecustomize`` it shadows (e.g. a tracer's bootstrap)."""
    own = work.root / "own-site"
    own.mkdir()
    marker = work.root / "own-sitecustomize-ran"
    (own / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    work._mp.setenv("CHILD_PYTHONPATH", str(own))
    capsule = work.capture("stripped")
    marker.unlink()
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert marker.exists()
    assert doc["replay_contract"]["child_processes_unobserved"] == 0
    assert doc["replay_contract"]["network_connections_live"] == 1


def test_a_python_child_that_inherits_the_environment_is_observed(work: Workload) -> None:
    capsule = work.capture("inherited")
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.listener.take() == 1
    contract = doc["replay_contract"]
    assert contract["network_connections_live"] == 1
    assert contract["network_connections_live_children"] == 1
    assert contract["child_processes_network_observed"] == 1
    assert contract["child_processes_unobserved"] == 0
    assert contract["network_observation_coverage"] == "complete"


def test_an_mcp_stdio_server_child_is_observed(work: Workload) -> None:
    """``log_mcp`` in miniature: the MCP client strips the server's environment."""
    pytest.importorskip("mcp.server.fastmcp")
    capsule = work.capture("mcp")
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.observed == {"mcp": "hi"}
    assert doc["tool_calls_mocked"] == 1  # the tool call was served from the capsule
    assert work.listener.take() == 1  # ... and the server child still connected
    contract = doc["replay_contract"]
    assert contract["network_connections_live"] == 1
    assert contract["network_connections_live_children"] == 1
    assert contract["child_processes_unobserved"] == 0
    assert contract["network_observation_coverage"] == "complete"
    # Its connection keeps the verdict from faithful (live_calls); its writes do not.
    assert _file_check(doc)["equal"] is True, _file_check(doc)


# ── non-Python children ──────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash /dev/tcp")
def test_a_non_python_child_is_counted_unobserved_and_the_count_is_a_lower_bound(
    work: Workload,
) -> None:
    capsule = work.capture("bash")
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.listener.take() == 1  # the oracle saw it ...
    contract = doc["replay_contract"]
    assert contract["network_connections_live"] == 0  # ... the observer could not
    assert contract["network_observed"] is True
    assert contract["child_processes_spawned"] == 1
    assert contract["child_processes_network_observed"] == 0
    assert contract["child_processes_unobserved"] == 1
    assert contract["network_observation_coverage"] == "partial"
    flat = _flat(result.output)
    assert "1 child process started during the replay (bash)" in flat
    assert "1 not observed" in flat
    assert "lower bound" in flat
    # A replay that could not see part of its process tree is never faithful.
    assert doc["fidelity"]["verdict"] != "faithful"


# ── the fold, event by event ─────────────────────────────────────────────────


def _started(pid: int, ppid: int, role: str) -> dict[str, Any]:
    return {"event": "observer_started", "pid": pid, "ppid": ppid, "role": role}


def _fold(events: list[dict[str, Any]]) -> dict[str, Any]:
    base = [
        {"event": "network_observer_installed", "pid": 10},
        {"event": "process_observer_installed", "pid": 10},
        _started(10, 1, "root"),
    ]
    report = summarize([], [], base + events, divergence_policy="fail", substitute_tools=True)
    return report.as_dict()


def test_a_fork_child_that_execs_a_program_is_unobserved() -> None:
    out = _fold([
        {"event": "child_process", "pid": 10, "audit_event": "os.fork"},
        _started(11, 10, "fork"),
        {"event": "child_process_exec", "pid": 11, "argv0": "sh"},
    ])
    assert (out["child_processes_spawned"], out["child_processes_unobserved"]) == (1, 1)
    assert out["network_observation_coverage"] == "partial"


def test_a_fork_child_that_execs_python_and_reports_in_is_observed() -> None:
    out = _fold([
        {"event": "child_process", "pid": 10, "audit_event": "os.fork"},
        _started(11, 10, "fork"),
        {"event": "child_process_exec", "pid": 11, "argv0": "python3"},
        _started(11, 10, "child"),
    ])
    assert out["child_processes_network_observed"] == 1
    assert out["child_processes_unobserved"] == 0


def test_a_fork_child_that_stays_python_is_observed() -> None:
    out = _fold([
        {"event": "child_process", "pid": 10, "audit_event": "os.fork"},
        _started(11, 10, "fork"),
        {"event": "network_live", "pid": 11, "host": "10.0.0.1", "port": 443},
    ])
    assert out["child_processes_unobserved"] == 0
    assert out["network_connections_live"] == 1
    assert out["network_connections_live_children"] == 1
    assert out["network_observation_coverage"] == "complete"


def test_a_python_grandchild_behind_a_shell_leaves_the_shell_unobserved() -> None:
    out = _fold([
        {"event": "child_process", "pid": 10, "audit_event": "subprocess.Popen", "argv0": "sh"},
        _started(12, 11, "child"),  # parent 11 (the shell) never reported in
        {"event": "network_live", "pid": 12, "host": "10.0.0.1", "port": 443},
    ])
    assert out["child_processes_network_observed"] == 0
    assert out["child_processes_unobserved"] == 1
    assert out["network_connections_live"] == 1  # still counted: it was observed


def test_without_lifecycle_events_every_child_is_unobserved() -> None:
    report = summarize(
        [], [],
        [
            {"event": "network_observer_installed", "pid": 10},
            {"event": "process_observer_installed", "pid": 10},
            {"event": "child_process", "pid": 10, "audit_event": "subprocess.Popen"},
        ],
        divergence_policy="fail", substitute_tools=True,
    ).as_dict()
    assert report["child_processes_unobserved"] == 1
    assert report["network_observation_coverage"] == "partial"


def test_coverage_is_none_without_a_network_observer_and_partial_when_capped() -> None:
    none = summarize([], [], [], divergence_policy="fail", substitute_tools=True).as_dict()
    assert none["network_observation_coverage"] == "none"
    capped = _fold([{"event": "child_process_capped", "pid": 10, "cap": 1}])
    assert capped["network_observation_coverage"] == "partial"


# ── the environment a child is given ─────────────────────────────────────────


@pytest.fixture
def replaying(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    site = tmp_path / "site"
    monkeypatch.setenv(_child_observer.OBSERVE_ENV, str(tmp_path / "events.jsonl"))
    monkeypatch.setenv(_child_observer.SITE_ENV, str(site))
    monkeypatch.delenv(_child_observer.QUEUE_ENV, raising=False)
    return site


def test_a_stripped_environment_gets_the_bootstrap_prepended(replaying: Path) -> None:
    given = {"PATH": "/bin", "PYTHONPATH": f"/a{os.pathsep}/b"}
    env = _child_observer.child_env(given)
    assert env is not None and env is not given
    assert env["PYTHONPATH"] == os.pathsep.join([str(replaying), "/a", "/b"])
    assert env[_child_observer.ROLE_ENV] == "child"
    assert env[_child_observer.OBSERVE_ENV] == os.environ[_child_observer.OBSERVE_ENV]
    assert given == {"PATH": "/bin", "PYTHONPATH": f"/a{os.pathsep}/b"}  # caller's dict untouched
    assert _child_observer.child_env(env) is None  # already complete: unchanged


def test_outside_a_replay_or_with_bytes_keys_nothing_is_changed(
    replaying: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _child_observer.child_env({b"PATH": b"/bin"}) is None  # type: ignore[dict-item]
    monkeypatch.delenv(_child_observer.OBSERVE_ENV)
    assert _child_observer.child_env({"PATH": "/bin"}) is None


def test_the_site_is_inert_outside_a_replay(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    write_replay_site(site)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOVAFABRIC_REPLAY_")}
    env["PYTHONPATH"] = str(site)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, socket; print(sorted(m for m in sys.modules if 'novafabric' in m"
         " or m == '_nf_child_observer')); print(type(socket.socket.connect).__name__)"],
        env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    # Nothing imported, socket.connect still the C method (not patched).
    assert proc.stdout.splitlines() == ["[]", "method_descriptor"], proc.stdout + proc.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site"]


# ── file writes of a child with a stripped environment ───────────────────────


def _file_check(doc: dict[str, Any]) -> dict[str, Any]:
    (check,) = [c for c in doc["fidelity"]["checks"] if c["name"] == "file_effects"]
    return check


def test_a_stripped_env_child_that_rewrites_a_recorded_file_makes_the_replay_diverge(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded file is written by the parent at capture and, at replay, only by
    an MCP-style child: before the child observed its writes, the file was never
    judged and the replay read as faithful."""
    capsule = work.capture("log-root")
    assert work.log.read_text() == "entry\n"
    monkeypatch.setenv("STEPS", "log-child")
    result, doc = work.replay(capsule)
    assert work.log.read_text() == "entry\nentry\n"  # the side effect was repeated
    assert doc["fidelity"]["verdict"] == "diverged", doc["fidelity"]
    check = _file_check(doc)
    assert check["equal"] is False
    assert [d["path"] for d in check["differences"]] == [str(work.log)]
    assert str(work.log) in check["note"]
    assert "file_effects" in " ".join(result.output.split())


def test_a_stripped_env_child_that_dies_without_reporting_leaves_writes_unknown(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = work.capture("log-root")
    monkeypatch.setenv("STEPS", "log-child-dies")
    _result, doc = work.replay(capsule)
    check = _file_check(doc)
    assert check["equal"] is None
    assert "without reporting its file writes" in check["note"]
    assert doc["fidelity"]["verdict"] == "not_compared", doc["fidelity"]


def test_the_child_write_observer_constants_match_file_effects() -> None:
    """Duplicated, not imported: the child bootstrap must never import novafabric."""
    pairs = {
        "REPLAY_OBSERVER_EVENT": "REPLAY_OBSERVER_EVENT",
        "REPLAY_WRITES_EVENT": "REPLAY_WRITES_EVENT",
        "MAX_WRITTEN_PATHS": "MAX_PATHS_PER_PROCESS",
        "MAX_PATH_CHARS": "MAX_PATH_CHARS",
        "_WRITE_MODE_CHARS": "_WRITE_MODE_CHARS",
        "_WRITE_FLAGS": "_WRITE_FLAGS",
        "_PSEUDO_ROOTS": "_PSEUDO_ROOTS",
        "_MAX_CALLER_FRAMES": "_MAX_CALLER_FRAMES",
    }
    for mine, theirs in pairs.items():
        assert getattr(_child_observer, mine) == getattr(file_effects, theirs), mine


@pytest.mark.parametrize(
    ("mode", "flags"),
    [("r", 0), ("rb", 0), ("w", 0), ("a", 0), ("x", 0), ("r+", 0), ("ab", 0),
     (None, os.O_RDONLY), (None, os.O_WRONLY), (None, os.O_RDWR), (None, os.O_CREAT),
     (None, os.O_APPEND), (None, os.O_TRUNC), (None, True), ("r", "w"), (5, None)],
)
def test_write_intent_matches_file_effects(mode: Any, flags: Any) -> None:
    assert _child_observer.write_intent(mode, flags) == file_effects.write_intent(mode, flags)


def test_an_mcp_stdio_server_child_that_writes_nothing_stays_faithful(
    work: Workload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """log_mcp without the update check: the observed child reports no writes and no
    connections, so observing it costs the verdict nothing."""
    pytest.importorskip("mcp.server.fastmcp")
    monkeypatch.setenv("SERVER_PORT", "0")
    capsule = work.capture("mcp")
    result, doc = work.replay(capsule)
    assert result.exit_code == 0, result.output
    assert work.listener.take() == 0
    contract = doc["replay_contract"]
    assert contract["child_processes_unobserved"] == 0
    assert _file_check(doc)["equal"] is True
    assert doc["fidelity"]["verdict"] == "faithful", doc["fidelity"]
