"""The network report counts connect *attempts*, with each attempt's outcome (c08 P9).

c08 log_mcp: the replay reported ``network_connections_live: 1`` while ``strace -f``
counted 9 ``connect()`` calls. The strace log shows 1 TCP connect plus 8 connects
on datagram sockets, each followed by an ``AF_UNSPEC`` disconnect. That is glibc
``getaddrinfo`` ordering the destination addresses (RFC 6724): it sends no packet,
and it runs inside C code that the Python socket methods never see. So the count
of 1 was the workload's true number of attempts. This module pins what the report
counts: every ``connect``/``connect_ex`` call on an IPv4/IPv6 Python socket of an
observed process, whatever its outcome. Each attempt's outcome is listed per
destination (``network_connect_outcomes``).
"""

from __future__ import annotations

import errno
import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay import _child_observer
from novafabric.replay._child_observer import connect_outcome
from novafabric.replay._contract import summarize
from novafabric.replay._dispatcher import NetworkObserver

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sockets")

REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = [
    json.loads((REPO / d / "replay-result.schema.json").read_text())
    for d in ("schemas", "src/novafabric/schemas", "ui/dashboard/src/data/schemas")
]
runner = CliRunner()


class _Events:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


class _Inet6Socket:
    """Stands in for an IPv6 socket (a host may have IPv6 disabled)."""

    family = socket.AF_INET6


def _oserror(code: int) -> OSError:
    return OSError(code, "test")


# ── outcome classification ───────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs,expected", [
    ({"result": None}, "ok"),                                  # connect() returned
    ({"result": 0}, "ok"),                                     # connect_ex() == 0
    ({"result": errno.ECONNREFUSED}, "ECONNREFUSED"),          # connect_ex() errno
    ({"result": errno.EINPROGRESS}, "in_progress"),            # non-blocking connect_ex
    ({"error": BlockingIOError(errno.EINPROGRESS, "x")}, "in_progress"),  # asyncio
    ({"error": _oserror(errno.ENETUNREACH)}, "ENETUNREACH"),
    ({"error": _oserror(errno.ECONNREFUSED)}, "ECONNREFUSED"),
    ({"error": TimeoutError("timed out")}, "timeout"),         # socket.timeout, no errno
    ({"error": ValueError("bad address")}, "error"),
    ({"result": 99999}, "errno_99999"),                        # an errno with no name
])
def test_each_connect_outcome_is_named(kwargs: dict[str, Any], expected: str) -> None:
    assert connect_outcome(**kwargs) == expected


# ── both observers report each attempt's outcome ─────────────────────────────


def _observer(kind: str, events: _Events) -> Any:
    if kind == "dispatcher":
        return NetworkObserver(events)  # type: ignore[arg-type]
    return _child_observer.SocketObserver(events)


