"""ADR-0319 / issue #52 part 1: the child processes a replayed process starts.

The observer is an audit hook, which cannot be removed once added, so each case
runs in a fresh interpreter rather than in the test process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from novafabric.replay._contract import summarize
from novafabric.replay._process_observer import program_name

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process calls")


def _observe(tmp_path: Path, body: str) -> list[dict[str, object]]:
    events = tmp_path / "events.jsonl"
    script = tmp_path / "observed.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "from novafabric.replay._contract import ReplayEventLog\n"
        "from novafabric.replay._process_observer import ProcessObserver\n"
        f"observer = ProcessObserver(ReplayEventLog({str(events)!r}))\n"
        "assert observer.install()\n"
        + textwrap.dedent(body)
    )
    subprocess.run([sys.executable, str(script)], check=True, timeout=60)
    if not events.exists():
        return []
    return [json.loads(line) for line in events.read_text().splitlines()]


def _spawned(events: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(e["audit_event"], e.get("argv0")) for e in events if e["event"] == "child_process"]


def test_each_process_creation_call_is_counted_once(tmp_path: Path) -> None:
    events = _observe(tmp_path, """
        subprocess.run(["/bin/true"])
        subprocess.run(["/bin/true"], close_fds=False)  # Popen + its own posix_spawn
        os.system("true")
        os.waitpid(os.posix_spawn("/bin/true", ["true"], os.environ), 0)
        os.spawnv(os.P_WAIT, "/bin/true", ["true"])  # fork, then exec in the child
    """)
    assert _spawned(events) == [
        ("subprocess.Popen", "true"),
        ("subprocess.Popen", "true"),
        ("os.system", "true"),
        ("os.posix_spawn", "true"),
        ("os.fork", None),
    ]
    # The fork child named the program it became, without a second count.
    assert [e.get("argv0") for e in events if e["event"] == "child_process_exec"] == ["true"]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert report.child_processes_spawned == 5
    assert report.child_processes_argv0 == ["true"]


def test_arguments_and_environment_assignments_are_never_logged(tmp_path: Path) -> None:
    events = _observe(tmp_path, """
        os.system("API_TOKEN=s3cret true --password=hunter2")
        subprocess.run(["/bin/true", "--token", "abc123"])
    """)
    text = json.dumps(events)
    for secret in ("s3cret", "hunter2", "abc123", "API_TOKEN"):
        assert secret not in text
    assert _spawned(events) == [("os.system", "true"), ("subprocess.Popen", "true")]


def test_uninstall_stops_observing(tmp_path: Path) -> None:
    events = _observe(tmp_path, """
        observer.uninstall()
        subprocess.run(["/bin/true"])
    """)
    assert _spawned(events) == []


def test_a_process_with_no_children_reports_none(tmp_path: Path) -> None:
    assert _spawned(_observe(tmp_path, "x = 1\n")) == []


def test_program_names_are_basenames_or_withheld() -> None:
    assert program_name("/usr/bin/ssh") == "ssh"
    assert program_name(b"/bin/true") == "true"
    assert program_name(Path("/opt/x/git")) == "git"
    assert program_name("weird name;rm -rf") == "?"
    assert program_name(None) == "?"


def test_summary_caps_and_lists_names_once() -> None:
    events = [
        {"event": "process_observer_installed"},
        {"event": "child_process", "audit_event": "subprocess.Popen", "argv0": "ssh"},
        {"event": "child_process", "audit_event": "subprocess.Popen", "argv0": "ssh"},
        {"event": "child_process", "audit_event": "os.fork"},
        {"event": "child_process_capped", "cap": 3},
    ]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    contract = report.as_dict()
    assert contract["child_processes_observed"] is True
    assert contract["child_processes_spawned"] == 3
    assert contract["child_processes_argv0"] == ["ssh"]
    assert contract["child_processes_capped"] is True
    assert contract["network_observation_scope"] == "python-process"
