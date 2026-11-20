"""Replay fidelity harness with oracles independent of the replay engine (issue #59).

One scenario = capture once with ``nova capture``, replay once with mocked
``nova replay``. Each phase is observed only through things the harness owns:

==================  =====================================================================
oracle              what it observes (never a NovaFabric counter)
==================  =====================================================================
canary model (E2)   requests that reached the workload's fake OpenAI transport (hit log)
                    and a loopback Ollama-shaped HTTP server in this process; at replay
                    both answer a canary string, so a canary in the output is a live call
sentinel tool (E3)  ``record.tool`` bodies append to a sentinel file
network (E4)        accepted connections on harness-owned loopback servers (a TCP sink
                    plus the fake Ollama server), whoever opened them -- child processes
                    included
side effect (E4)    files created or changed under a directory outside the work dir
outcome (E5)        the workload's own start/end markers (exit code it returned) and the
                    sha256 of its output file, compared with the capture run
budget (E6)         wall clock of the replay phase
==================  =====================================================================

``replay_result.yaml`` is read *separately* and only to (a) compare its counters with
the oracles (E7 report truthfulness) and (b) tell a refusal from a divergence.

Limits stated plainly: the side-effect oracle sees only the designated ``outside``
directory, not the whole filesystem; the network oracle sees only connections to the
harness's own loopback servers (no namespace, no strace). Both are enough for the
corpus because every scenario's effect is aimed at them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

WORKLOAD = Path(__file__).with_name("_workload.py")
CANARY = "CANARY-7d1f0b"
REPLAY_BUDGET_S = 60.0

#: Verdicts. ``faithful``: executed, no live activity of any kind, outcome equal, in
#: budget. ``refused``: no live activity, outcome not reproduced, and the report does
#: not claim success. ``live-reported`` / ``live-unreported``: an oracle saw live
#: activity and the report did / did not acknowledge every kind of it.
#: ``diverged``: anything else (outcome differs with no live activity, yet the report
#: claims success; or over budget).
VERDICTS = ("faithful", "refused", "live-reported", "live-unreported", "diverged")

_runner = CliRunner()


# ── harness-owned loopback servers ───────────────────────────────────────────


class _PhaseCounter:
    def __init__(self) -> None:
        self.phase = "idle"
        self._lock = threading.Lock()
        self._hits: dict[str, int] = {}

    def hit(self) -> None:
        with self._lock:
            self._hits[self.phase] = self._hits.get(self.phase, 0) + 1

    def count(self, phase: str) -> int:
        with self._lock:
            return self._hits.get(phase, 0)


class TcpSink:
    """Loopback TCP server counting accepted connections per phase."""

    def __init__(self) -> None:
        self.counter = _PhaseCounter()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.counter.hit()
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


class FakeOllama:
    """Loopback Ollama-shaped ``/api/chat`` server; answers a canary outside capture."""

    def __init__(self) -> None:
        self.requests = _PhaseCounter()
        self.connections = _PhaseCounter()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # one request per connection

            def setup(self) -> None:
                owner.connections.hit()
                super().setup()

            def do_POST(self) -> None:  # noqa: N802 -- http.server API
                self.rfile.read(int(self.headers.get("content-length", 0)))
                owner.requests.hit()
                capturing = owner.requests.phase.endswith(":capture")
                prefix = "capture-answer" if capturing else CANARY
                body = json.dumps(
                    {
                        "model": "fidelity-model",
                        "created_at": "2026-01-01T00:00:00Z",
                        "message": {"role": "assistant", "content": f"{prefix}:q1"},
                        "done": True,
                        "done_reason": "stop",
                        "prompt_eval_count": 1,
                        "eval_count": 1,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def set_phase(self, phase: str) -> None:
        self.requests.phase = self.connections.phase = phase

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=2)


# ── scenario definition and observations ─────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    sid: str
    title: str
    steps: tuple[str, ...]
    level: str = "standard"
    #: Pre-registered: the verdicts a correct replay system may produce.
    expected: frozenset[str] = frozenset({"faithful"})
    needs: tuple[str, ...] = ()  # executables the scenario needs (skip if absent)


@dataclass
class PhaseObservation:
    """What the oracles saw in one phase -- no field comes from NovaFabric."""

    started: bool
    finished: bool
    exit_code: int | None  # as returned by the workload (its end marker)
    output_digest: str | None
    output: dict[str, Any] | None
    model_hits: int  # fake OpenAI transport + fake Ollama server requests
    canary_in_output: bool
    tool_bodies: int
    connections: int  # TCP sink + fake Ollama server accepts
    outside_writes: list[str]
    wall_s: float

    @property
    def live(self) -> dict[str, int]:
        return {
            "model": max(self.model_hits, int(self.canary_in_output)),
            "tool": self.tool_bodies,
            "network": self.connections,
            "side_effects": len(self.outside_writes),
        }


@dataclass
class Score:
    sid: str
    capture: PhaseObservation
    replay: PhaseObservation
    report: dict[str, Any]
    e7: dict[str, dict[str, Any]]
    acknowledged: dict[str, bool]
    outcome_equal: bool
    verdict: str
    expected: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out = asdict(self)
        out["live"] = self.replay.live
        return out


# ── reading the report (E7 only) ─────────────────────────────────────────────


def report_counters(result: dict[str, Any]) -> dict[str, Any]:
    contract = result.get("replay_contract") or {}
    return {
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "model_calls_mocked": result.get("model_calls_mocked"),
        "model_calls_available": result.get("model_calls_available"),
        "model_calls_recorded": contract.get("model_calls_recorded"),
        "model_calls_live": contract.get("model_calls_live"),
        "tool_calls_mocked": result.get("tool_calls_mocked"),
        "tool_calls_live": result.get("tool_calls_live"),
        "network_connections_live": contract.get("network_connections_live"),
        "network_observed": contract.get("network_observed"),
        "child_processes_spawned": contract.get("child_processes_spawned"),
        "divergences": [d.get("kind") for d in contract.get("divergences") or []],
        "fidelity_verdict": (result.get("fidelity") or {}).get("verdict"),
        "fidelity_failing": [
            c.get("name") for c in (result.get("fidelity") or {}).get("checks") or []
            if c.get("equal") is False and not c.get("advisory")
        ],
    }


def _reported(report: dict[str, Any], kind: str) -> int | None:
    key = {
        "model": "model_calls_live",
        "tool": "tool_calls_live",
        "network": "network_connections_live",
    }.get(kind)
    return None if key is None else report.get(key)


def acknowledges(report: dict[str, Any], kind: str, oracle: int) -> bool:
    """Does the report admit *oracle* live events of *kind*?

    The direct rule is "its counter is at least the oracle count". Each alternative is
    the admission the matching fix proposes, so the strict-xfail tests flip when it lands:
    a divergence naming an unservable / live model call (#49 strict mode, proposed as
    ``model_surface_not_servable``), a declared count of unobserved child processes
    (#52), a non-faithful outcome verdict (#53). A divergence that only says a recorded
    call went unconsumed (``model_calls_unconsumed``) does not admit a live call.
    """
    if oracle == 0:
        return True
    reported = _reported(report, kind)
    if reported is not None and reported >= oracle:
        return True
    if kind == "model":
        return any(
            (k or "").startswith("model_") and ("not_servable" in k or "live" in k)
            for k in report["divergences"]
        )
    if kind == "network":
        return bool(report.get("child_processes_spawned"))
    if kind == "side_effects":
        return report.get("fidelity_verdict") in ("diverged", "failed")
    return False


def classify(
    capture: PhaseObservation,
    replay: PhaseObservation,
    report: dict[str, Any],
    live: dict[str, int] | None = None,
) -> tuple[str, bool, dict[str, bool]]:
    """Verdict from oracle observations; *report* only separates refusal and E7."""
    live = replay.live if live is None else live
    outcome_equal = (
        replay.started
        and replay.finished == capture.finished
        and replay.exit_code == capture.exit_code
        and replay.output_digest == capture.output_digest
    )
    ack = {kind: acknowledges(report, kind, n) for kind, n in live.items()}
    if any(live.values()):
        return ("live-reported" if all(ack.values()) else "live-unreported"), outcome_equal, ack
    if outcome_equal and replay.wall_s <= REPLAY_BUDGET_S:
        return "faithful", outcome_equal, ack
    if not outcome_equal and report.get("status") != "success":
        return "refused", outcome_equal, ack
    return "diverged", outcome_equal, ack


def score_from_report_only(score: Score) -> str:
    """The harness mutation control: replace every oracle with the report's own counters."""
    r = score.report
    live = {
        "model": int(r.get("model_calls_live") or 0),
        "tool": int(r.get("tool_calls_live") or 0),
        "network": int(r.get("network_connections_live") or 0),
        "side_effects": 0,  # the report has no counter for it
    }
    status_ok = r.get("status") == "success"
    if any(live.values()):
        return "live-reported"  # by construction everything it sees is reported
    return "faithful" if status_ok else "refused"


# ── running a scenario ───────────────────────────────────────────────────────


class Harness:
    """Owns the loopback servers and per-scenario directories for one test session."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sink = TcpSink()
        self.ollama = FakeOllama()

    def close(self) -> None:
        self.sink.close()
        self.ollama.close()

    # -- environment ----------------------------------------------------------

    @contextmanager
    def _env(self, base: Path, phase: str, extra: dict[str, str]) -> Iterator[None]:
        env = {
            "HOME": str(base / "home"),
            "XDG_CONFIG_HOME": str(base / "xdg" / "config"),
            "XDG_DATA_HOME": str(base / "xdg" / "data"),
            "XDG_CACHE_HOME": str(base / "xdg" / "cache"),
            "XDG_STATE_HOME": str(base / "xdg" / "state"),
            "NOVAFABRIC_HOME": str(base / "home" / ".novafabric"),
            "NOVAFABRIC_AUDIT_LOG_PATH": str(base / "audit.jsonl"),
            "FIDELITY_PHASE": phase,
            "FIDELITY_CANARY": CANARY,
            "FIDELITY_MARKERS": str(base / f"markers.{phase}"),
            "FIDELITY_MODEL_HITS": str(base / f"model-hits.{phase}"),
            "FIDELITY_SENTINEL": str(base / f"sentinel.{phase}"),
            "FIDELITY_SINK_PORT": str(self.sink.port),
            "FIDELITY_OUTSIDE": str(base / "outside"),
            "OLLAMA_BASE_URL": f"http://127.0.0.1:{self.ollama.port}",
            "AGENT_OUT": str(base / "work" / "out.json"),
            **extra,
        }
        with pytest.MonkeyPatch.context() as mp:
            for key in list(os.environ):
                if key.startswith(("NOVAFABRIC_", "FIDELITY_")) or key in (
                    "FORCE_COLOR",
                    "COLORTERM",
                    "OLLAMA_HOST",
                    "NOVA_CAPTURE_LEVEL",
                ):
                    mp.delenv(key, raising=False)
            for key, value in env.items():
                mp.setenv(key, value)
            mp.chdir(base / "work")
            yield

    # -- observation ----------------------------------------------------------

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, str]:
        return {
            str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        }

    @staticmethod
    def _lines(path: Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    def _observe(
        self, base: Path, phase: str, before: dict[str, str], wall_s: float
    ) -> PhaseObservation:
        markers = self._lines(base / f"markers.{phase}")
        end = [m for m in markers if m.startswith(f"end {phase} rc=")]
        out_path = base / "work" / "out.json"
        raw = out_path.read_bytes() if out_path.exists() else None
        if out_path.exists():
            out_path.unlink()
        after = self._snapshot(base / "outside")
        writes = sorted(k for k, v in after.items() if before.get(k) != v)
        return PhaseObservation(
            started=f"start {phase}" in markers,
            finished=bool(end),
            exit_code=int(end[-1].rsplit("=", 1)[1]) if end else None,
            output_digest=hashlib.sha256(raw).hexdigest() if raw is not None else None,
            output=json.loads(raw) if raw else None,
            model_hits=len(self._lines(base / f"model-hits.{phase}"))
            + self.ollama.requests.count(f"{base.name}:{phase}"),
            canary_in_output=raw is not None and CANARY.encode() in raw,
            tool_bodies=len(self._lines(base / f"sentinel.{phase}")),
            connections=self.sink.counter.count(f"{base.name}:{phase}")
            + self.ollama.connections.count(f"{base.name}:{phase}"),
            outside_writes=writes,
            wall_s=round(wall_s, 3),
        )

    def _run(
        self, base: Path, phase: str, args: list[str], extra: dict[str, str]
    ) -> tuple[PhaseObservation, Any]:
        tag = f"{base.name}:{phase}"
        before = self._snapshot(base / "outside")
        self.sink.counter.phase = tag
        self.ollama.set_phase(tag)
        try:
            with self._env(base, phase, extra):
                t0 = time.monotonic()
                result = _runner.invoke(app, args)
                wall = time.monotonic() - t0
        finally:
            self.sink.counter.phase = "idle"
            self.ollama.set_phase("idle")
        return self._observe(base, phase, before, wall), result

    # -- public API -----------------------------------------------------------

    def capture(self, scenario: Scenario, name: str | None = None) -> Captured:
        for exe in scenario.needs:
            if shutil.which(exe) is None:
                pytest.skip(f"{scenario.sid} needs {exe!r} on PATH")
        base = self.root / (name or scenario.sid)
        for sub in ("work", "outside", "runs", "replays", "home"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        extra = {"FIDELITY_STEPS": ",".join(scenario.steps), "NOVA_CAPTURE_LEVEL": scenario.level}
        obs, result = self._run(
            base,
            "capture",
            ["capture", "--output-dir", str(base / "runs"), sys.executable, str(WORKLOAD)],
            extra,
        )
        capsules = [p for p in (base / "runs").iterdir() if (p / "capsule.yaml").exists()]
        assert len(capsules) == 1, f"{scenario.sid}: no capsule written\n{result.output}"
        assert obs.started and obs.finished, f"{scenario.sid}: capture did not run\n{result.output}"
        return Captured(scenario, base, capsules[0], obs, extra)

    def replay(
        self,
        captured: Captured,
        *flags: str,
        extra: dict[str, str] | None = None,
        mutate_capsule: Callable[[Path], None] | None = None,
        name: str | None = None,
    ) -> Score:
        capsule = captured.capsule
        if mutate_capsule is not None:
            capsule = captured.base / "runs-mutated" / (name or "mutated")
            shutil.copytree(captured.capsule, capsule)
            mutate_capsule(capsule)
        replays = captured.base / "replays"
        before = set(replays.iterdir())
        phase = "replay" if name is None else f"replay-{name}"
        obs, result = self._run(
            captured.base,
            phase,
            ["replay", str(capsule), "-o", str(replays), *flags],
            {**captured.extra, **(extra or {})},
        )
        new = sorted(set(replays.iterdir()) - before)
        assert len(new) == 1, f"{captured.scenario.sid}: no replay result\n{result.output}"
        report = report_counters(yaml.safe_load((new[0] / "replay_result.yaml").read_text()))
        verdict, outcome_equal, ack = classify(captured.observation, obs, report)
        e7 = {
            kind: {
                "oracle": n,
                "reported": _reported(report, kind),
                # None: the report has no counter for this kind (side effects today).
                "agree": None if _reported(report, kind) is None else _reported(report, kind) == n,
                "acknowledged": ack[kind],
            }
            for kind, n in obs.live.items()
        }
        return Score(
            sid=captured.scenario.sid if name is None else f"{captured.scenario.sid}:{name}",
            capture=captured.observation,
            replay=obs,
            report=report,
            e7=e7,
            acknowledged=ack,
            outcome_equal=outcome_equal,
            verdict=verdict,
            expected=sorted(captured.scenario.expected) if name is None else [],
        )


@dataclass
class Captured:
    scenario: Scenario
    base: Path
    capsule: Path
    observation: PhaseObservation
    extra: dict[str, str]


# ── the corpus ───────────────────────────────────────────────────────────────

SCENARIOS: dict[str, Scenario] = {
    s.sid: s
    for s in (
        Scenario("S1", "OpenAI SDK model calls only", ("model",)),
        Scenario(
            "S2",
            "+ record.tool at the default standard level",
            ("model", "tool"),
            expected=frozenset({"refused"}),
        ),
        Scenario("S3", "+ record.tool at forensic", ("model", "tool"), level="forensic"),
        Scenario(
            "S4",
            "+ Python socket to a loopback sink",
            ("model", "net"),
            expected=frozenset({"live-reported", "refused"}),
        ),
        Scenario(
            "S5",
            "+ non-Python child process connecting to the sink",
            ("model", "child_net"),
            expected=frozenset({"live-reported", "refused"}),
            needs=("bash",),
        ),
        Scenario(
            "S6",
            "+ file side effect outside the work dir",
            ("model", "file"),
            expected=frozenset({"live-reported", "refused"}),
        ),
        # S7 was pre-registered on 2026-11-19 as live-reported/refused, when replay had
        # no Ollama surface; ADR-0320 (#51) records and serves /api/chat, so a correct
        # system now replays it faithfully. S9 keeps the unservable-model case (#49).
        Scenario("S7", "Ollama /api/chat to a loopback fake (served, ADR-0320)", ("ollama",)),
        Scenario("S8", "original run exits 1", ("model", "exit1")),
        Scenario(
            "S9",
            "Ollama /api/embed to a loopback fake (a model call replay cannot serve)",
            ("ollama_embed",),
            expected=frozenset({"live-reported", "refused"}),
        ),
    )
}


class Corpus:
    """Lazily captures and replays each scenario once per session."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self._captured: dict[str, Captured] = {}
        self._scores: dict[str, Score] = {}
        self.controls: dict[str, dict[str, Any]] = {}

    def captured(self, sid: str) -> Captured:
        if sid not in self._captured:
            self._captured[sid] = self.harness.capture(SCENARIOS[sid])
        return self._captured[sid]

    def score(self, sid: str) -> Score:
        if sid not in self._scores:
            self._scores[sid] = self.harness.replay(self.captured(sid))
        return self._scores[sid]

    def baseline(self) -> dict[str, Any]:
        return {
            "issue": "#59",
            "scenarios": {
                sid: {"title": SCENARIOS[sid].title, **s.summary()}
                for sid, s in sorted(self._scores.items())
            },
            "controls": self.controls,
        }
