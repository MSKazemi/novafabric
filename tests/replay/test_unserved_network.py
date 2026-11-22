"""W0 (replay strategy 2026-11-21, ADR-0319 amendment): unserved network fails a strict replay.

Before: a strict mocked replay reported a live non-loopback connection only as a
counter (``network_connections_live``); the status could still be ``success``. A model
surface replay has no record for -- Google Gemini / Vertex before they were in the URL
registry, gRPC, an unregistered provider, a raw-HTTP tool -- therefore ran live with
no divergence: the same silent fail-open as the original Ollama defect.

Now every non-loopback IPv4/IPv6 connect attempt an observed process makes (served
surfaces answer in-process and never open a socket) is counted in
``network_connections_unserved``; in a strict replay it is the divergence
``unserved_network`` (destinations listed), which fails the replay. ``--permissive``
counts and reports it, as before. Loopback (127.0.0.0/8, ::1, ``localhost``) and
AF_UNIX stay allowed and reported as before.

The e2e tests use TEST-NET-1 (192.0.2.1, RFC 5737: never routed) with a non-blocking
connect, so nothing is ever awaited or reached.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from _mocked_replay_agent import openai_record, write_agent, write_capsule
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._child_observer import SocketObserver, is_loopback_host
from novafabric.replay._contract import summarize
from novafabric.replay._result import ReplayResult
from novafabric.replay._unserved_network import (
    UNSERVED_NETWORK,
    UNSERVED_NETWORK_AFTER_CAP_EVENT,
)

REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = [
    json.loads((REPO / d / "replay-result.schema.json").read_text())
    for d in ("schemas", "src/novafabric/schemas", "ui/dashboard/src/data/schemas")
]

runner = CliRunner()


# ── the loopback rule ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.9.8.7", "::1", "localhost", "LOCALHOST", "api.localhost",
     "::ffff:127.0.0.1", "0.0.0.0", "::"],
)
def test_loopback_hosts_are_allowed(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["192.0.2.1", "10.0.0.1", "169.254.169.254", "8.8.8.8", "2001:db8::1",
     "::ffff:192.0.2.1", "generativelanguage.googleapis.com", "localhost.evil.example",
     "127.0.0.1.nip.io", "", "not an address"],
)
def test_everything_else_is_not_loopback(host: str) -> None:
    assert is_loopback_host(host) is False


# ── the fold: strict diverges, permissive reports, loopback never ────────────


def _net(host: str, port: int = 443, pid: int = 100) -> dict[str, Any]:
    return {"event": "network_live", "host": host, "port": port, "pid": pid}


_ROOT = {"event": "observer_started", "role": "root", "pid": 100}
_INSTALLED = {"event": "network_observer_installed", "pid": 100}


def _kinds(report: Any) -> list[str]:
    return [d["kind"] for d in report.divergences]


def test_strict_replay_turns_a_non_loopback_connection_into_unserved_network() -> None:
    events = [_INSTALLED, _ROOT, _net("192.0.2.1", 443), _net("192.0.2.1", 443),
              _net("10.1.2.3", 80), _net("127.0.0.1", 11434)]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert _kinds(report) == [UNSERVED_NETWORK]
    (div,) = report.divergences
    assert div["connections"] == 3
    assert div["destinations"] == ["192.0.2.1:443", "10.1.2.3:80"]
    assert "192.0.2.1:443" in div["message"] and "--permissive" in div["message"]
    out = report.as_dict()
    assert out["network_connections_live"] == 4  # loopback still counted, as before
    assert out["network_connections_unserved"] == 3
    assert out["network_destinations_unserved"] == ["192.0.2.1:443", "10.1.2.3:80"]
    assert out["unserved_network_policy"] == "divergence"
    for schema in _SCHEMAS:
        jsonschema.validate(out, schema["properties"]["replay_contract"])


def test_permissive_replay_counts_unserved_network_without_a_divergence() -> None:
    events = [_INSTALLED, _ROOT, _net("192.0.2.1")]
    report = summarize([], [], events, divergence_policy="warn", substitute_tools=True)
    assert report.divergences == []
    out = report.as_dict()
    assert out["network_connections_unserved"] == 1
    assert out["network_destinations_unserved"] == ["192.0.2.1:443"]
    assert out["unserved_network_policy"] == "reported"


def test_loopback_only_is_never_a_divergence() -> None:
    events = [_INSTALLED, _ROOT, _net("127.0.0.1"), _net("::1"), _net("localhost", 8080)]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert report.divergences == []
    assert report.as_dict()["network_connections_unserved"] == 0
    assert report.as_dict()["network_destinations_unserved"] == []


def test_a_child_process_connection_is_unserved_network_too() -> None:
    """c08: an MCP stdio server child's live version check went to the network. The
    run did go live, so a strict replay must not pass."""
    child = {"event": "observer_started", "role": "child", "pid": 200, "ppid": 100}
    events = [_INSTALLED, _ROOT,
              {"event": "child_process", "pid": 100, "argv0": "python3"},
              child, _net("192.0.2.1", 443, pid=200)]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert _kinds(report) == [UNSERVED_NETWORK]
    (div,) = report.divergences
    assert div["connections_children"] == 1
    assert "child" in div["message"]
    assert report.as_dict()["network_connections_live_children"] == 1


def test_a_connection_past_the_event_cap_still_counts_and_is_a_lower_bound() -> None:
    events = [_INSTALLED, _ROOT, _net("127.0.0.1"),
              {"event": "network_live_capped", "cap": 1, "pid": 100},
              {"event": UNSERVED_NETWORK_AFTER_CAP_EVENT, "host": "192.0.2.1", "port": 9,
               "pid": 100}]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert _kinds(report) == [UNSERVED_NETWORK]
    (div,) = report.divergences
    assert div["destinations"] == ["192.0.2.1:9"]
    assert "lower bound" in div["message"]
    assert report.as_dict()["network_connections_unserved"] == 1


def test_a_partial_observation_says_the_count_is_a_lower_bound() -> None:
    events = [_INSTALLED, _ROOT, {"event": "process_observer_installed", "pid": 100},
              {"event": "child_process", "pid": 100, "argv0": "curl"},
              _net("192.0.2.1")]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    (div,) = report.divergences
    assert "lower bound" in div["message"]


# ── the observers: one event past the cap for a non-loopback attempt ─────────


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.items.append((event, fields))


@pytest.mark.parametrize("which", ["dispatcher", "child"])
def test_observers_report_the_first_non_loopback_attempt_past_the_cap(which: str) -> None:
    from novafabric.replay._dispatcher import NetworkObserver

    events = _Events()
    observer: Any = (
        NetworkObserver(events, cap=1)  # type: ignore[arg-type]
        if which == "dispatcher" else SocketObserver(events, cap=1)
    )
    sock = socket.socket()
    try:
        observer.seen(sock, ("127.0.0.1", 1))
        observer.seen(sock, ("127.0.0.1", 2))  # over the cap, loopback: nothing more
        observer.seen(sock, ("192.0.2.1", 9))  # over the cap, non-loopback: once
        observer.seen(sock, ("192.0.2.2", 9))  # already said: nothing more
    finally:
        sock.close()
    assert events.items == [
        ("network_live", {"host": "127.0.0.1", "port": 1}),
        ("network_live_capped", {"cap": 1}),
        (UNSERVED_NETWORK_AFTER_CAP_EVENT, {"host": "192.0.2.1", "port": 9}),
    ]


# ── end to end: a real replayed process ──────────────────────────────────────


class Agent:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.path = write_agent(root)
        self.out = root / "out.json"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_SIDE", str(root / "side.log"))
        monkeypatch.setenv("COLUMNS", "400")
        for var in ("AGENT_CANNED", "FAKE_ANTHROPIC_CANNED", "FORCE_COLOR", "COLORTERM"):
            monkeypatch.delenv(var, raising=False)

    def capsule(self, model_calls: list[dict[str, Any]], plan: list[dict[str, Any]]) -> Path:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        return write_capsule(self.root, self.path, model_calls, [])

    def replay(
        self, model_calls: list[dict[str, Any]], plan: list[dict[str, Any]], *,
        permissive: bool = False,
    ) -> ReplayResult:
        cap = self.capsule(model_calls, plan)
        result = ReplayEngine(
            capsule_dir=cap,
            flags=ReplayFlags(mode="mocked", permissive=permissive),
            base_dir=self.root / "replays",
        ).run()
        for schema in _SCHEMAS:
            jsonschema.validate(result.as_dict(), schema)
        return result


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Agent:
    pytest.importorskip("openai")
    pytest.importorskip("mcp")
    return Agent(tmp_path, monkeypatch)


def _contract(result: ReplayResult) -> dict[str, Any]:
    assert result.replay_contract is not None
    return result.replay_contract


def test_e2e_strict_replay_fails_on_an_unserved_non_loopback_connection(agent: Agent) -> None:
    result = agent.replay([openai_record("x")], [{"op": "chat"}, {"op": "net_out"}])
    contract = _contract(result)
    assert [d["kind"] for d in contract["divergences"]] == [UNSERVED_NETWORK]
    assert contract["network_destinations_unserved"] == ["192.0.2.1:9"]
    assert result.status == "failure"
    assert result.error is not None and result.error["type"] == "ReplayDivergence"
    assert result.divergence_reason is not None
    assert result.divergence_reason.startswith(UNSERVED_NETWORK)
    fidelity = result.fidelity
    assert fidelity is not None and fidelity["verdict"] != "faithful"
    checks = {c["name"]: c for c in fidelity["checks"]}
    assert checks["divergence"]["equal"] is False
    assert checks["live_calls"]["equal"] is False


def test_e2e_permissive_replay_counts_it_and_does_not_diverge(agent: Agent) -> None:
    result = agent.replay(
        [openai_record("x")], [{"op": "chat"}, {"op": "net_out"}], permissive=True
    )
    contract = _contract(result)
    assert contract["divergences"] == []
    assert contract["network_connections_unserved"] == 1
    assert contract["unserved_network_policy"] == "reported"
    assert result.status == "success"
    assert result.fidelity is not None and result.fidelity["verdict"] != "faithful"


def test_e2e_loopback_connection_is_reported_not_a_divergence(agent: Agent) -> None:
    result = agent.replay([openai_record("x")], [{"op": "chat"}, {"op": "net"}])
    contract = _contract(result)
    assert contract["divergences"] == []
    assert contract["network_connections_live"] == 1
    assert contract["network_connections_unserved"] == 0
    assert result.status == "success"


def test_e2e_a_served_openai_call_opens_no_socket(agent: Agent) -> None:
    """Served surfaces answer in-process: the strict rule cannot fire on them."""
    result = agent.replay([openai_record("x")], [{"op": "chat"}])
    contract = _contract(result)
    assert contract["network_observed"] is True
    assert contract["network_connections_live"] == 0
    assert contract["network_connections_unserved"] == 0
    assert contract["unserved_network_policy"] == "divergence"
    assert result.status == "success"


def test_cli_names_the_unserved_destinations(agent: Agent) -> None:
    cap = agent.capsule([openai_record("x")], [{"op": "chat"}, {"op": "net_out"}])
    result = runner.invoke(app, ["replay", str(cap), "-o", str(agent.root / "r")])
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "unserved network: 1 non-loopback connection attempt" in flat
    assert "192.0.2.1:9" in flat
    assert "unserved_network" in flat

    result = runner.invoke(
        app, ["replay", str(cap), "--permissive", "-o", str(agent.root / "p")]
    )
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "unserved network: 1 non-loopback connection attempt" in flat
    assert "reported (--permissive)" in flat


def test_the_agent_helper_still_runs_outside_a_replay(tmp_path: Path) -> None:
    """The ``net_out`` op itself returns at once (non-blocking connect)."""
    import subprocess

    path = write_agent(tmp_path)
    out = tmp_path / "o.json"
    env = {"AGENT_OUT": str(out), "AGENT_SIDE": str(tmp_path / "s"),
           "AGENT_PLAN": json.dumps([{"op": "net_out"}]), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(path)], env=env, timeout=60,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text()) == [{"op": "net_out"}]