@pytest.fixture
def loopback_listener() -> Iterator[int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv.getsockname()[1]
    srv.close()


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.mark.parametrize("kind", ["dispatcher", "child"])
def test_several_failing_attempts_and_one_success_are_all_attempts(
    kind: str, loopback_listener: int
) -> None:
    events = _Events()
    observer = _observer(kind, events)

    def unreachable(sock: Any, address: Any) -> None:
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    connect = observer._wrap(socket.socket.connect)
    failing = observer._wrap(unreachable)
    for _ in range(3):  # what an IPv6 attempt without a route does
        with pytest.raises(OSError) as raised:
            failing(_Inet6Socket(), ("2001:db8::1", 443))
        assert raised.value.errno == errno.ENETUNREACH  # re-raised unchanged
    refused = _closed_port()
    with socket.socket() as s:
        with pytest.raises(ConnectionRefusedError):
            connect(s, ("127.0.0.1", refused))
    with socket.socket() as s:
        assert observer._wrap(socket.socket.connect_ex)(
            s, ("127.0.0.1", refused)) == errno.ECONNREFUSED
    with socket.socket() as s:
        connect(s, ("127.0.0.1", loopback_listener))

    live = [e for e in events.events if e["event"] == "network_live"]
    outcomes = [(e["host"], e["port"], e["outcome"]) for e in events.events
                if e["event"] == "network_connect_outcome"]
    assert len(live) == 6
    assert outcomes == [
        ("2001:db8::1", 443, "ENETUNREACH"),
        ("2001:db8::1", 443, "ENETUNREACH"),
        ("2001:db8::1", 443, "ENETUNREACH"),
        ("127.0.0.1", refused, "ECONNREFUSED"),
        ("127.0.0.1", refused, "ECONNREFUSED"),
        ("127.0.0.1", loopback_listener, "ok"),
    ]
    # The attempt is logged before the call, its outcome after it.
    assert [e["event"] for e in events.events][:2] == ["network_live", "network_connect_outcome"]


@pytest.mark.parametrize("kind", ["dispatcher", "child"])
def test_a_unix_socket_connect_is_neither_an_attempt_nor_an_outcome(
    kind: str, tmp_path: Path
) -> None:
    events = _Events()
    connect = _observer(kind, events)._wrap(socket.socket.connect)
    with socket.socket(socket.AF_UNIX) as s:
        with pytest.raises(OSError):
            connect(s, str(tmp_path / "absent.sock"))
    assert events.events == []


@pytest.mark.parametrize("kind", ["dispatcher", "child"])
def test_attempts_beyond_the_cap_log_no_outcome(kind: str) -> None:
    events = _Events()
    observer = (NetworkObserver(events, cap=1)  # type: ignore[arg-type]
                if kind == "dispatcher" else _child_observer.SocketObserver(events, cap=1))
    ok = observer._wrap(lambda sock, address: None)
    with socket.socket() as s:
        for _ in range(3):
            ok(s, ("127.0.0.1", 9))
    assert [e["event"] for e in events.events] == [
        "network_live", "network_connect_outcome", "network_live_capped",
    ]


# ── the report ───────────────────────────────────────────────────────────────


def _live(host: str, port: int, pid: int = 1) -> dict[str, Any]:
    return {"event": "network_live", "host": host, "port": port, "pid": pid}


def _outcome(host: str, port: int, outcome: str, pid: int = 1) -> dict[str, Any]:
    return {"event": "network_connect_outcome", "host": host, "port": port,
            "outcome": outcome, "pid": pid}


def test_the_report_lists_attempts_and_outcomes_per_destination() -> None:
    events = [
        {"event": "installed"}, {"event": "network_observer_installed"},
        _live("2a04:4e42::223", 443), _outcome("2a04:4e42::223", 443, "ENETUNREACH"),
        _live("151.101.64.223", 443), _outcome("151.101.64.223", 443, "in_progress"),
        _live("151.101.64.223", 443),  # the process died before the call returned
        _live("127.0.0.1", 5000, pid=2), _outcome("127.0.0.1", 5000, "ok", pid=2),
    ]
    contract = summarize([], [], events, divergence_policy="fail",
                         substitute_tools=True).as_dict()
    assert contract["network_connections_live"] == 4
    assert contract["network_connect_outcomes"] == [
        {"destination": "2a04:4e42::223:443", "attempts": 1,
         "outcomes": {"ENETUNREACH": 1}},
        {"destination": "151.101.64.223:443", "attempts": 2,
         "outcomes": {"in_progress": 1, "unknown": 1}},
        {"destination": "127.0.0.1:5000", "attempts": 1, "outcomes": {"ok": 1}},
    ]


def test_no_attempt_means_no_outcome_list() -> None:
    events = [{"event": "installed"}, {"event": "network_observer_installed"}]
    contract = summarize([], [], events, divergence_policy="fail",
                         substitute_tools=True).as_dict()
    assert "network_connect_outcomes" not in contract


def test_an_outcome_without_its_attempt_is_not_counted() -> None:
    """Outcomes never add attempts: only ``network_live`` counts one."""
    events = [{"event": "installed"}, _live("127.0.0.1", 1),
              _outcome("127.0.0.1", 1, "ok"), _outcome("127.0.0.1", 1, "ok"),
              _outcome("10.0.0.1", 2, "ok")]
    contract = summarize([], [], events, divergence_policy="fail",
                         substitute_tools=True).as_dict()
    assert contract["network_connections_live"] == 1
    assert contract["network_connect_outcomes"] == [
        {"destination": "127.0.0.1:1", "attempts": 1, "outcomes": {"ok": 1}},
    ]


# ── end to end: a replayed process and its stripped-environment child ───────


AGENT = r'''
import os, socket, subprocess, sys
listener, refused = int(os.environ["LISTENER_PORT"]), int(os.environ["REFUSED_PORT"])
for _ in range(3):
    try:
        socket.create_connection(("127.0.0.1", refused), timeout=5).close()
    except OSError:
        pass
socket.create_connection(("127.0.0.1", listener), timeout=5).close()
# An MCP stdio server's environment: an allowlist without the replay's variables.
subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "child.py")],
               env={"PATH": os.environ.get("PATH", ""), "LISTENER_PORT": str(listener)},
               check=True)
print("done")
'''

CHILD = r'''
import os, socket
socket.create_connection(("127.0.0.1", int(os.environ["LISTENER_PORT"])), timeout=5).close()
'''


def test_a_replay_reports_every_attempt_and_its_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loopback_listener: int
) -> None:
    (tmp_path / "agent.py").write_text(AGENT)
    (tmp_path / "child.py").write_text(CHILD)
    refused = _closed_port()
    monkeypatch.setenv("LISTENER_PORT", str(loopback_listener))
    monkeypatch.setenv("REFUSED_PORT", str(refused))
    monkeypatch.setenv("COLUMNS", "400")
    runs, replays = tmp_path / "runs", tmp_path / "replays"
    captured = runner.invoke(
        app, ["capture", "--output-dir", str(runs), sys.executable, str(tmp_path / "agent.py")])
    assert captured.exit_code == 0, captured.output
    (capsule,) = [p for p in runs.iterdir() if (p / "capsule.yaml").exists()]

    result = runner.invoke(app, ["replay", str(capsule), "-o", str(replays), "--permissive"])
    assert result.exit_code == 0, result.output
    (replay_dir,) = sorted(replays.iterdir())
    doc = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    for schema in _SCHEMAS:
        jsonschema.validate(doc, schema)
    contract = doc["replay_contract"]
    assert contract["network_connections_live"] == 5
    assert contract["network_connections_live_children"] == 1
    assert contract["network_observation_coverage"] == "complete"
    assert contract["network_connect_outcomes"] == [
        {"destination": f"127.0.0.1:{refused}", "attempts": 3,
         "outcomes": {"ECONNREFUSED": 3}},
        {"destination": f"127.0.0.1:{loopback_listener}", "attempts": 2,
         "outcomes": {"ok": 2}},
    ]


def test_the_child_bootstrap_still_imports_nothing_but_the_standard_library() -> None:
    """``_child_observer`` is copied into the replay's site directory and must stay
    stdlib-only: importing it in a bare interpreter must not pull in novafabric."""
    source = Path(_child_observer.__file__).read_text()
    code = (
        "import sys, types\n"
        "m = types.ModuleType('obs'); exec(compile(sys.stdin.read(), 'obs', 'exec'), m.__dict__)\n"
        "print(m.connect_outcome(error=OSError(101, 'x')))\n"
        "print(sorted(k for k in sys.modules if k.split('.')[0] == 'novafabric'))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], input=source,
                          capture_output=True, text=True, check=True)
    assert done.stdout.splitlines() == ["ENETUNREACH", "[]"]
